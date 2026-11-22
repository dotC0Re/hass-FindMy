# ruff: noqa: PLR0917
# pyright: basic, reportAttributeAccessIssue=false, reportArgumentType=false, reportTypedDictNotRequiredAccess=false
"""Tests for the config flow.

The Home Assistant flow manager is not available as a fixture yet, so the steps
are called directly on a flow object wired to a minimal ``hass``. All FindMy
network classes are replaced; nothing talks to Apple.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import json
import logging
import os
import plistlib
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import cast
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
    LocalAnisetteProvider,
    LoginState,
    RemoteAnisetteProvider,
    UnhandledProtocolError,
)
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow

from custom_components.findmy import anisette_libs, config_flow
from custom_components.findmy.account import without_password
from custom_components.findmy.anisette_libs import (
    AnisetteLibsChecksumError,
    AnisetteLibsUnavailableError,
    anisette_libs_path,
)
from custom_components.findmy.const import DOMAIN

from .offline import FakeDownload, install_network_guard, make_library_archive

EMAIL = "john.secret@example.com"
PASSWORD = "PW-SECRET-4711"
CODE = "CODE-987654"
FILE_ID = "FILE-ID-SECRET-0815"
# Shaped like FindMy's serialised account state, which includes the password.
ACCOUNT_JSON = {
    "type": "account",
    "ids": {"uid": "uid", "devid": "devid"},
    "account": {"username": EMAIL, "password": PASSWORD, "info": None},
    "login": {
        "state": LoginState.LOGGED_IN.value,
        "data": {"dsid": "1", "mobileme_data": {"tokens": {"searchPartyToken": "token"}}},
    },
    "anisette": {"type": "local"},
    "marker": "account-json",
}
STORED_ACCOUNT_JSON = without_password(ACCOUNT_JSON)
WORKER_THREAD = "findmy-login"


def login_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == WORKER_THREAD]


@pytest_asyncio.fixture
async def hass(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    # Reloading an entry would try to set up the integration for real.
    hass.config_entries.async_schedule_reload = MagicMock()
    yield hass
    await hass.async_stop(force=True)
    # A flow that was left open is cleaned up by Home Assistant's shutdown at the latest.
    assert not login_threads(), "a sign-in thread outlived Home Assistant"


@pytest.fixture(autouse=True)
def network_guard(monkeypatch):
    """No test of this file may reach the network, see offline.py."""
    attempts = install_network_guard(monkeypatch)
    yield
    assert not attempts, f"the network was used: {attempts}"


class LibsStub:
    """Stands in for the check and download of the Anisette library bundle."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0
        self.error: Exception | None = None

    async def ensure(self, _hass) -> Path:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.path


@pytest.fixture(autouse=True)
def libs(hass, monkeypatch) -> LibsStub:
    """The bundle is "in place" for every flow; anisette_libs.py has tests of its own."""
    stub = LibsStub(anisette_libs_path(hass))
    monkeypatch.setattr(config_flow, "async_ensure_anisette_libs", stub.ensure)
    return stub


def _record(calls: list[tuple[str, str]], name: str) -> None:
    calls.append((name, threading.current_thread().name))


def make_account(
    final_state: LoginState = LoginState.LOGGED_IN,
    login_error: BaseException | None = None,
    calls: list[tuple[str, str]] | None = None,
) -> MagicMock:
    """Return a fake account whose login ends in ``final_state`` or raises.

    Every library call is noted in ``account.calls`` with the thread it ran on.
    """
    account = MagicMock(spec=AsyncAppleAccount)
    account.calls = [] if calls is None else calls
    account.login_state = LoginState.LOGGED_OUT
    account.account_name = "John"

    async def login(_email, _password):
        _record(account.calls, "login")
        if login_error is not None:
            raise login_error
        account.login_state = final_state
        return final_state

    async def close():
        _record(account.calls, "close")

    def to_json():
        _record(account.calls, "to_json")
        return ACCOUNT_JSON

    account.login = AsyncMock(side_effect=login)
    account.close = AsyncMock(side_effect=close)
    account.to_json = MagicMock(side_effect=to_json)

    sms = MagicMock(spec=AsyncSmsSecondFactor)
    sms.phone_number = "+49 *** 42"

    async def request():
        _record(account.calls, "request")

    async def submit(_code):
        _record(account.calls, "submit")
        account.login_state = LoginState.LOGGED_IN
        return LoginState.LOGGED_IN

    async def get_2fa_methods():
        _record(account.calls, "get_2fa_methods")
        return [sms]

    sms.request = AsyncMock(side_effect=request)
    sms.submit = AsyncMock(side_effect=submit)
    account.get_2fa_methods = AsyncMock(side_effect=get_2fa_methods)
    account.sms = sms
    return account


@pytest.fixture
def accounts(monkeypatch):
    """Patch the FindMy classes; every created account is appended to the list.

    ``factory_state["calls"]`` lists every library call, constructors included, with
    the thread it ran on.
    """
    created: list[MagicMock] = []
    calls: list[tuple[str, str]] = []
    factory_state: dict = {
        "final_state": LoginState.LOGGED_IN,
        "login_error": None,
        "customize": lambda _account: None,
        "calls": calls,
    }

    def factory(*_args, **_kwargs):
        _record(calls, "AsyncAppleAccount")
        account = make_account(factory_state["final_state"], factory_state["login_error"], calls)
        factory_state["customize"](account)
        created.append(account)
        return account

    def provider(name: str):
        mock = MagicMock(side_effect=lambda *_args, **_kwargs: _record(calls, name))
        # Kept so that a test can look at how the provider was created.
        factory_state[name] = mock
        return mock

    monkeypatch.setattr(config_flow, "AsyncAppleAccount", factory)
    monkeypatch.setattr(config_flow, "LocalAnisetteProvider", provider("LocalAnisetteProvider"))
    monkeypatch.setattr(config_flow, "RemoteAnisetteProvider", provider("RemoteAnisetteProvider"))
    return created, factory_state


def make_flow(hass, source: str = config_entries.SOURCE_USER, **context):
    flow = config_flow.InitialSetupConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.flow_id = f"flow-{id(flow)}"
    flow.context = {"source": source, **context}
    return flow


