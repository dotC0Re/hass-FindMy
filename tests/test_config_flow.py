# ruff: noqa: S105, SLF001, ANN001, ANN201, ANN202, INP001, TC003
# pyright: basic, reportAttributeAccessIssue=false, reportArgumentType=false, reportTypedDictNotRequiredAccess=false
"""Tests for the config flow.

The Home Assistant flow manager is not available as a fixture yet, so the steps
are called directly on a flow object wired to a minimal ``hass``. All FindMy
network classes are replaced; nothing talks to Apple.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from findmy import (
    AsyncAppleAccount,
    AsyncSmsSecondFactor,
    KeyPair,
    LoginState,
)
from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from custom_components.findmy import config_flow
from custom_components.findmy.const import DOMAIN

EMAIL = "john.secret@example.com"
PASSWORD = "PW-SECRET-4711"
CODE = "CODE-987654"
FILE_ID = "FILE-ID-SECRET-0815"
ACCOUNT_JSON = {"account": {"username": EMAIL}, "marker": "account-json"}


@pytest_asyncio.fixture
async def hass(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    # Reloading an entry would try to set up the integration for real.
    hass.config_entries.async_schedule_reload = MagicMock()
    yield hass
    await hass.async_stop(force=True)


def make_account(final_state: LoginState = LoginState.LOGGED_IN) -> MagicMock:
    """Return a fake account whose login ends in ``final_state``."""
    account = MagicMock(spec=AsyncAppleAccount)
    account.login_state = LoginState.LOGGED_OUT
    account.account_name = "John"

    async def login(_email, _password):
        account.login_state = final_state
        return final_state

    account.login = AsyncMock(side_effect=login)
    account.close = AsyncMock()
    account.to_json = MagicMock(return_value=ACCOUNT_JSON)

    sms = MagicMock(spec=AsyncSmsSecondFactor)
    sms.phone_number = "+49 *** 42"
    sms.request = AsyncMock()

    async def submit(_code):
        account.login_state = LoginState.LOGGED_IN
        return LoginState.LOGGED_IN

    sms.submit = AsyncMock(side_effect=submit)
    account.get_2fa_methods = AsyncMock(return_value=[sms])
    account.sms = sms
    return account


@pytest.fixture
def accounts(monkeypatch):
    """Patch the FindMy classes; every created account is appended to the list."""
    created: list[MagicMock] = []
    factory_state = {"final_state": LoginState.LOGGED_IN}

    def factory(*_args, **_kwargs):
        account = make_account(factory_state["final_state"])
        created.append(account)
        return account

    monkeypatch.setattr(config_flow, "AsyncAppleAccount", factory)
    monkeypatch.setattr(config_flow, "LocalAnisetteProvider", MagicMock())
    monkeypatch.setattr(config_flow, "RemoteAnisetteProvider", MagicMock())
    return created, factory_state


def make_flow(hass, source: str = config_entries.SOURCE_USER, **context):
    flow = config_flow.InitialSetupConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.flow_id = f"flow-{id(flow)}"
    flow.context = {"source": source, **context}
    return flow


def add_account_entry(hass, unique_id: str = EMAIL) -> config_entries.ConfigEntry:
    entry = config_entries.ConfigEntry(
        data={"type": "account", "account_data": {"old": True}},
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        minor_version=1,
        options={},
        source=config_entries.SOURCE_USER,
        subentries_data=None,
        title=f"Account: {unique_id}",
        unique_id=unique_id,
        version=1,
    )
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


def uploaded(tmp_path: Path, content: str | bytes):
    """Return a stand-in for ``process_uploaded_file`` that serves ``content``."""
    path = tmp_path / "upload.bin"
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)

    @contextlib.contextmanager
    def process_uploaded_file(_hass, _file_id):
        yield path

    return process_uploaded_file


def login_input(email: str = EMAIL, password: str = PASSWORD, anisette_url: str | None = None):
    return {
        "email": email,
        "password": password,
        "advanced_options": {"anisette_url": anisette_url} if anisette_url else {},
    }


###########
### S2  ###
###########


@pytest.mark.asyncio
async def test_no_secrets_in_debug_log(hass, accounts, monkeypatch, caplog, tmp_path):
    """Walk every step with recognisable secrets; none may reach the log."""
    _created, factory_state = accounts
    factory_state["final_state"] = LoginState.REQUIRE_2FA
    caplog.set_level(logging.DEBUG)

    flow = make_flow(hass)
    await flow.async_step_user()
    await flow.async_step_start_acc()
    result = await flow.async_step_acc_login(login_input())
    assert result["step_id"] == "acc_2fa_request"
    await flow.async_step_acc_2fa_request({"method_id": "0"})
    result = await flow.async_step_acc_2fa_submit({"code": CODE})
    assert result["type"] == "create_entry"

    # Re-authentication of an existing account.
    entry = add_account_entry(hass)
    factory_state["final_state"] = LoginState.LOGGED_IN
    reauth = make_flow(hass, config_entries.SOURCE_REAUTH, entry_id=entry.entry_id)
    await reauth.async_step_reauth(dict(entry.data))
    await reauth.async_step_reauth_confirm(login_input())

    # Device steps.
    private_key = KeyPair.new().private_key_b64
    dev = make_flow(hass)
    await dev.async_step_start_dev()
    await dev.async_step_dev_choose({"device_type": "static"})
    result = await dev.async_step_dev_static({"name": "Tag", "private_key": private_key})
    assert result["type"] == "create_entry"

    monkeypatch.setattr(config_flow, "process_uploaded_file", uploaded(tmp_path, "garbage"))
    await make_flow(hass).async_step_dev_rolling_derived({"name": "Tag", "file": FILE_ID})
    await make_flow(hass).async_step_dev_rolling_pre_generated({"name": "Tag", "file": FILE_ID})

    log = caplog.text
    assert "Step: dev_static" in log, "the test must actually capture the step logs"
    for secret in (PASSWORD, CODE, private_key, FILE_ID, EMAIL):
        assert secret not in log
    assert "j***@example.com" in log
    # The address is only logged on debug level.
    assert all(EMAIL not in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno >= logging.INFO and "***@" in r.getMessage()]


def test_redact_masks_nested_and_keeps_harmless_fields():
    redacted = config_flow._redact(
        {
            "email": "a@b.c",
            "password": "x",
            "advanced_options": {"anisette_url": "http://ani", "code": "1"},
            "name": "Tag",
        },
    )
    assert redacted == {
        "email": "a***@b.c",
        "password": "**REDACTED**",
        "advanced_options": {"anisette_url": "http://ani", "code": "**REDACTED**"},
        "name": "Tag",
    }
    assert config_flow._redact(None) is None
    assert config_flow._mask_email("no-at-sign") == "**REDACTED**"
