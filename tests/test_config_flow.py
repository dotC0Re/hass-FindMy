# ruff: noqa: PLR0917
# pyright: basic, reportAttributeAccessIssue=false, reportArgumentType=false, reportTypedDictNotRequiredAccess=false
"""Tests for the config flow.

The Home Assistant flow manager is not available as a fixture yet, so the steps
are called directly on a flow object wired to a minimal ``hass``. All FindMy
network classes are replaced; nothing talks to Apple.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import plistlib
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
import pytest_asyncio
from findmy import (
    AsyncAppleAccount,
    AsyncSmsSecondFactor,
    FindMyAccessory,
    InvalidCredentialsError,
    KeyPair,
    LoginState,
    UnhandledProtocolError,
)
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow

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


def make_account(
    final_state: LoginState = LoginState.LOGGED_IN,
    login_error: Exception | None = None,
) -> MagicMock:
    """Return a fake account whose login ends in ``final_state`` or raises."""
    account = MagicMock(spec=AsyncAppleAccount)
    account.login_state = LoginState.LOGGED_OUT
    account.account_name = "John"

    async def login(_email, _password):
        if login_error is not None:
            raise login_error
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
    factory_state: dict = {
        "final_state": LoginState.LOGGED_IN,
        "login_error": None,
        "customize": lambda _account: None,
    }

    def factory(*_args, **_kwargs):
        account = make_account(factory_state["final_state"], factory_state["login_error"])
        factory_state["customize"](account)
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


@pytest.mark.parametrize(
    ("url", "logged"),
    [
        ("http://ani.example:6969", "http://ani.example:6969"),
        ("https://user:s3cret@ani.example:6969/v3", "https://**REDACTED**@ani.example:6969/v3"),
        ("https://token@ani.example", "https://**REDACTED**@ani.example"),
    ],
)
def test_anisette_url_is_logged_without_credentials(url: str, logged: str) -> None:
    assert config_flow._url_without_credentials(url) == logged


###########
### R6  ###
###########


_HTML_ERROR = json.JSONDecodeError("Expecting value", "<html>", 0)
LOGIN_ERRORS = [
    (InvalidCredentialsError("wrong password"), None, "invalid_auth"),
    (aiohttp.ClientConnectionError("no route"), None, "cannot_connect"),
    (TimeoutError(), None, "cannot_connect"),
    (UnhandledProtocolError("status 503"), None, "protocol_error"),
    (aiohttp.InvalidURL("http://"), "http://ani.example", "invalid_anisette_url"),
    # A remote URL that serves something other than Anisette JSON.
    (_HTML_ERROR, "http://ani.example", "invalid_anisette_url"),
    (_HTML_ERROR, None, "protocol_error"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "anisette_url", "expected"), LOGIN_ERRORS)
@pytest.mark.parametrize("source", [config_entries.SOURCE_USER, config_entries.SOURCE_REAUTH])
async def test_login_errors_are_form_errors(hass, accounts, source, error, anisette_url, expected):
    """Login failures keep the flow open with a retryable form error."""
    _created, factory_state = accounts
    factory_state["login_error"] = error
    if source == config_entries.SOURCE_REAUTH:
        entry = add_account_entry(hass)
        flow = make_flow(hass, source, entry_id=entry.entry_id)
        await flow.async_step_reauth(dict(entry.data))
        result = await flow.async_step_reauth_confirm(login_input(anisette_url=anisette_url))
        expected_step = "reauth_confirm"
    else:
        flow = make_flow(hass)
        result = await flow.async_step_acc_login(login_input(anisette_url=anisette_url))
        expected_step = "acc_login"

    assert result["type"] == "form"
    assert result["step_id"] == expected_step
    assert result["errors"] == {"base": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["not a url", "ftp://ani.example", "http://", "http://[::1"])
async def test_malformed_anisette_url_is_rejected_before_login(hass, accounts, url):
    created, _factory_state = accounts
    result = await make_flow(hass).async_step_acc_login(login_input(anisette_url=url))

    assert result["step_id"] == "acc_login"
    assert result["errors"] == {"base": "invalid_anisette_url"}
    assert not created


async def _flow_at_2fa_request(hass, accounts):
    created, factory_state = accounts
    factory_state["final_state"] = LoginState.REQUIRE_2FA
    flow = make_flow(hass)
    result = await flow.async_step_acc_login(login_input())
    assert result["step_id"] == "acc_2fa_request"
    return flow, created[-1]


@pytest.mark.asyncio
async def test_2fa_methods_failure_returns_to_login(hass, accounts):
    _created, factory_state = accounts
    factory_state["final_state"] = LoginState.REQUIRE_2FA

    def fail_methods(account):
        account.get_2fa_methods.side_effect = UnhandledProtocolError("status 500")

    factory_state["customize"] = fail_methods

    result = await make_flow(hass).async_step_acc_login(login_input())

    assert result["step_id"] == "acc_login"
    assert result["errors"] == {"base": "protocol_error"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (UnhandledProtocolError("status 500"), "protocol_error"),
        (aiohttp.ClientConnectionError(), "cannot_connect"),
    ],
)
async def test_2fa_request_failure_is_form_error(hass, accounts, error, expected):
    flow, account = await _flow_at_2fa_request(hass, accounts)
    account.sms.request.side_effect = error

    result = await flow.async_step_acc_2fa_request({"method_id": "0"})

    assert result["step_id"] == "acc_2fa_request"
    assert result["errors"] == {"base": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("info", [{"method_id": "1"}, {"method_id": "-1"}, {"method_id": "x"}, {}])
async def test_unknown_2fa_method_is_form_error(hass, accounts, info):
    flow, account = await _flow_at_2fa_request(hass, accounts)

    result = await flow.async_step_acc_2fa_request(info)

    assert result["step_id"] == "acc_2fa_request"
    assert result["errors"] == {"base": "invalid_2fa_method"}
    account.sms.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_2fa_code_stays_on_submit_step(hass, accounts):
    flow, _account = await _flow_at_2fa_request(hass, accounts)
    await flow.async_step_acc_2fa_request({"method_id": "0"})

    result = await flow.async_step_acc_2fa_submit({"code": ""})

    # Was "2fa_submit", a step that does not exist.
    assert result["step_id"] == "acc_2fa_submit"
    assert result["errors"] == {"base": "2fa_invalid"}


@pytest.mark.asyncio
async def test_wrong_2fa_code_can_be_retried(hass, accounts):
    flow, account = await _flow_at_2fa_request(hass, accounts)
    await flow.async_step_acc_2fa_request({"method_id": "0"})
    good_submit = account.sms.submit.side_effect
    account.sms.submit.side_effect = UnhandledProtocolError("status 401")

    result = await flow.async_step_acc_2fa_submit({"code": "000000"})
    assert result["step_id"] == "acc_2fa_submit"
    assert result["errors"] == {"base": "2fa_invalid"}

    account.sms.submit.side_effect = good_submit
    result = await flow.async_step_acc_2fa_submit({"code": CODE})
    assert result["type"] == "create_entry"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state_after", "expected_step", "expected_error"),
    [
        (LoginState.REQUIRE_2FA, "acc_2fa_submit", "2fa_invalid"),
        (LoginState.AUTHENTICATED, "acc_login", "login_incomplete"),
        (LoginState.LOGGED_OUT, "acc_login", "login_incomplete"),
    ],
)
async def test_no_entry_unless_logged_in_after_2fa(
    hass, accounts, state_after, expected_step, expected_error
):
    flow, account = await _flow_at_2fa_request(hass, accounts)
    await flow.async_step_acc_2fa_request({"method_id": "0"})

    async def submit(_code):
        account.login_state = state_after
        return state_after

    account.sms.submit.side_effect = submit
    result = await flow.async_step_acc_2fa_submit({"code": CODE})

    assert result["type"] == "form"
    assert result["step_id"] == expected_step
    assert result["errors"] == {"base": expected_error}
    account.to_json.assert_not_called()


@pytest.mark.asyncio
async def test_no_entry_when_login_stops_half_way(hass, accounts):
    _created, factory_state = accounts
    factory_state["final_state"] = LoginState.AUTHENTICATED

    result = await make_flow(hass).async_step_acc_login(login_input())

    assert result["step_id"] == "acc_login"
    assert result["errors"] == {"base": "login_incomplete"}


def _accessory_json() -> str:
    accessory = FindMyAccessory(
        master_key=os.urandom(28),
        skn=os.urandom(32),
        sks=os.urandom(32),
        paired_at=datetime(2024, 1, 1, tzinfo=UTC),
        name="Tag",
    )
    return json.dumps(accessory.to_json())


_PARTIAL_PLIST = plistlib.dumps({"privateKey": {"key": {"data": b"x" * 28}}})
BAD_DERIVED_FILES = [
    pytest.param("garbage", id="garbage"),
    pytest.param(_PARTIAL_PLIST, id="plist-missing-fields"),
    pytest.param(json.dumps({"master_key": "00"}), id="json-without-type"),
    pytest.param(json.dumps({"type": "custom_rolling_key_accessory"}), id="json-wrong-type"),
    pytest.param(json.dumps(["not", "an", "object"]), id="json-list"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", BAD_DERIVED_FILES)
async def test_bad_derived_key_file_is_form_error(hass, monkeypatch, tmp_path, content):
    monkeypatch.setattr(config_flow, "process_uploaded_file", uploaded(tmp_path, content))

    result = await make_flow(hass).async_step_dev_rolling_derived({"name": "Tag", "file": FILE_ID})

    assert result["step_id"] == "dev_rolling_derived"
    assert result["errors"] == {"base": "invalid_dev_key"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        pytest.param("garbage", id="garbage"),
        pytest.param(json.dumps({"private_keys": []}), id="json-without-type"),
        pytest.param(json.dumps({"type": "accessory"}), id="json-wrong-type"),
    ],
)
async def test_bad_pre_generated_key_file_is_form_error(hass, monkeypatch, tmp_path, content):
    monkeypatch.setattr(config_flow, "process_uploaded_file", uploaded(tmp_path, content))

    result = await make_flow(hass).async_step_dev_rolling_pre_generated(
        {"name": "Tag", "file": FILE_ID},
    )

    assert result["step_id"] == "dev_rolling_pre_generated"
    assert result["errors"] == {"base": "invalid_dev_key"}


@pytest.mark.asyncio
async def test_missing_upload_is_form_error(hass):
    # file_upload is not set up, so every upload id is unknown.
    result = await make_flow(hass).async_step_dev_rolling_derived({"name": "Tag", "file": FILE_ID})

    assert result["errors"] == {"base": "invalid_dev_key"}


@pytest.mark.asyncio
async def test_valid_derived_key_file_creates_entry(hass, monkeypatch, tmp_path):
    monkeypatch.setattr(config_flow, "process_uploaded_file", uploaded(tmp_path, _accessory_json()))

    result = await make_flow(hass).async_step_dev_rolling_derived({"name": "", "file": FILE_ID})

    assert result["type"] == "create_entry"
    assert result["data"]["type"] == "device_rolling_derived"


###########
### R7  ###
###########


@pytest.mark.asyncio
@pytest.mark.usefixtures("accounts")
async def test_reauth_form_prefills_account(hass):
    entry = add_account_entry(hass)
    flow = make_flow(hass, config_entries.SOURCE_REAUTH, entry_id=entry.entry_id)

    result = await flow.async_step_reauth(dict(entry.data))

    assert result["step_id"] == "reauth_confirm"
    assert (result.get("description_placeholders") or {}).get("account") == EMAIL


@pytest.mark.asyncio
@pytest.mark.usefixtures("accounts")
async def test_reauth_with_same_account_updates_entry(hass):
    entry = add_account_entry(hass)
    flow = make_flow(hass, config_entries.SOURCE_REAUTH, entry_id=entry.entry_id)

    result = await flow.async_step_reauth_confirm(login_input(email=EMAIL.upper()))

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data["account_data"] == ACCOUNT_JSON
    hass.config_entries.async_schedule_reload.assert_called_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_reauth_with_other_account_is_rejected(hass, accounts):
    created, _factory_state = accounts
    entry = add_account_entry(hass)
    flow = make_flow(hass, config_entries.SOURCE_REAUTH, entry_id=entry.entry_id)

    with pytest.raises(AbortFlow) as aborted:
        await flow.async_step_reauth_confirm(login_input(email="someone.else@example.com"))

    assert aborted.value.reason == "wrong_account"
    assert not created, "no login may be attempted for the wrong account"
    assert entry.data["account_data"] == {"old": True}


###########
### R14 ###
###########


@pytest.mark.asyncio
async def test_every_login_attempt_closes_its_account(hass, accounts):
    created, factory_state = accounts
    flow = make_flow(hass)

    failed_attempts = 3
    factory_state["login_error"] = InvalidCredentialsError("wrong password")
    for _ in range(failed_attempts):
        result = await flow.async_step_acc_login(login_input())
        assert result["errors"] == {"base": "invalid_auth"}
    factory_state["login_error"] = None
    result = await flow.async_step_acc_login(login_input())

    assert result["type"] == "create_entry"
    assert len(created) == failed_attempts + 1
    for account in created:
        account.close.assert_awaited_once()
    # Serialised before closing, so the entry carries the session.
    assert result["data"]["account_data"] == ACCOUNT_JSON


@pytest.mark.asyncio
async def test_account_stays_open_during_2fa(hass, accounts):
    flow, account = await _flow_at_2fa_request(hass, accounts)
    await flow.async_step_acc_2fa_request({"method_id": "0"})
    account.sms.submit.side_effect = UnhandledProtocolError("status 401")
    await flow.async_step_acc_2fa_submit({"code": "000000"})

    account.close.assert_not_awaited()

    account.sms.submit.side_effect = None
    account.sms.submit.return_value = LoginState.LOGGED_IN
    account.login_state = LoginState.LOGGED_IN
    result = await flow.async_step_acc_2fa_submit({"code": CODE})

    assert result["type"] == "create_entry"
    account.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_flow_closes_account(hass, accounts):
    flow, account = await _flow_at_2fa_request(hass, accounts)

    flow.async_remove()
    await hass.async_block_till_done()

    account.close.assert_awaited_once()
    # A flow without an open account has nothing to clean up.
    make_flow(hass).async_remove()


@pytest.mark.asyncio
async def test_failed_2fa_lookup_closes_account(hass, accounts):
    created, factory_state = accounts
    factory_state["final_state"] = LoginState.REQUIRE_2FA

    def fail_methods(account):
        account.get_2fa_methods.side_effect = aiohttp.ClientConnectionError()

    factory_state["customize"] = fail_methods
    result = await make_flow(hass).async_step_acc_login(login_input())

    assert result["errors"] == {"base": "cannot_connect"}
    created[0].close.assert_awaited_once()


@pytest.mark.asyncio
async def test_reauth_closes_account(hass, accounts):
    created, _factory_state = accounts
    entry = add_account_entry(hass)
    flow = make_flow(hass, config_entries.SOURCE_REAUTH, entry_id=entry.entry_id)

    result = await flow.async_step_reauth_confirm(login_input())

    assert result["reason"] == "reauth_successful"
    created[0].close.assert_awaited_once()