def add_account_entry(
    hass, unique_id: str = EMAIL, account_data: object = None
) -> config_entries.ConfigEntry:
    """An account entry; by default with data nothing can be restored from."""
    entry = config_entries.ConfigEntry(
        data={
            "type": "account",
            "account_data": {"old": True} if account_data is None else account_data,
        },
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


def test_redaction_strips_credentials_from_the_anisette_url() -> None:
    """The URL arrives inside the advanced options of the login form."""
    redacted = config_flow._redact(
        {
            "email": "jane@example.com",
            "password": "hunter2",
            "advanced_options": {"anisette_url": "https://user:s3cret@ani.example/v3"},
        }
    )

    assert "s3cret" not in repr(redacted)
    assert redacted is not None
    assert redacted["advanced_options"] == {"anisette_url": "https://**REDACTED**@ani.example/v3"}


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.InvalidURL("https://user:s3cret@ani.example:port/v3"),
        aiohttp.ClientConnectionError("Cannot connect to https://user:s3cret@ani.example/v3"),
    ],
)
def test_logged_errors_do_not_quote_url_credentials(
    caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    """aiohttp puts the request URL into its errors, which were logged as they came."""
    with caplog.at_level(logging.DEBUG):
        _ = config_flow._error_key(error, "login", remote_anisette=True)

    assert "s3cret" not in caplog.text
    assert "ani.example" in caplog.text


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
    (KeyError("searchPartyToken"), None, "find_my_token_missing"),
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
    assert entry.data["account_data"] == STORED_ACCOUNT_JSON
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
    assert result["data"]["account_data"] == STORED_ACCOUNT_JSON


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


###########
### R20 ###
###########

LIBRARY_CALLS = {
    "AsyncAppleAccount",
    "LocalAnisetteProvider",
    "login",
    "get_2fa_methods",
    "request",
    "submit",
    "to_json",
    "close",
}
# The fake login computes this long without awaiting, like Anisette emulation and SRP.
BURN_SECONDS = 0.5
MAX_STALL_SECONDS = 0.1
TICK = 0.01


def burn_cpu(seconds: float) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


async def longest_stall(work):
    """Run ``work()`` and return its result and the longest time the loop stood still.

    A task sleeps ``TICK`` in a loop and notes how late it wakes up; while the loop is
    blocked nothing runs, so the lateness is the length of the stall.
    """
    stalls: list[float] = []
    running = True

    async def ticker() -> None:
        while running:
            started = time.perf_counter()
            await asyncio.sleep(TICK)
            stalls.append(time.perf_counter() - started - TICK)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(5 * TICK)
    stalls.clear()
    result = await work()
    # A stall is only noticed when the ticker wakes up again.
    await asyncio.sleep(3 * TICK)
    running = False
    await task
    return result, max(stalls, default=0.0)


@pytest.mark.asyncio
async def test_library_runs_only_on_the_flow_worker(hass, accounts):
    _created, factory_state = accounts
    factory_state["final_state"] = LoginState.REQUIRE_2FA
    flow = make_flow(hass)

    await flow.async_step_acc_login(login_input())
    await flow.async_step_acc_2fa_request({"method_id": "0"})
    result = await flow.async_step_acc_2fa_submit({"code": CODE})

    assert result["type"] == "create_entry"
    calls = factory_state["calls"]
    assert {name for name, _thread in calls} == LIBRARY_CALLS
    assert {thread for _name, thread in calls} == {WORKER_THREAD}
    assert not login_threads(), "the worker stops once the entry is created"


@pytest.mark.asyncio
async def test_login_does_not_block_the_event_loop(hass, accounts):
    _created, factory_state = accounts

    def burning_login(account):
        login = account.login.side_effect

        async def burn_then_login(email, password):
            burn_cpu(BURN_SECONDS)
            return await login(email, password)

        account.login.side_effect = burn_then_login

    factory_state["customize"] = burning_login

    async def burn_on_loop():
        burn_cpu(BURN_SECONDS)

    _result, blocked = await longest_stall(burn_on_loop)
    flow = make_flow(hass)
    result, stall = await longest_stall(lambda: flow.async_step_acc_login(login_input()))

    # Run with -s to see the numbers.
    print(f"loop blocked {blocked * 1000:.0f} ms on the loop, {stall * 1000:.0f} ms via the flow")  # noqa: T201
    assert blocked >= 0.8 * BURN_SECONDS, "the probe must notice a blocked loop"
    assert result["type"] == "create_entry"
    assert stall < MAX_STALL_SECONDS


@pytest.mark.asyncio
async def test_worker_starts_on_the_first_sign_in_attempt(hass, accounts, keypair_factory):
    flow = make_flow(hass)
    await flow.async_step_user()
    await flow.async_step_start_acc()
    result = await flow.async_step_acc_login(login_input(anisette_url="ftp://ani.example"))
    assert result["errors"] == {"base": "invalid_anisette_url"}
    assert not login_threads()

    device = make_flow(hass)
    key = keypair_factory().private_key_b64
    result = await device.async_step_dev_static({"name": "Tag", "private_key": key})
    assert result["type"] == "create_entry"
    assert not login_threads(), "device flows never need a sign-in thread"


@pytest.mark.asyncio
async def test_wrong_passwords_reuse_one_worker(hass, accounts):
    created, factory_state = accounts
    factory_state["login_error"] = InvalidCredentialsError("wrong password")
    flow = make_flow(hass)

    threads: set[threading.Thread] = set()
    for _ in range(3):
        result = await flow.async_step_acc_login(login_input())
        assert result["errors"] == {"base": "invalid_auth"}
        threads.update(login_threads())

    assert len(threads) == 1
    for account in created:
        account.close.assert_awaited_once()
    # The user gives up on the login form.
    flow.async_remove()
    await hass.async_block_till_done()
    assert not login_threads()


@pytest.mark.asyncio
async def test_cancel_during_2fa_stops_worker_without_blocking(hass, accounts):
    flow, account = await _flow_at_2fa_request(hass, accounts)
    closed = threading.Event()

    async def slow_close():
        await asyncio.sleep(0.3)
        closed.set()

    account.close.side_effect = slow_close

    started = time.perf_counter()
    flow.async_remove()
    assert time.perf_counter() - started < 0.05, "async_remove must not wait for the thread"
    assert not closed.is_set()

    await hass.async_block_till_done()
    assert closed.is_set()
    assert not login_threads()

    # A removed flow never starts another thread.
    result = await flow.async_step_acc_login(login_input())
    assert result["errors"] == {"base": "login_incomplete"}
    assert not login_threads()


@pytest.mark.asyncio
async def test_home_assistant_stop_ends_an_idle_flow(hass, accounts):
    flow, account = await _flow_at_2fa_request(hass, accounts)
    assert login_threads()

    await hass.async_stop(force=True)

    assert not login_threads()
    account.close.assert_awaited_once()
    flow.async_remove()


@pytest.mark.asyncio
async def test_home_assistant_stop_during_login_is_a_form_error(hass, accounts):
    created, factory_state = accounts
    in_login = threading.Event()

    def hanging_login(account):
        async def login(_email, _password):
            in_login.set()
            await asyncio.sleep(30)

        account.login.side_effect = login

    factory_state["customize"] = hanging_login
    flow = make_flow(hass)
    step = asyncio.create_task(flow.async_step_acc_login(login_input()))
    async with asyncio.timeout(5):
        while not in_login.is_set():  # noqa: ASYNC110
            await asyncio.sleep(0.01)

    await hass.async_stop(force=True)
    # The step must not wait for the hanging login to finish.
    async with asyncio.timeout(5):
        result = await step

    assert result["step_id"] == "acc_login"
    assert result["errors"] == {"base": "login_incomplete"}
    created[0].close.assert_awaited_once()
    assert not login_threads()


@pytest.mark.asyncio
async def test_login_timeout_is_retryable(hass, accounts, monkeypatch):
    created, factory_state = accounts
    monkeypatch.setattr(config_flow, "_LOGIN_TIMEOUT", 0.05)

    def slow_login(account):
        async def login(_email, _password):
            await asyncio.sleep(5)

        account.login.side_effect = login

    factory_state["customize"] = slow_login
    flow = make_flow(hass)

    result = await flow.async_step_acc_login(login_input())
    assert result["errors"] == {"base": "cannot_connect"}
    created[0].close.assert_awaited_once()
    assert len(login_threads()) == 1, "the worker stays for the next attempt"

    factory_state["customize"] = lambda _account: None
    result = await flow.async_step_acc_login(login_input())
    assert result["type"] == "create_entry"
    assert not login_threads()


@pytest.mark.asyncio
async def test_unexpected_error_stops_worker(hass, accounts):
    created, factory_state = accounts
    factory_state["login_error"] = RuntimeError("a bug")

    with pytest.raises(RuntimeError, match="a bug"):
        await make_flow(hass).async_step_acc_login(login_input())

    created[0].close.assert_awaited_once()
    assert not login_threads()


@pytest.mark.asyncio
async def test_abort_stops_worker(hass, accounts):
    flow, account = await _flow_at_2fa_request(hass, accounts)

    result = await flow.async_step_acc_2fa_request(None)

    assert result["type"] == "abort"
    account.close.assert_awaited_once()
    assert not login_threads()


@pytest.mark.asyncio
async def test_reauth_stops_worker(hass, accounts):
    entry = add_account_entry(hass)
    flow = make_flow(hass, config_entries.SOURCE_REAUTH, entry_id=entry.entry_id)

    result = await flow.async_step_reauth_confirm(login_input())

    assert result["reason"] == "reauth_successful"
    assert not login_threads()


##########
### S1 ###
##########


def _children(obj) -> list[object]:
    """What the secret search follows from ``obj``; Home Assistant itself is skipped."""
    if isinstance(obj, dict | MappingProxyType):
        return [part for item in obj.items() for part in item]
    if isinstance(obj, list | tuple | set | frozenset):
        return list(obj)
    module = type(obj).__module__
    if module.startswith(("custom_components.", "unittest.mock")) and hasattr(obj, "__dict__"):
        return [vars(obj)]
    return []


def contains_secret(obj, secret: str, seen: set[int] | None = None) -> bool:
    """Whether ``secret`` is reachable from ``obj``.

    Follows containers and the attributes of the integration's own objects, the
    worker and mocks (a mock remembers the arguments it was called with).
    """
    seen = set() if seen is None else seen
    if id(obj) in seen or isinstance(obj, HomeAssistant):
        return False
    seen.add(id(obj))
    if isinstance(obj, str):
        return secret in obj
    if isinstance(obj, bytes):
        return secret.encode() in obj
    return any(contains_secret(child, secret, seen) for child in _children(obj))


def assert_not_logged(caplog, *secrets: str) -> None:
    """No secret in any record at any level, including arguments and tracebacks."""
    formatter = logging.Formatter()
    assert caplog.records, "the test must actually capture log records"
    for record in caplog.records:
        parts = [record.getMessage(), repr(record.args)]
        if record.exc_info:
            parts.append(formatter.formatException(record.exc_info))
        if record.stack_info:
            parts.append(record.stack_info)
        for secret in secrets:
            assert all(secret not in part for part in parts), record
    for secret in secrets:
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_password_is_not_stored_or_kept(hass, accounts, caplog):
    caplog.set_level(logging.DEBUG)
    _created, factory_state = accounts
    factory_state["final_state"] = LoginState.REQUIRE_2FA
    flow = make_flow(hass)

    factory_state["login_error"] = InvalidCredentialsError("wrong password")
    await flow.async_step_acc_login(login_input())
    factory_state["login_error"] = None
    await flow.async_step_acc_login(login_input())
    await flow.async_step_acc_2fa_request({"method_id": "0"})
    # While signing in, the account knows the password; the search must find it.
    assert contains_secret(flow, PASSWORD), "the search must reach the flow's account"
    result = await flow.async_step_acc_2fa_submit({"code": CODE})

    assert result["type"] == "create_entry"
    assert result["data"]["account_data"] == STORED_ACCOUNT_JSON
    assert result["data"]["account_data"]["account"]["password"] is None
    assert not contains_secret(result, PASSWORD)
    assert ACCOUNT_JSON["account"]["password"] == PASSWORD, "the library's data is not changed"
    flow.async_remove()
    await hass.async_block_till_done()
    assert not contains_secret(flow, PASSWORD)
    assert_not_logged(caplog, PASSWORD)


@pytest.mark.asyncio
@pytest.mark.usefixtures("accounts")
async def test_reauth_does_not_store_password(hass, caplog):
    caplog.set_level(logging.DEBUG)
    entry = add_account_entry(hass)
    flow = make_flow(hass, config_entries.SOURCE_REAUTH, entry_id=entry.entry_id)
    update = MagicMock(wraps=flow.async_update_reload_and_abort)
    flow.async_update_reload_and_abort = update

    result = await flow.async_step_reauth_confirm(login_input())

    assert result["reason"] == "reauth_successful"
    update.assert_called_once()
    assert update.call_args.kwargs["data"]["account_data"] == STORED_ACCOUNT_JSON
    assert not contains_secret(update.call_args, PASSWORD)
    assert not contains_secret(dict(entry.data), PASSWORD)
    del flow.async_update_reload_and_abort
    assert not contains_secret(flow, PASSWORD)
    assert_not_logged(caplog, PASSWORD)


####################
### Translations ###
####################

_COMPONENT = Path(config_flow.__file__).parent
_ABORT_KEY = re.compile(r'(?:_async_abort\(|reason=)"([a-z0-9_]+)"')
_ERROR_KEY = re.compile(
    r'(?:return |"base": |_async_restart_login\(|error or |error = )"([a-z0-9_]+)"',
)


def test_flow_error_keys_match_translations():
    """Hassfest rejects keys without a translation, and stale translations confuse."""
    source = (_COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    strings = json.loads((_COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
    config = strings["config"]

    errors = set(_ERROR_KEY.findall(source))
    aborts = set(_ABORT_KEY.findall(source))

    assert "invalid_auth" in errors, "the pattern must find the error keys"
    assert errors == set(config["error"])
    assert aborts <= set(config["abort"])


###########
### R22 ###
###########

# None of this could be checked against Apple: the error is modelled on the reports
# about Apple's sign-in change in October 2026 (FindMy.py issue #278).
MISSING_TOKEN = KeyError("searchPartyToken")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [config_entries.SOURCE_USER, config_entries.SOURCE_REAUTH])
async def test_missing_find_my_token_explains_itself(hass, accounts, source):
    created, factory_state = accounts
    factory_state["login_error"] = MISSING_TOKEN
    if source == config_entries.SOURCE_REAUTH:
        entry = add_account_entry(hass)
        flow = make_flow(hass, source, entry_id=entry.entry_id)
        result = await flow.async_step_reauth_confirm(login_input())
    else:
        result = await make_flow(hass).async_step_acc_login(login_input())

    assert result["errors"] == {"base": "find_my_token_missing"}
    placeholders = result.get("description_placeholders") or {}
    assert placeholders["issue_tracker_url"] == "https://github.com/malmeloo/FindMy.py/issues"
    created[0].close.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_find_my_token_after_2fa_returns_to_login(hass, accounts):
    flow, account = await _flow_at_2fa_request(hass, accounts)
    await flow.async_step_acc_2fa_request({"method_id": "0"})

    async def submit(_code):
        account.login_state = LoginState.AUTHENTICATED
        raise MISSING_TOKEN

    account.sms.submit.side_effect = submit
    result = await flow.async_step_acc_2fa_submit({"code": CODE})

    assert result["step_id"] == "acc_login"
    assert result["errors"] == {"base": "find_my_token_missing"}
    account.close.assert_awaited_once()
    account.to_json.assert_not_called()


@pytest.mark.asyncio
async def test_other_key_errors_are_not_mistaken_for_the_token(hass, accounts):
    created, factory_state = accounts
    factory_state["login_error"] = KeyError("dsid")

    with pytest.raises(KeyError, match="dsid"):
        await make_flow(hass).async_step_acc_login(login_input())

    created[0].close.assert_awaited_once()
    assert not login_threads()


@pytest.mark.asyncio
async def test_no_entry_for_a_session_without_find_my_token(hass, accounts):
    _created, factory_state = accounts
    without_token = {
        **ACCOUNT_JSON,
        "login": {"state": LoginState.LOGGED_IN.value, "data": {"mobileme_data": {"tokens": {}}}},
    }

    def lacking_token(account):
        account.to_json.side_effect = lambda: without_token

    factory_state["customize"] = lacking_token
    flow = make_flow(hass)

    result = await flow.async_step_acc_login(login_input())

    assert result["type"] == "form"
    assert result["errors"] == {"base": "find_my_token_missing"}
    # A later attempt that gets the token still succeeds.
    factory_state["customize"] = lambda _account: None
    result = await flow.async_step_acc_login(login_input())
    assert result["type"] == "create_entry"


################
### H3 / G7  ###
################

# What the entry of an account holds before it is re-authenticated.
OLD_UID = "0A1B2C3D-UID-OF-THE-ENTRY"
OLD_DEVID = "4E5F6A7B-DEVID-OF-THE-ENTRY"
OLD_TOKEN = "OLD-SEARCH-PARTY-TOKEN"
NEW_TOKEN = "NEW-SEARCH-PARTY-TOKEN"
# The provisioning state is an opaque archive to FindMy.py until the provider is used.
PROV_DATA = base64.b64encode(b"PROVISIONING-STATE-OF-THE-ENTRY").decode()
LOCAL_ANISETTE = {"type": "aniLocal", "prov_data": PROV_DATA}
REMOTE_URL = "https://ani-user:s3cret-ani@ani.example:6969/v3"
REMOTE_ANISETTE = {"type": "aniRemote", "url": REMOTE_URL}


def stored_account(anisette: dict | None = None) -> dict:
    """Account state as an entry stores it: signed in, without a password."""
    return {
        "type": "account",
        "ids": {"uid": OLD_UID, "devid": OLD_DEVID},
        "account": {
            "username": EMAIL,
            "password": None,
            "info": {
                "account_name": EMAIL,
                "first_name": "John",
                "last_name": "Secret",
                "trusted_device_2fa": False,
            },
        },
        "login": {
            "state": LoginState.LOGGED_IN.value,
            "data": {"dsid": "1", "mobileme_data": {"tokens": {"searchPartyToken": OLD_TOKEN}}},
        },
        "anisette": copy.deepcopy(LOCAL_ANISETTE if anisette is None else anisette),
    }


@pytest.fixture
def library(monkeypatch):
    """FindMy.py for real, except for the two requests a sign-in sends to Apple.

    Accounts, providers, ``from_json``, ``to_json`` and the state check of ``login`` are the
    library's own. The returned class lists what happened: ``events`` (name, thread),
    ``restores`` (the arguments of ``from_json``) and ``instances``.
    """

    class Account(AsyncAppleAccount):
        events: list[tuple[str, str]] = []  # noqa: RUF012  # one class per test
        restores: list[dict] = []  # noqa: RUF012
        instances: list[AsyncAppleAccount] = []  # noqa: RUF012
        login_error: BaseException | None = None

        def __init__(self, anisette, *, state_info=None):
            _record(self.events, "create")
            super().__init__(anisette, state_info=state_info)
            # `login` only accepts an account that is logged out.
            self.state_when_created = self.login_state
            self.instances.append(self)

        @classmethod
        def from_json(cls, val, /, *, anisette_libs_path=None):
            _record(cls.events, "from_json")
            cls.restores.append({"data": copy.deepcopy(val), "libs_path": anisette_libs_path})
            return super().from_json(val, anisette_libs_path=anisette_libs_path)

        async def _gsa_authenticate(self, username=None, password=None):
            _record(self.events, "authenticate")
            if self.login_error is not None:
                raise self.login_error
            self._username, self._password = username, password
            self._account_info = {
                "account_name": username,
                "first_name": "John",
                "last_name": "Secret",
                "trusted_device_2fa": False,
            }
            return self._set_login_state(LoginState.AUTHENTICATED, {"idms_pet": "pet"})

        async def _login_mobileme(self):
            return self._set_login_state(
                LoginState.LOGGED_IN,
                {"dsid": "2", "mobileme_data": {"tokens": {"searchPartyToken": NEW_TOKEN}}},
            )

        def to_json(self, path=None, /):
            _record(self.events, "to_json")
            return super().to_json(path)

        async def close(self):
            _record(self.events, "close")
            await super().close()

    monkeypatch.setattr(config_flow, "AsyncAppleAccount", Account)
    return Account


def reauth_flow(hass, entry):
    return make_flow(hass, config_entries.SOURCE_REAUTH, entry_id=entry.entry_id)


def suggested_anisette_url(result) -> str | None:
    """The Anisette URL a login form starts with."""
    advanced = result["data_schema"].schema["advanced_options"]
    for key in advanced.schema.schema:
        if key == "anisette_url":
            return (key.description or {}).get("suggested_value")
    msg = "the form has no field for the Anisette URL"
    raise AssertionError(msg)


@pytest.mark.asyncio
async def test_reauth_keeps_device_ids_and_local_provisioning(hass, library, libs):
    """H3: Apple sees the device it already knows, not a new one."""
    entry = add_account_entry(hass, account_data=stored_account())

    result = await reauth_flow(hass, entry).async_step_reauth_confirm(login_input())

    assert result["reason"] == "reauth_successful"
    data = entry.data["account_data"]
    assert data["ids"] == {"uid": OLD_UID, "devid": OLD_DEVID}
    # The provider was never used here, so the library hands back the state it was given.
    assert data["anisette"] == LOCAL_ANISETTE
    # The session is the new one, and it is complete.
    assert data["login"]["state"] == LoginState.LOGGED_IN.value
    assert data["login"]["data"]["mobileme_data"]["tokens"]["searchPartyToken"] == NEW_TOKEN
    assert data["account"]["password"] is None
    # Restored once, logged out, from nothing but the identity, and with the library bundle.
    (restore,) = library.restores
    assert restore["libs_path"] == libs.path
    assert restore["data"] == {
        "type": "account",
        "ids": {"uid": OLD_UID, "devid": OLD_DEVID},
        "account": {"username": None, "password": None, "info": None},
        "login": {"state": LoginState.LOGGED_OUT.value, "data": {}},
        "anisette": LOCAL_ANISETTE,
    }
    (account,) = library.instances
    assert account.state_when_created is LoginState.LOGGED_OUT
    assert isinstance(account._anisette, LocalAnisetteProvider)
    assert account._anisette._libs_path == libs.path


@pytest.mark.asyncio
async def test_reauth_runs_the_library_on_the_flow_worker_only(hass, library):
    entry = add_account_entry(hass, account_data=stored_account())

    result = await reauth_flow(hass, entry).async_step_reauth_confirm(login_input())

    assert result["reason"] == "reauth_successful"
    assert [name for name, _thread in library.events] == [
        "from_json",
        "create",
        "authenticate",
        "to_json",
        "close",
    ]
    assert {thread for _name, thread in library.events} == {WORKER_THREAD}
    assert not login_threads()


@pytest.mark.asyncio
async def test_reauth_form_starts_with_the_remote_anisette_url(hass, library):
    """G7: submitting the form as it is must not move the account to another provider."""
    entry = add_account_entry(hass, account_data=stored_account(REMOTE_ANISETTE))

    result = await reauth_flow(hass, entry).async_step_reauth(dict(entry.data))

    assert result["step_id"] == "reauth_confirm"
    assert suggested_anisette_url(result) == REMOTE_URL
    # Also when the form comes back with an error.
    library.login_error = InvalidCredentialsError("wrong password")
    result = await reauth_flow(hass, entry).async_step_reauth_confirm(
        login_input(anisette_url=REMOTE_URL)
    )
    assert result["errors"] == {"base": "invalid_auth"}
    assert suggested_anisette_url(result) == REMOTE_URL


@pytest.mark.asyncio
@pytest.mark.usefixtures("library")
@pytest.mark.parametrize("account_data", [stored_account(), {"old": True}, "damaged"])
async def test_login_forms_without_a_remote_server_start_empty(hass, account_data):
    entry = add_account_entry(hass, account_data=account_data)

    result = await reauth_flow(hass, entry).async_step_reauth(dict(entry.data))
    assert suggested_anisette_url(result) is None

    result = await make_flow(hass).async_step_start_acc()
    assert suggested_anisette_url(result) is None


@pytest.mark.asyncio
async def test_reauth_keeps_the_remote_anisette_server(hass, library, libs):
    """The URL survives, together with an option that only exists in the stored data."""
    anisette = {**REMOTE_ANISETTE, "allow_unverified_https": True}
    entry = add_account_entry(hass, account_data=stored_account(anisette))

    result = await reauth_flow(hass, entry).async_step_reauth_confirm(
        login_input(anisette_url=REMOTE_URL)
    )

    assert result["reason"] == "reauth_successful"
    data = entry.data["account_data"]
    assert data["anisette"] == anisette
    assert data["ids"] == {"uid": OLD_UID, "devid": OLD_DEVID}
    (account,) = library.instances
    assert isinstance(account._anisette, RemoteAnisetteProvider)
    # A remote server needs no library bundle.
    assert libs.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "submitted_url", "expected"),
    [
        pytest.param(
            REMOTE_ANISETTE,
            "http://other.example:6969",
            {"type": "aniRemote", "url": "http://other.example:6969"},
            id="another-server",
        ),
        pytest.param(
            LOCAL_ANISETTE,
            "http://other.example:6969",
            {"type": "aniRemote", "url": "http://other.example:6969"},
            id="integrated-to-remote",
        ),
        pytest.param(
            REMOTE_ANISETTE,
            None,
            # A provider that has not been used yet has no provisioning state.
            {"type": "aniLocal", "prov_data": None},
            id="remote-to-integrated",
        ),
    ],
)
async def test_reauth_with_another_provider_keeps_the_device_ids(
    hass, library, libs, stored, submitted_url, expected
):
    entry = add_account_entry(hass, account_data=stored_account(stored))

    result = await reauth_flow(hass, entry).async_step_reauth_confirm(
        login_input(anisette_url=submitted_url)
    )

    assert result["reason"] == "reauth_successful"
    data = entry.data["account_data"]
    assert data["anisette"] == expected
    assert data["ids"] == {"uid": OLD_UID, "devid": OLD_DEVID}
    assert libs.calls == (1 if submitted_url is None else 0)
    assert len(library.restores) == 1


@pytest.mark.asyncio
async def test_every_reauth_attempt_starts_from_the_stored_identity(hass, library):
    """A wrong password must not cost the identity: the next attempt restores it again."""
    entry = add_account_entry(hass, account_data=stored_account())
    flow = reauth_flow(hass, entry)

    library.login_error = InvalidCredentialsError("wrong password")
    result = await flow.async_step_reauth_confirm(login_input(password="wrong"))
    assert result["errors"] == {"base": "invalid_auth"}
    library.login_error = None
    result = await flow.async_step_reauth_confirm(login_input())

    assert result["reason"] == "reauth_successful"
    assert [restore["data"]["ids"] for restore in library.restores] == [
        {"uid": OLD_UID, "devid": OLD_DEVID}
    ] * 2
    assert entry.data["account_data"]["ids"] == {"uid": OLD_UID, "devid": OLD_DEVID}
    assert entry.data["account_data"]["anisette"] == LOCAL_ANISETTE
    first, second = library.instances
    assert first is not second
    assert [name for name, _thread in library.events].count("close") == 2


def _without(key: str) -> dict:
    data = stored_account()
    del data[key]
    return data


def _with_ids(ids: object) -> dict:
    return {**stored_account(), "ids": ids}


UNUSABLE_IDENTITIES = [
    pytest.param({"old": True}, id="unrelated-data"),
    pytest.param("damaged", id="not-a-mapping"),
    pytest.param(_without("ids"), id="no-ids"),
    pytest.param(_with_ids("uid"), id="ids-not-a-mapping"),
    pytest.param(_with_ids({"uid": OLD_UID}), id="no-devid"),
    pytest.param(_with_ids({"uid": "", "devid": OLD_DEVID}), id="empty-uid"),
    pytest.param(_with_ids({"uid": 1, "devid": 2}), id="ids-not-text"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("account_data", UNUSABLE_IDENTITIES)
async def test_reauth_without_usable_ids_signs_in_as_a_new_device(
    hass, library, caplog, account_data
):
    entry = add_account_entry(hass, account_data=account_data)
    caplog.set_level(logging.INFO)

    result = await reauth_flow(hass, entry).async_step_reauth_confirm(login_input())

    assert result["reason"] == "reauth_successful"
    assert not library.restores
    ids = entry.data["account_data"]["ids"]
    assert ids["uid"] not in {OLD_UID, ""}
    assert ids["devid"] not in {OLD_DEVID, ""}
    assert entry.data["account_data"]["anisette"] == {"type": "aniLocal", "prov_data": None}
    messages = [r.getMessage() for r in caplog.records if r.name == config_flow.__name__]
    assert any("signing in as a new device" in message for message in messages)
    assert_not_logged(caplog, PASSWORD, OLD_UID, OLD_DEVID, PROV_DATA, OLD_TOKEN)


@pytest.mark.asyncio
async def test_reauth_with_unreadable_provisioning_state_signs_in_as_a_new_device(
    hass, library, caplog
):
    """The ids look fine, but the library cannot restore the provider."""
    broken = "not-base64!"
    entry = add_account_entry(
        hass, account_data=stored_account({"type": "aniLocal", "prov_data": broken})
    )
    caplog.set_level(logging.INFO)

    result = await reauth_flow(hass, entry).async_step_reauth_confirm(login_input())

    assert result["reason"] == "reauth_successful"
    assert len(library.restores) == 1
    data = entry.data["account_data"]
    assert data["ids"]["uid"] != OLD_UID
    assert data["ids"]["devid"] != OLD_DEVID
    assert data["anisette"] == {"type": "aniLocal", "prov_data": None}
    assert "cannot be restored" in caplog.text
    assert "signing in as a new device" in caplog.text
    assert_not_logged(caplog, PASSWORD, OLD_UID, OLD_DEVID, broken)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "anisette",
    [
        pytest.param({"type": "aniOther"}, id="unknown-provider"),
        pytest.param({"type": "aniRemote"}, id="remote-without-url"),
        pytest.param({"type": "aniLocal", "prov_data": 42}, id="state-not-text"),
        pytest.param("damaged", id="not-a-mapping"),
    ],
)
async def test_reauth_with_unusable_provider_keeps_the_ids(hass, library, anisette):
    """The provider the form asks for is created anew; the ids are still the known ones."""
    account_data = {**stored_account(), "anisette": anisette}
    entry = add_account_entry(hass, account_data=account_data)

    result = await reauth_flow(hass, entry).async_step_reauth_confirm(login_input())

    assert result["reason"] == "reauth_successful"
    data = entry.data["account_data"]
    assert data["ids"] == {"uid": OLD_UID, "devid": OLD_DEVID}
    assert data["anisette"] == {"type": "aniLocal", "prov_data": None}


@pytest.mark.asyncio
async def test_initial_setup_always_creates_a_new_identity(hass, library, libs):
    first = await make_flow(hass).async_step_acc_login(login_input())
    second = await make_flow(hass).async_step_acc_login(login_input(email="other@example.com"))

    assert first["type"] == second["type"] == "create_entry"
    assert not library.restores
    ids = [result["data"]["account_data"]["ids"] for result in (first, second)]
    assert ids[0]["uid"] != ids[1]["uid"]
    assert ids[0]["devid"] != ids[1]["devid"]
    for account in library.instances:
        assert isinstance(account._anisette, LocalAnisetteProvider)
        assert account._anisette._libs_path == libs.path


@pytest.mark.asyncio
@pytest.mark.parametrize("anisette", [LOCAL_ANISETTE, REMOTE_ANISETTE])
async def test_reauth_logs_nothing_secret(hass, library, caplog, anisette):
    """Neither the identity nor the credentials of a private Anisette server reach a log."""
    entry = add_account_entry(hass, account_data=stored_account(anisette))
    caplog.set_level(logging.DEBUG)
    url = anisette.get("url")
    flow = reauth_flow(hass, entry)

    await flow.async_step_reauth(dict(entry.data))
    library.login_error = aiohttp.ClientConnectionError(f"Cannot connect to host {REMOTE_URL}")
    result = await flow.async_step_reauth_confirm(login_input(anisette_url=url))
    assert result["errors"] == {"base": "cannot_connect"}
    library.login_error = None
    result = await flow.async_step_reauth_confirm(login_input(anisette_url=url))

    assert result["reason"] == "reauth_successful"
    assert "Step: reauth_confirm" in caplog.text, "the test must capture the flow's log"
    assert_not_logged(
        caplog,
        PASSWORD,
        "s3cret-ani",
        "ani-user",
        OLD_UID,
        OLD_DEVID,
        PROV_DATA,
        OLD_TOKEN,
        NEW_TOKEN,
        EMAIL,
    )


def test_reusable_identity_never_shares_data_with_the_entry():
    """The entry's data is read-only and stays on Home Assistant's side."""
    stored = stored_account({**REMOTE_ANISETTE, "allow_unverified_https": True})
    before = copy.deepcopy(stored)

    identity = config_flow._reusable_identity(stored, REMOTE_URL)

    assert identity is not None
    assert identity["anisette"] == stored["anisette"]
    assert identity["anisette"] is not stored["anisette"]
    # Changing the copy must leave the entry's data alone.
    cast("dict[str, object]", identity["anisette"])["url"] = "changed"
    identity["ids"]["uid"] = "changed"
    assert stored == before


###########
### M3  ###
###########


@pytest.mark.asyncio
async def test_integrated_anisette_gets_the_library_bundle(hass, accounts, libs):
    """Without the path the anisette package downloads its libraries on first use."""
    _created, factory_state = accounts

    result = await make_flow(hass).async_step_acc_login(login_input())

    assert result["type"] == "create_entry"
    assert libs.calls == 1
    factory_state["LocalAnisetteProvider"].assert_called_once_with(libs_path=libs.path)


@pytest.mark.asyncio
async def test_remote_anisette_needs_no_library_bundle(hass, accounts, libs):
    _created, factory_state = accounts

    result = await make_flow(hass).async_step_acc_login(
        login_input(anisette_url="http://ani.example:6969")
    )

    assert result["type"] == "create_entry"
    assert libs.calls == 0
    factory_state["RemoteAnisetteProvider"].assert_called_once_with("http://ani.example:6969")
    factory_state["LocalAnisetteProvider"].assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [config_entries.SOURCE_USER, config_entries.SOURCE_REAUTH])
async def test_changed_library_bundle_stops_the_sign_in(hass, accounts, libs, source, caplog):
    """The bundle is not used, and the form says which file to delete."""
    created, _factory_state = accounts
    entry = add_account_entry(hass)
    context = {"entry_id": entry.entry_id} if source == config_entries.SOURCE_REAUTH else {}
    flow = make_flow(hass, source, **context)
    step = (
        flow.async_step_reauth_confirm
        if source == config_entries.SOURCE_REAUTH
        else flow.async_step_acc_login
    )
    email = EMAIL if source == config_entries.SOURCE_REAUTH else "new@example.com"
    libs.error = AnisetteLibsChecksumError(libs.path, "a" * 64, "b" * 64, "c" * 64)

    with caplog.at_level(logging.DEBUG):
        result = await step(login_input(email=email))

    assert result["type"] == "form"
    assert result["errors"] == {"base": "anisette_libs_changed"}
    placeholders = result.get("description_placeholders") or {}
    assert placeholders["anisette_libs_path"] == str(libs.path)
    assert not created, "no account may be created without a verified bundle"
    assert not login_threads(), "nothing was signed in, so no thread is needed"
    # The log names the file too, for those who do not see the form.
    assert str(libs.path) in caplog.text
    assert_not_logged(caplog, PASSWORD)

    # After the user dealt with the file, the same form signs in.
    libs.error = None
    result = await step(login_input(email=email))
    assert result["type"] in {"create_entry", "abort"}
    assert len(created) == 1


@pytest.mark.asyncio
async def test_failed_bundle_download_is_a_retryable_form_error(hass, accounts, libs):
    created, _factory_state = accounts
    flow = make_flow(hass)
    libs.error = AnisetteLibsUnavailableError("the download did not finish within 60 s")

    result = await flow.async_step_acc_login(login_input())

    assert result["errors"] == {"base": "anisette_libs_unavailable"}
    assert not created
    assert not login_threads()

    libs.error = None
    result = await flow.async_step_acc_login(login_input())
    assert result["type"] == "create_entry"
    assert libs.calls == 2


@pytest.mark.asyncio
async def test_sign_in_stores_the_bundle_and_later_sign_ins_reuse_it(hass, library, monkeypatch):
    """The flow with the real bundle store; only the download is a fake."""
    download = FakeDownload(make_library_archive())
    monkeypatch.setattr(anisette_libs, "async_get_clientsession", lambda _hass: download)
    monkeypatch.setattr(
        config_flow, "async_ensure_anisette_libs", anisette_libs.async_ensure_anisette_libs
    )
    path = anisette_libs_path(hass)

    result = await make_flow(hass).async_step_acc_login(login_input())
    assert result["type"] == "create_entry"
    assert path.is_file()
    stored = path.read_bytes()

    entry = add_account_entry(hass, account_data=result["data"]["account_data"])
    result = await reauth_flow(hass, entry).async_step_reauth_confirm(login_input())

    assert result["reason"] == "reauth_successful"
    assert len(download.requests) == 1
    assert path.read_bytes() == stored
    assert [account._anisette._libs_path for account in library.instances] == [path, path]


def test_error_texts_explain_the_library_bundle():
    strings = json.loads((_COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
    errors = strings["config"]["error"]

    assert "{anisette_libs_path}" in errors["anisette_libs_changed"]
    assert "delete" in errors["anisette_libs_changed"]
    assert "try again" in errors["anisette_libs_unavailable"]


####################################
### Rejected sign-in (first real) ###
####################################

# The first sign-in on a real system was turned away by Apple at the first request, with the
# right password, and the flow said "Invalid username or password" without a word of Apple's.


@pytest.mark.asyncio
async def test_what_apple_says_about_a_rejected_sign_in_is_logged_and_shown(hass, accounts, caplog):
    _created, factory_state = accounts
    said = f"Email verification failed: This Apple Account ({EMAIL.upper()}) is locked."
    factory_state["login_error"] = InvalidCredentialsError(said)
    flow = make_flow(hass)

    with caplog.at_level(logging.WARNING, logger="custom_components.findmy"):
        result = await flow.async_step_acc_login(login_input())

    assert result["errors"] == {"base": "invalid_auth"}
    shown = (result.get("description_placeholders") or {}).get("apple_message")
    assert shown == "Email verification failed: This Apple Account (j***@example.com) is locked."
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warning == f"Apple rejected the sign-in during login: {shown}"
    assert EMAIL.lower() not in caplog.text.lower()
    assert PASSWORD not in caplog.text

    # The next attempt does not show what was said about the previous one.
    factory_state["login_error"] = aiohttp.ClientConnectionError("no route")
    result = await flow.async_step_acc_login(login_input())
    assert result["errors"] == {"base": "cannot_connect"}
    placeholders = result.get("description_placeholders") or {}
    assert placeholders.get("apple_message") == "no details"


def test_text_of_a_rejection_is_tamed_before_it_is_logged_or_shown():
    reason = config_flow._rejection_reason
    assert reason(InvalidCredentialsError("  two\n lines\tand a \x1b[31m colour "), None) == (
        "two lines and a ?[31m colour"
    )
    assert reason(InvalidCredentialsError(""), EMAIL) == "no details"
    long = reason(InvalidCredentialsError("x" * 1000), None)
    assert long == "x" * 300 + "..."


def test_error_text_for_a_rejection_has_its_placeholder():
    strings = json.loads((_COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
    assert "{apple_message}" in strings["config"]["error"]["invalid_auth"]


@pytest.mark.asyncio
async def test_white_space_around_the_address_is_not_sent_to_apple(hass, accounts):
    created, _factory_state = accounts
    flow = make_flow(hass)

    result = await flow.async_step_acc_login(login_input(email=f"  {EMAIL}\t"))

    assert result["type"] == "create_entry"
    created[0].login.assert_awaited_once_with(EMAIL, PASSWORD)
    assert flow.unique_id == EMAIL


###########################################
### Failed Anisette provisioning (real) ###
###########################################

# The second sign-in attempt on a real system ended inside the anisette package, before any
# sign-in request: Apple's answer to the request that finishes the provisioning of the
# emulated device had no `ptm`, and the flow showed "Unknown error".


def provisioning_failure(finish_answer: dict[str, object]) -> Exception:
    """Run the real provisioning code of the anisette package against prepared answers."""
    from anisette._session import ProvisioningSession  # noqa: PLC0415

    answers = [
        {"Response": {"spim": base64.b64encode(b"start").decode()}},
        {"Response": finish_answer},
    ]
    session = ProvisioningSession.__new__(ProvisioningSession)
    session._ProvisioningSession__urlBag = {  # pyright: ignore[reportAttributeAccessIssue]
        "midStartProvisioning": "https://gsa.example/start",
        "midFinishProvisioning": "https://gsa.example/finish",
    }
    session._adi = MagicMock()
    session._adi.start_provisioning.return_value = MagicMock(cpim=b"cpim", session=7)
    session._post = lambda *_args: plistlib.dumps(answers.pop(0))  # pyright: ignore[reportAttributeAccessIssue]
    try:
        session.provision(-2)
    except Exception as err:  # noqa: BLE001
        return err
    msg = "the provisioning was expected to fail"
    raise AssertionError(msg)


def test_failed_provisioning_is_described_with_the_status_apple_sent():
    err = provisioning_failure(
        {"Status": {"ec": -80045, "em": "This action cannot be completed\nat this time."}}
    )
    assert isinstance(err, KeyError)

    described = config_flow._anisette_failure(err)

    assert described == (
        "KeyError: 'ptm' in anisette._session.provision; Apple answered with error -80045: "
        "This action cannot be completed at this time."
    )
    assert config_flow._is_flow_error(err)


def test_failed_provisioning_without_a_status_lists_the_fields_but_no_values():
    err = provisioning_failure({"tk": base64.b64encode(b"TRUST-KEY-SECRET").decode(), "x": 1})

    described = config_flow._anisette_failure(err)

    assert described == (
        "KeyError: 'ptm' in anisette._session.provision; Apple's answer holds only: tk, x"
    )
    assert "TRUST" not in (described or "")


def test_other_errors_are_not_taken_for_an_anisette_failure():
    answer: dict[str, str] = {}
    with pytest.raises(KeyError) as raised:
        _ = answer["ptm"]
    # The same error, raised somewhere else: a bug that keeps its traceback.
    assert config_flow._anisette_failure(raised.value) is None
    assert not config_flow._is_flow_error(raised.value)
    assert config_flow._anisette_failure(KeyError("ptm")) is None


@pytest.mark.asyncio
async def test_failed_provisioning_is_a_form_error_with_what_apple_answered(hass, accounts, caplog):
    _created, factory_state = accounts
    factory_state["login_error"] = provisioning_failure(
        {"Status": {"ec": -80045, "em": "This action cannot be completed at this time."}}
    )
    flow = make_flow(hass)

    with caplog.at_level(logging.WARNING, logger="custom_components.findmy"):
        result = await flow.async_step_acc_login(login_input())

    assert result["type"] == "form"
    assert result["errors"] == {"base": "anisette_failed"}
    shown = (result.get("description_placeholders") or {}).get("apple_message")
    assert shown == (
        "KeyError: 'ptm' in anisette._session.provision; Apple answered with error -80045: "
        "This action cannot be completed at this time."
    )
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warning == f"The integrated Anisette provider failed during login: {shown}"
    # A form error, not a traceback.
    assert all(record.exc_info is None for record in caplog.records)


def test_error_text_for_a_failed_provisioning_has_its_placeholder():
    strings = json.loads((_COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
    assert "{apple_message}" in strings["config"]["error"]["anisette_failed"]
