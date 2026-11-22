# pyright: reportUnknownParameterType=false, reportExplicitAny=false, reportUnknownMemberType=false

"""Integration config flow.

Signing in again keeps the device identity
------------------------------------------
No Apple ID password is stored, so every expired session ends in a re-authentication. To Apple
a sign-in comes from a device, which FindMy.py describes with two ids (``ids.uid`` and
``ids.devid``) and the Anisette data (for the integrated provider its provisioning state, for a
remote server whatever that server holds). A sign-in from an unknown device raises a notice on
all of the user's Apple devices and has to earn trust again.

A re-authentication therefore signs in with an account that is restored from what the entry
already holds: the ids and the Anisette provider, with the login part reset to "logged out"
(finding H3). See ``_reusable_identity`` for what is taken over and when a new identity is
used instead. The initial setup always starts with a new one.

Library route: ``AsyncAppleAccount.from_json`` on a copy of the stored mapping. It is the
public way to give an account its ids and to restore its provider, and ``login()`` only asks
for the state ``LOGGED_OUT``, which the copy carries. Nothing private is touched.

What FindMy.py 0.10.2 offers for an expired session, from its source: nothing cheaper than a
full sign-in. ``fetch_raw_reports`` answers an HTTP 401 with ``_gsa_authenticate()``, which is
the SRP exchange with the password and fails without one; there is no request that renews the
session from stored tokens. The tokens of the first stage (``idms_pet``, ``adsid``) are
replaced by the Find My data once the sign-in completes and are not kept. Whether Apple skips
the second factor for a known device is Apple's decision and could not be tested.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from typing import TYPE_CHECKING, Literal, TypedDict, cast, final, override
from urllib.parse import urlsplit

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.core import HassJob, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    FileSelector,  # pyright: ignore[reportUnknownVariableType]
    FileSelectorConfig,
    NumberSelector,  # pyright: ignore[reportUnknownVariableType]
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,  # pyright: ignore[reportUnknownVariableType]
    SelectSelectorConfig,
)

from findmy import (
    AccountStateMapping,
    AsyncAppleAccount,
    AsyncSmsSecondFactor,
    AsyncTrustedDeviceSecondFactor,
    FindMyAccessory,
    FindMyAccessoryMapping,
    FixedRollingKeyPairAccessory,
    FixedRollingKeyPairAccessoryMapping,
    InvalidCredentialsError,
    KeyPair,
    KeyPairMapping,
    LocalAnisetteProvider,
    LoginState,
    RemoteAnisetteProvider,
    SmsSecondFactorMethod,
    TrustedDeviceSecondFactorMethod,
    UnhandledProtocolError,
)

from .account import without_password
from .anisette_libs import (
    AnisetteLibsChecksumError,
    AnisetteLibsUnavailableError,
    anisette_libs_path,
    async_ensure_anisette_libs,
)
from .const import (
    CONF_AWAY_TIMEOUT,
    CONFIG_FLOW_VERSION_MAJOR,
    CONFIG_FLOW_VERSION_MINOR,
    DEFAULT_AWAY_TIMEOUT_MINUTES,
    DOMAIN,
)
from .redaction import REDACTED as _REDACTED
from .redaction import credentials_are_recognised
from .redaction import describe_error as _describe
from .redaction import url_without_credentials as _url_without_credentials
from .worker import FindMyWorker, WorkerNotRunningError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path
    from types import FrameType
    from typing import Any

    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

type AsyncSecondFactorMethod = AsyncSmsSecondFactor | AsyncTrustedDeviceSecondFactor

_LOGGER = logging.getLogger(__name__)

# Sign-in, 2FA and the local Anisette emulation (CPU emulation, SRP, plist parsing) do
# seconds of synchronous work inside FindMy's coroutines. Each account flow therefore
# runs the library on a thread of its own (finding R20). The shared worker of the
# integration cannot be used: it does not exist before the first entry is set up.
_WORKER_NAME = "findmy-login"
# Limits on the run time of one job on that thread. A synchronous stretch cannot be
# interrupted, so a job ends at the next await after its limit.
# The first sign-in with the local Anisette emulation also provisions it. Its library bundle
# is in place before the job starts (anisette_libs.py), so no download counts against this.
_LOGIN_TIMEOUT = 120.0
# Requesting or submitting a code; submitting also finishes the sign-in.
_2FA_TIMEOUT = 60.0
# Reading or serialising the session, closing its HTTP sessions.
_LOCAL_TIMEOUT = 30.0
# Stopping runs the account's close on the worker, itself limited to 10 s by the worker.
_WORKER_STOP_TIMEOUT = 15.0

# The token FindMy needs to fetch location reports. Since Apple changed its sign-in in
# October 2026, sign-ins are reported to end without it (FindMy.py issue #278). The
# handling below follows those reports and the library source only; it could not be
# verified against Apple.
_FIND_MY_TOKEN = "searchPartyToken"  # noqa: S105 - the name of a field, not a secret
_ISSUE_TRACKER_URL = "https://github.com/malmeloo/FindMy.py/issues"

# Flow input that must never reach a log: the account password, the private key
# of a static tag (it grants location access forever, since static keys never
# rotate), the one-time 2FA code and the upload id of a key file.
_SECRET_FIELDS = frozenset({"password", "private_key", "code", "file"})


def _mask_email(email: str) -> str:
    """Keep just enough of an address to tell accounts apart in a shared log."""
    local, sep, domain = email.partition("@")
    if not sep:
        return _REDACTED
    return f"{local[:1]}***@{domain}"


def _redact(user_input: Mapping[str, object] | None) -> dict[str, object] | None:
    """Return a copy of flow input that is safe to log.

    Debug logs end up in public issues, so every log line of the flows goes
    through here instead of printing raw input.
    """
    if user_input is None:
        return None
    redacted: dict[str, object] = {}
    for key, value in user_input.items():
        if key in _SECRET_FIELDS:
            redacted[key] = _REDACTED
        elif key == "email" and isinstance(value, str):
            redacted[key] = _mask_email(value)
        elif key == "anisette_url" and isinstance(value, str):
            # May carry `user:password@`.
            redacted[key] = _url_without_credentials(value)
        elif isinstance(value, dict):
            # Form sections (e.g. advanced_options) arrive as nested dicts.
            redacted[key] = _redact(cast("dict[str, object]", value))
        else:
            redacted[key] = value
    return redacted


# Failures of FindMy calls that the user can fix or simply retry. Anything else is
# a bug and should surface with a traceback.
_FLOW_ERRORS = (
    InvalidCredentialsError,
    UnhandledProtocolError,
    aiohttp.ClientError,
    TimeoutError,
    json.JSONDecodeError,
    # The flow's worker stopped, e.g. because Home Assistant is shutting down.
    WorkerNotRunningError,
)


def _is_missing_find_my_token(err: BaseException) -> bool:
    """Whether FindMy failed because Apple's answer lacks the Find My token.

    Only this one key: any other ``KeyError`` is a bug and keeps its traceback.
    """
    return isinstance(err, KeyError) and err.args == (_FIND_MY_TOKEN,)


# The package behind the integrated Anisette provider, as its modules are named.
_ANISETTE_PACKAGE = "anisette"
# The local variable its provisioning keeps Apple's answer in (anisette/_session.py).
_PROVISIONING_ANSWER = "spim_response"
# How many field names of an answer without a status are listed.
_MAX_ANSWER_FIELDS = 8


def _module_of(frame: FrameType) -> str:
    name: object = frame.f_globals.get("__name__")
    return name if isinstance(name, str) else ""


def _anisette_failure(err: BaseException) -> str | None:
    """Describe a failure inside the integrated Anisette provider, or return None.

    The provider registers ("provisions") an emulated device with Apple before the first
    sign-in request. The ``anisette`` package reads Apple's answers without checking them,
    so an answer that lacks a field ends in a bare ``KeyError`` from deep inside the package,
    which the flow could only show as "Unknown error" (seen on a real system: ``KeyError:
    'ptm'`` from the request that finishes the provisioning).

    Such a failure is recognised by where it came from: its traceback passes through the
    ``anisette`` package, whether the package raised it itself or something it called did
    (its own HTTP client, when Apple cannot be reached). What Apple answered is not part of
    the exception. It is still in the frame that failed, so the status of the answer is read
    from there: its error code and message if it has them, otherwise the names of the fields
    it holds. Only those; an answer that went well holds the keys of the emulated device.
    """
    frames: list[FrameType] = []
    traceback = err.__traceback__
    while traceback is not None:
        module = _module_of(traceback.tb_frame)
        if module == _ANISETTE_PACKAGE or module.startswith(f"{_ANISETTE_PACKAGE}."):
            frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    if not frames:
        return None

    detail = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__
    innermost = frames[-1]
    described = f"{detail} in {_module_of(innermost)}.{innermost.f_code.co_name}"
    answer: object = None
    for frame in reversed(frames):
        answer = frame.f_locals.get(_PROVISIONING_ANSWER)
        if answer is not None:
            break
    if isinstance(answer, dict):
        fields = cast("dict[object, object]", answer)
        status = fields.get("Status")
        if isinstance(status, dict):
            status = cast("dict[object, object]", status)
            described += f"; Apple answered with error {status.get('ec')}: {status.get('em')}"
        else:
            names = sorted(str(name) for name in fields)[:_MAX_ANSWER_FIELDS]
            described += f"; Apple's answer holds only: {', '.join(names) or 'nothing'}"
    return _tame(described)


def _is_flow_error(err: BaseException) -> bool:
    """Whether a failure is one the user can fix or retry, see ``_error_key``."""
    return (
        isinstance(err, _FLOW_ERRORS)
        or _is_missing_find_my_token(err)
        or _anisette_failure(err) is not None
    )


def _has_find_my_token(account_data: Mapping[str, object]) -> bool:
    """Whether a serialised session carries the token needed to fetch reports.

    Follows the lookup in ``fetch_raw_reports`` of FindMy 0.10.2, which reads the
    token only when fetching. Without this check the flow would create an entry that
    fails on every refresh.
    """
    node: object = account_data
    for key in ("login", "data", "mobileme_data", "tokens", _FIND_MY_TOKEN):
        node = _get_key(node, key)
    return bool(node)


def _get_key(node: object, key: str) -> object:
    return cast("dict[str, object]", node).get(key) if isinstance(node, dict) else None


# How much of what Apple answered is logged and shown.
_MAX_REJECTION_LENGTH = 300
_NO_REJECTION_DETAILS = "no details"


def _rejection_reason(err: Exception, email: str | None) -> str:
    """What Apple said when it rejected a sign-in, fit for the log and for the form.

    FindMy.py raises ``InvalidCredentialsError`` for every error status of the two sign-in
    requests and puts Apple's message into its text: "Email verification failed: ..." for
    the first one, before any password is checked, and "Password authentication failed: ..."
    for the second. A wrong password is only one of the reasons; a locked account or a device
    Apple does not accept reads the same without this text.

    The text comes from a server, so it is cut, reduced to one line of printable characters,
    and the address that was entered is masked in it.
    """
    text = str(err)
    if email:
        text = re.sub(re.escape(email), _mask_email(email), text, flags=re.IGNORECASE)
    return _tame(text)


def _tame(text: str) -> str:
    """Reduce a text that quotes a server to one short line of printable characters."""
    text = " ".join(text.split())
    text = "".join(char if char.isprintable() else "?" for char in text)
    if len(text) > _MAX_REJECTION_LENGTH:
        text = text[:_MAX_REJECTION_LENGTH] + "..."
    return text or _NO_REJECTION_DETAILS


def _error_key(  # noqa: PLR0911  # one return per kind of failure reads best as a list
    err: Exception, action: str, *, remote_anisette: bool = False, email: str | None = None
) -> str:
    """Map an expected FindMy failure to a form error key and log it.

    ``email`` is the address of the sign-in, which is masked if a logged text quotes it.
    """
    if isinstance(err, InvalidCredentialsError):
        # At warning level: without Apple's text nobody can tell a wrong password from an
        # account or a device that Apple turns away.
        _LOGGER.warning(
            "Apple rejected the sign-in during %s: %s", action, _rejection_reason(err, email)
        )
        return "invalid_auth"
    if _is_missing_find_my_token(err):
        _LOGGER.warning("Apple did not return the Find My token during %s", action)
        return "find_my_token_missing"
    anisette_failure = _anisette_failure(err)
    if anisette_failure is not None:
        _LOGGER.warning(
            "The integrated Anisette provider failed during %s: %s", action, anisette_failure
        )
        return "anisette_failed"
    if isinstance(err, WorkerNotRunningError):
        _LOGGER.warning("Sign-in was stopped during %s: %s", action, err)
        return "login_incomplete"
    if isinstance(err, aiohttp.InvalidURL) or (
        remote_anisette and isinstance(err, json.JSONDecodeError)
    ):
        # During login only the remote Anisette server answers in JSON (Apple
        # uses plist), so a parse error means the URL points at something else.
        _LOGGER.warning("Anisette server not usable during %s: %s", action, _describe(err))
        return "invalid_anisette_url"
    if isinstance(err, aiohttp.ClientError | TimeoutError):
        _LOGGER.warning("Connection error during %s: %s", action, _describe(err))
        return "cannot_connect"
    _LOGGER.error("Unexpected response from Apple during %s", action, exc_info=err)
    return "protocol_error"


def _is_valid_anisette_url(url: str) -> bool:
    """Whether a URL can be used as it is, and logged without its credentials.

    A password with an unencoded ``/``, ``?`` or ``#`` ends the host part early: what is left
    of it reads as ``user:<not a port>``, and the rest of the password lands in the path. Such
    a URL would fail at the first request, and the redaction of redaction.py would not
    recognise its credentials. Asking for the port is what finds it.

    Credentials with an unencoded quote, angle bracket or space are refused for the same
    reason: the redaction would stop at that character (review finding K-3). All of these can
    be entered percent-encoded.
    """
    try:
        parsed = urlsplit(url)
        _ = parsed.port
        recognised = credentials_are_recognised(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and recognised


# What restoring an account from a prepared identity can still raise: a provisioning state
# that is not base64 (ValueError), fields the checks below do not look at (KeyError, or
# ValueError where FindMy.py translates it), or values of the wrong shape (TypeError,
# AssertionError).
_IDENTITY_ERRORS = (ValueError, KeyError, TypeError, AssertionError)

# The second half of the two log lines that tell why Apple may announce a new device.
_NEW_DEVICE = "signing in as a new device"

_ANISETTE_LOCAL = "aniLocal"
_ANISETTE_REMOTE = "aniRemote"


def _stored_anisette(account_data: object) -> dict[str, object] | None:
    """The Anisette part of stored account state, if a provider can be restored from it."""
    anisette = _get_key(account_data, "anisette")
    if not isinstance(anisette, dict):
        return None
    anisette = cast("dict[str, object]", anisette)
    kind = anisette.get("type")
    if kind == _ANISETTE_LOCAL:
        # No provisioning state yet is a valid state: the provider provisions on first use.
        prov_data = anisette.get("prov_data")
        return anisette if prov_data is None or isinstance(prov_data, str) else None
    if kind == _ANISETTE_REMOTE:
        url = anisette.get("url")
        return anisette if isinstance(url, str) and url else None
    return None


def _stored_anisette_url(account_data: object) -> str | None:
    """The URL of the remote Anisette server an account was stored with, if it uses one.

    May carry ``user:password@``. It is shown in the form of the entry's owner, and must go
    through ``_url_without_credentials`` before it is logged.
    """
    anisette = _stored_anisette(account_data)
    if anisette is None or anisette.get("type") != _ANISETTE_REMOTE:
        return None
    return cast("str", anisette["url"])


def _reusable_identity(
    account_data: object, anisette_url: str | None
) -> AccountStateMapping | None:
    """Stored account state reduced to the identity a new sign-in presents to Apple.

    The result restores (``AsyncAppleAccount.from_json``) to an account that is logged out, so
    ``login()`` accepts it, and that has

    * the device ids of the stored account, and
    * its Anisette provider, as long as the sign-in asks for the same one: the integrated
      provider with its provisioning state, or the remote server with the same URL and
      options. If the user chose another provider, that one is used with the same ids.

    Nothing of the old session is taken over: no tokens, no account details, no password.

    Returns None if the stored data has no usable ids; the sign-in then starts with a new
    identity, like the initial setup. Never raises, whatever the entry holds.
    """
    ids = _get_key(account_data, "ids")
    uid, devid = _get_key(ids, "uid"), _get_key(ids, "devid")
    if not (isinstance(uid, str) and uid and isinstance(devid, str) and devid):
        return None

    stored = _stored_anisette(account_data)
    requested = _ANISETTE_LOCAL if anisette_url is None else _ANISETTE_REMOTE
    if (
        stored is not None
        and stored.get("type") == requested
        and (anisette_url is None or stored.get("url") == anisette_url)
    ):
        # A copy: the data of a config entry is read-only and belongs to Home Assistant's loop.
        anisette = copy.deepcopy(stored)
    elif anisette_url is None:
        anisette = {"type": _ANISETTE_LOCAL, "prov_data": None}
    else:
        anisette = {"type": _ANISETTE_REMOTE, "url": anisette_url}

    identity: dict[str, object] = {
        "type": "account",
        "ids": {"uid": uid, "devid": devid},
        "account": {"username": None, "password": None, "info": None},
        "login": {"state": LoginState.LOGGED_OUT.value, "data": {}},
        "anisette": anisette,
    }
    # Checked field by field above; the type checker cannot follow that.
    return cast("AccountStateMapping", cast("object", identity))


DATA_SCHEMA_ACC_LOGIN = vol.Schema(
    {
        vol.Required("email"): str,
        vol.Required("password"): str,
        "advanced_options": section(
            vol.Schema(
                {
                    vol.Optional("anisette_url"): str,
                },
            ),
            {"collapsed": True},
        ),
    },
)
DATA_SCHEME_ACC_2FA = vol.Schema(
    {
        vol.Required("code"): str,
    },
)

DATA_SCHEME_DEV_CHOOSE = vol.Schema(
    {
        "device_type": SelectSelector(
            SelectSelectorConfig(
                options=["static", "rolling_derived", "rolling_pre_generated"],
                translation_key="device_type",
            ),
        ),
    },
)

DATA_SCHEME_DEV_STATIC = vol.Schema(
    {
        vol.Required("name"): str,
        vol.Required("private_key"): str,
    },
)

DATA_SCHEME_DEV_ROLLING_DERIVED = vol.Schema(
    {
        vol.Optional("name"): str,
        vol.Required("file"): FileSelector(FileSelectorConfig(accept=".json,.plist")),
    },
)

DATA_SCHEME_DEV_ROLLING_PRE_GENERATED = vol.Schema(
    {
        vol.Optional("name"): str,
        vol.Required("file"): FileSelector(FileSelectorConfig(accept=".json")),
    },
)


class LoginFlowAdvancedOptions(TypedDict):
    anisette_url: str | None


class LoginFlowInput(TypedDict):
    """Input to the login step."""

    email: str
    password: str
    advanced_options: LoginFlowAdvancedOptions


class MfaSubmitInput(TypedDict):
    code: str


class DeviceTypeInput(TypedDict):
    device_type: Literal["static", "rolling_derived", "rolling_pre_generated"]


class StaticDeviceInput(TypedDict):
    name: str
    private_key: str


class RollingDeviceInput(TypedDict):
    name: str
    file: str


class EntryDataAccount(TypedDict):
    type: Literal["account"]
    account_data: AccountStateMapping


class EntryDataStaticDevice(TypedDict):
    type: Literal["device_static"]
    data: KeyPairMapping


class EntryDataRollingDerivedDevice(TypedDict):
    type: Literal["device_rolling_derived"]
    data: FindMyAccessoryMapping


class EntryDataRollingPreGeneratedDevice(TypedDict):
    type: Literal["device_rolling_pre_generated"]
    data: FixedRollingKeyPairAccessoryMapping


type DeviceEntryData = (
    EntryDataStaticDevice | EntryDataRollingDerivedDevice | EntryDataRollingPreGeneratedDevice
)
type EntryData = EntryDataAccount | DeviceEntryData


@final
class _LoginSession:
    """The FindMy objects of one account flow.

    Every method runs on the flow's worker only, inside a job or as its stop
    callback, so the account, its HTTP sessions and the Anisette provider are
    created and used on the worker's event loop. Results are plain data.
    """

    def __init__(self) -> None:
        self._account: AsyncAppleAccount | None = None
        self._methods: list[AsyncSecondFactorMethod] = []

    def _require_account(self) -> AsyncAppleAccount:
        if self._account is None:
            msg = "the config flow has no open account"
            raise RuntimeError(msg)
        return self._account

    @staticmethod
    def _create_account(
        anisette_url: str | None,
        identity: AccountStateMapping | None,
        libs_path: Path | None,
    ) -> AsyncAppleAccount:
        """Create a logged-out account, with the given identity if it can be restored."""
        if identity is not None:
            try:
                # A copy per attempt: the library keeps the nested dicts it is given, and a
                # second attempt after a wrong password must start from the same data.
                return AsyncAppleAccount.from_json(
                    copy.deepcopy(identity), anisette_libs_path=libs_path
                )
            except _IDENTITY_ERRORS as err:
                # Only the kind of error: its text may quote stored data.
                _LOGGER.info(
                    "The stored device identity cannot be restored (%s); %s",
                    type(err).__name__,
                    _NEW_DEVICE,
                )
        if anisette_url is None:
            anisette = LocalAnisetteProvider(libs_path=libs_path)
        else:
            anisette = RemoteAnisetteProvider(anisette_url)
        return AsyncAppleAccount(anisette=anisette)

    async def async_login(
        self,
        email: str,
        password: str,
        anisette_url: str | None,
        *,
        identity: AccountStateMapping | None = None,
        libs_path: Path | None = None,
    ) -> LoginState:
        """Sign in with a new account object.

        ``identity`` is what ``_reusable_identity`` returned for a re-authentication; without
        it the account gets new device ids and a new Anisette provider. ``libs_path`` is the
        library bundle for the integrated Anisette provider, see anisette_libs.py.
        """
        # Each attempt needs a fresh account; closing the previous one first keeps
        # every wrong password from leaking a client session.
        await self.async_close()
        self._account = self._create_account(anisette_url, identity, libs_path)
        # FindMy's login-state decorator hides the return type from the type checker.
        _ = cast("LoginState", await self._account.login(email, password))
        return self._account.login_state

    async def async_login_state(self) -> LoginState:
        if self._account is None:
            return LoginState.LOGGED_OUT
        return self._account.login_state

    async def async_get_2fa_methods(self) -> list[str]:
        """Remember the usable 2FA methods and return their labels, in order."""
        methods = cast(
            "list[AsyncSecondFactorMethod]",
            await self._require_account().get_2fa_methods(),
        )
        self._methods = []
        labels: list[str] = []
        for method in methods:
            if isinstance(method, TrustedDeviceSecondFactorMethod):
                labels.append("Trusted Device")
            elif isinstance(method, SmsSecondFactorMethod):  # pyright: ignore[reportUnnecessaryIsInstance]
                labels.append(f"SMS - {method.phone_number}")
            else:
                _LOGGER.warning("Unknown 2FA method: %s", type(method).__name__)
                continue
            self._methods.append(method)
        return labels

    async def async_request_2fa(self, index: int) -> None:
        await self._methods[index].request()

    async def async_submit_2fa(self, index: int, code: str) -> LoginState:
        _ = await self._methods[index].submit(code)
        return self._require_account().login_state

    async def async_serialise(self) -> tuple[AccountStateMapping, str | None]:
        """Return the session to store, without the password, and the account name."""
        account = self._require_account()
        # Config entries are plain text and part of every backup, so the password
        # is never stored (finding S1); an expired session means re-authentication.
        # The copy keeps the account's own dicts from crossing the thread boundary.
        data = copy.deepcopy(without_password(account.to_json()))
        return data, cast("str | None", account.account_name)

    async def async_close(self) -> None:
        account, self._account = self._account, None
        self._methods = []
        if account is not None:
            await account.close()


async def _async_stop_worker(worker: FindMyWorker | None) -> None:
    """Stop a flow's worker without letting a stuck close hold up the caller."""
    if worker is None:
        return
    try:
        async with asyncio.timeout(_WORKER_STOP_TIMEOUT):
            await worker.async_stop()
    except TimeoutError:
        # The worker keeps stopping in the background; its thread is a daemon.
        _LOGGER.warning("The sign-in thread did not stop within %s s", _WORKER_STOP_TIMEOUT)


@final
class InitialSetupConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for initial setup."""

    VERSION, MINOR_VERSION = CONFIG_FLOW_VERSION_MAJOR, CONFIG_FLOW_VERSION_MINOR

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Options for rolling-key accessories."""
        return RollingDeviceOptionsFlow()

    @classmethod
    @callback
    @override
    def async_supports_options_flow(cls, config_entry: config_entries.ConfigEntry) -> bool:
        """Only accessories that are matched locally have presence options."""
        entry_type: object = config_entry.data.get("type")
        return entry_type == "device_rolling_derived"

    def __init__(self, *args, **kwargs) -> None:  # pyright: ignore[reportMissingParameterType]
        """Initialize."""
        super().__init__(*args, **kwargs)

        # The sign-in thread and the FindMy objects on it, created on the first
        # sign-in attempt and stopped when the flow ends.
        self._worker: FindMyWorker | None = None
        self._session: _LoginSession | None = None
        self._remove_shutdown_job: CALLBACK_TYPE | None = None
        # Set once Home Assistant removed the flow; no worker may be started after it.
        self._removed = False

        # Plain data copied out of the session's jobs. Home Assistant's side never
        # touches a FindMy object.
        self._login_state: LoginState | None = None
        # The address of the current sign-in, and what Apple said if it rejected it. Both
        # only serve the error texts.
        self._login_email: str | None = None
        self._rejection: str | None = None
        self._2fa_labels: list[str] = []
        self._2fa_method_index: int | None = None
        # Kept so the method selection can be shown again after a failed request.
        self._2fa_schema: vol.Schema | None = None

    def _log_step(self, step: str, user_input: Mapping[str, object] | None) -> None:
        _LOGGER.debug("%s Step: %s - %s", self.__class__.__name__, step, _redact(user_input))

    ##########################
    ### Sign-in worker     ###
    ##########################

    def _error(self, err: Exception, action: str, *, remote_anisette: bool = False) -> str:
        """Map a failure to a form error key, log it, and keep Apple's words for the form."""
        if isinstance(err, InvalidCredentialsError):
            self._rejection = _rejection_reason(err, self._login_email)
        else:
            self._rejection = _anisette_failure(err)
        return _error_key(err, action, remote_anisette=remote_anisette, email=self._login_email)

    def _error_placeholders(self) -> dict[str, str]:
        """What the texts of the form errors refer to; needed by every form that shows one."""
        return {
            # Used by the texts of the invalid_auth and anisette_failed errors.
            "apple_message": self._rejection or _NO_REJECTION_DETAILS,
            # Used by the text of the find_my_token_missing error.
            "issue_tracker_url": _ISSUE_TRACKER_URL,
            # Used by the text of the anisette_libs_changed error.
            "anisette_libs_path": str(anisette_libs_path(self.hass)),
        }

    def _reset_login(self) -> None:
        self._login_state = None
        self._2fa_labels = []
        self._2fa_method_index = None
        self._2fa_schema = None

    async def _async_ensure_worker(self) -> tuple[FindMyWorker, _LoginSession]:
        """Return the flow's running worker, starting one on first use."""
        if self._worker is not None and self._session is not None:
            if self._worker.is_running:
                return self._worker, self._session
            # It stopped without the flow asking, so its account is gone too.
            await self._async_stop_worker()
        if self._removed or self.hass.is_stopping:
            # A thread started now would outlive the flow or Home Assistant.
            msg = "the config flow has ended"
            raise WorkerNotRunningError(msg)

        worker = FindMyWorker(_WORKER_NAME)
        session = _LoginSession()
        worker.start()
        # Closing the account on the worker's stop covers every way the flow ends,
        # including cancellation while a job runs.
        _ = worker.add_stop_callback(session.async_close)
        self._worker, self._session = worker, session
        # Flows are not removed when Home Assistant stops; without this the thread
        # and its HTTP sessions would live until the process exits.
        self._remove_shutdown_job = self.hass.async_add_shutdown_job(
            HassJob(self._async_shutdown, "findmy config flow sign-in shutdown"),
        )
        return worker, session

    def _detach_worker(self) -> FindMyWorker | None:
        """Forget the worker and the session state that lived on it."""
        worker, self._worker = self._worker, None
        self._session = None
        self._reset_login()
        remove, self._remove_shutdown_job = self._remove_shutdown_job, None
        if remove is not None:
            remove()
        return worker

    async def _async_stop_worker(self) -> None:
        """Stop the worker; its stop callback closes the account."""
        await _async_stop_worker(self._detach_worker())

    async def _async_shutdown(self) -> None:
        # Home Assistant iterates over its shutdown jobs while starting them, so this
        # job must not unregister itself.
        self._remove_shutdown_job = None
        await self._async_stop_worker()

    @callback
    @override
    def async_remove(self) -> None:
        """Stop the sign-in thread when the flow ends, e.g. cancelled during 2FA.

        A sync callback: the stop is only requested here and awaited in a task, so
        the event loop is never blocked.
        """
        self._removed = True
        worker = self._detach_worker()
        if worker is not None:
            _ = self.hass.async_create_task(
                _async_stop_worker(worker),
                "findmy config flow sign-in cleanup",
            )

    async def _async_run[T](
        self,
        job: Callable[[_LoginSession], Awaitable[T]],
        timeout: float,  # noqa: ASYNC109
    ) -> T:
        """Run ``job(session)`` on the flow's worker and return its plain result."""
        worker, session = await self._async_ensure_worker()
        try:
            return await worker.run(lambda: job(session), timeout=timeout)
        except Exception as err:
            if isinstance(err, WorkerNotRunningError) or not _is_flow_error(err):
                # The session is gone, or a bug left it in an unknown state: drop it
                # instead of keeping a half-used account open.
                await self._async_stop_worker()
            raise

    async def _async_close_account(self) -> None:
        """Close the flow's account but keep the worker for another attempt."""
        self._reset_login()
        if self._worker is None or not self._worker.is_running:
            # Nothing open: a stopped worker closed its account on the way out.
            return
        try:
            await self._async_run(lambda session: session.async_close(), _LOCAL_TIMEOUT)
        except Exception as err:
            if not _is_flow_error(err):
                raise
            # The worker's stop callback retries when the flow ends.
            _LOGGER.debug("Could not close the account of the flow: %r", err)

    async def _async_current_login_state(self) -> LoginState:
        """Read the login state after a failed step; LOGGED_OUT if it is lost."""
        if self._worker is None or not self._worker.is_running:
            return LoginState.LOGGED_OUT
        try:
            return await self._async_run(
                lambda session: session.async_login_state(),
                _LOCAL_TIMEOUT,
            )
        except Exception as err:
            if not _is_flow_error(err):
                raise
            return LoginState.LOGGED_OUT

    async def _async_abort(self, reason: str) -> config_entries.ConfigFlowResult:
        await self._async_stop_worker()
        return self.async_abort(reason=reason)

    @override
    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        self._log_step("user", user_input)

        return self.async_show_menu(
            step_id="user",
            menu_options={
                "start_acc": "Apple Account",
                "start_dev": "FindMy Device",
            },
        )

    ##########################
    ### Account Setup Flow ###
    ##########################

    async def async_step_start_acc(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        self._log_step("start_acc", user_input)

        return self._show_login_form()

    def _reauth_email(self) -> str:
        return self._get_reauth_entry().unique_id or ""

    def _reauth_account_data(self) -> object:
        """What the entry being re-authenticated stores for its account; may be damaged."""
        return cast("Mapping[str, object]", self._get_reauth_entry().data).get("account_data")

    def _show_login_form(
        self,
        errors: dict[str, str] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show the login form of this flow, which differs for re-authentication."""
        placeholders = self._error_placeholders()
        if self.source != config_entries.SOURCE_REAUTH:
            return self.async_show_form(
                step_id="acc_login",
                data_schema=DATA_SCHEMA_ACC_LOGIN,
                errors=errors,
                description_placeholders=placeholders,
            )

        # The unique_id is the account email, so the address can be pre-filled
        # and the user only has to supply the password and 2FA code.
        email = self._reauth_email()
        suggested: dict[str, object] = {"email": email}
        anisette_url = _stored_anisette_url(self._reauth_account_data())
        if anisette_url is not None:
            # Without this the field starts empty, and submitting the form as it is would
            # move the account from its remote server to the integrated provider (finding
            # G7). Clearing the field is still how the user asks for exactly that.
            suggested["advanced_options"] = {"anisette_url": anisette_url}
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(DATA_SCHEMA_ACC_LOGIN, suggested),
            errors=errors,
            description_placeholders={**placeholders, "account": email},
        )

    async def _async_restart_login(self, error: str) -> config_entries.ConfigFlowResult:
        """Drop the current account and ask for the login again."""
        await self._async_close_account()
        return self._show_login_form({"base": error})

    async def _async_login(self, info: LoginFlowInput) -> config_entries.ConfigFlowResult:
        """Log in with the form input, then continue with 2FA or finish.

        Shared by the initial setup and re-authentication. Failures the user can
        fix or retry are shown on the login form instead of aborting the flow.
        """
        anisette_url = (info.get("advanced_options") or {}).get("anisette_url") or None
        _LOGGER.debug(
            "Logging into Apple account %s (anisette: %s)",
            _mask_email(info["email"]),
            _url_without_credentials(anisette_url) if anisette_url else "<integrated>",
        )
        if anisette_url is not None and not _is_valid_anisette_url(anisette_url):
            return self._show_login_form({"base": "invalid_anisette_url"})

        identity: AccountStateMapping | None = None
        if self.source == config_entries.SOURCE_REAUTH:
            identity = _reusable_identity(self._reauth_account_data(), anisette_url)
            if identity is None:
                _LOGGER.info("The stored account data holds no device identity; %s", _NEW_DEVICE)

        libs_path: Path | None = None
        if anisette_url is None:
            # The integrated provider must find its library bundle in place; otherwise it
            # downloads one itself, unchecked and without a time limit. Checked before every
            # attempt, on Home Assistant's side: it needs the HTTP session and the executor.
            try:
                libs_path = await async_ensure_anisette_libs(self.hass)
            except AnisetteLibsChecksumError as err:
                # The text names the file and the checksums, nothing else.
                _LOGGER.error("%s", err)  # noqa: TRY400  # no traceback: it explains itself
                return self._show_login_form({"base": "anisette_libs_changed"})
            except AnisetteLibsUnavailableError as err:
                _LOGGER.warning("%s", err)
                return self._show_login_form({"base": "anisette_libs_unavailable"})

        # Surrounding white space is never part of an Apple ID, but easily part of what was
        # pasted into the form, and Apple would answer that it does not know the account.
        email, password = info["email"].strip(), info["password"]
        self._login_email, self._rejection = email, None
        self._reset_login()
        try:
            self._login_state = await self._async_run(
                lambda session: session.async_login(
                    email, password, anisette_url, identity=identity, libs_path=libs_path
                ),
                _LOGIN_TIMEOUT,
            )
        except Exception as err:
            if not _is_flow_error(err):
                raise
            error = self._error(err, "login", remote_anisette=anisette_url is not None)
            return await self._async_restart_login(error)

        _LOGGER.debug("State after login: %s", self._login_state)

        if self._login_state == LoginState.REQUIRE_2FA:
            return await self.async_step_acc_2fa_prompt()
        return await self.async_step_acc_done()

    async def async_step_acc_login(
        self,
        info: LoginFlowInput | None,
    ) -> config_entries.ConfigFlowResult:
        self._log_step("acc_login", info)

        if info is None:
            _LOGGER.warning("Login step called without info data")
            return self._show_login_form()

        _ = await self.async_set_unique_id(info["email"].strip().lower())
        self._abort_if_unique_id_configured()

        return await self._async_login(info)

    async def async_step_acc_2fa_prompt(
        self,
        info: dict[Any, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        self._log_step("acc_2fa_prompt", info)

        if self._login_state != LoginState.REQUIRE_2FA:
            _LOGGER.error("2FA step called but account configured incorrectly")
            return await self._async_abort("unknown_error")

        try:
            labels = await self._async_run(
                lambda session: session.async_get_2fa_methods(),
                _2FA_TIMEOUT,
            )
        except Exception as err:
            if not _is_flow_error(err):
                raise
            # The account cannot be reused for another login attempt, so the user
            # starts over from the login form.
            return await self._async_restart_login(self._error(err, "2FA method lookup"))

        if not labels:
            return await self._async_restart_login("2fa_unavailable")

        self._2fa_labels = labels
        menu_options = [
            SelectOptionDict(label=label, value=str(i)) for i, label in enumerate(labels)
        ]
        self._2fa_schema = vol.Schema(
            {
                "method_id": SelectSelector(
                    SelectSelectorConfig(
                        options=menu_options,
                    ),
                ),
            },
        )
        return self.async_show_form(
            step_id="acc_2fa_request",
            data_schema=self._2fa_schema,
        )

    async def async_step_acc_2fa_request(
        self,
        info: dict[Any, Any] | None,
    ) -> config_entries.ConfigFlowResult:
        self._log_step("acc_2fa_request", info)

        if info is None or self._2fa_schema is None:
            _LOGGER.error("2FA requested but no method ID")
            return await self._async_abort("unknown_error")

        try:
            method_id = int(cast("int", info["method_id"]))
        except KeyError, TypeError, ValueError:
            method_id = -1
        # A range check instead of catching IndexError: negative ids would
        # silently pick a method from the end of the list.
        if not 0 <= method_id < len(self._2fa_labels):
            _LOGGER.warning("Unknown 2FA method ID: %s", info.get("method_id"))
            return self.async_show_form(
                step_id="acc_2fa_request",
                data_schema=self._2fa_schema,
                errors={"base": "invalid_2fa_method"},
            )
        self._2fa_method_index = method_id

        try:
            await self._async_run(
                lambda session: session.async_request_2fa(method_id),
                _2FA_TIMEOUT,
            )
        except Exception as err:
            if not _is_flow_error(err):
                raise
            error = self._error(err, "2FA code request")
            if isinstance(err, WorkerNotRunningError):
                # The account went with the worker; only a fresh login can continue.
                return await self._async_restart_login(error)
            return self.async_show_form(
                step_id="acc_2fa_request",
                data_schema=self._2fa_schema,
                errors={"base": error},
                description_placeholders=self._error_placeholders(),
            )

        return self.async_show_form(
            step_id="acc_2fa_submit",
            data_schema=DATA_SCHEME_ACC_2FA,
        )

    async def async_step_acc_2fa_submit(
        self,
        info: MfaSubmitInput,
    ) -> config_entries.ConfigFlowResult:
        self._log_step("acc_2fa_submit", info)

        code = info.get("code")
        if not code:
            _LOGGER.error("No 2FA code submitted")
            return self.async_show_form(
                step_id="acc_2fa_submit",
                data_schema=DATA_SCHEME_ACC_2FA,
                errors={"base": "2fa_invalid"},
            )

        method_id = self._2fa_method_index
        if self._login_state != LoginState.REQUIRE_2FA or method_id is None:
            _LOGGER.error("No active 2FA method in the flow")
            return await self._async_abort("unknown_error")

        error: str | None = None
        try:
            state = await self._async_run(
                lambda session: session.async_submit_2fa(method_id, code),
                _2FA_TIMEOUT,
            )
        except Exception as err:
            if not _is_flow_error(err):
                raise
            if isinstance(err, UnhandledProtocolError):
                # Apple answers a wrong code with an unexpected status code.
                _LOGGER.warning("2FA code was rejected", exc_info=True)
                error = "2fa_invalid"
            else:
                error = self._error(err, "2FA submit")
            if error == "find_my_token_missing":
                # The code was accepted; a new attempt starts with signing in.
                return await self._async_restart_login(error)
            state = await self._async_current_login_state()
        self._login_state = state

        _LOGGER.debug("State after 2FA: %s", state)
        if state == LoginState.LOGGED_IN:
            return await self.async_step_acc_done()
        if state == LoginState.REQUIRE_2FA:
            return self.async_show_form(
                step_id="acc_2fa_submit",
                data_schema=DATA_SCHEME_ACC_2FA,
                errors={"base": error or "2fa_invalid"},
                description_placeholders=self._error_placeholders(),
            )
        # The 2FA session is gone; only a fresh login can continue.
        return await self._async_restart_login(error or "login_incomplete")

    async def async_step_acc_done(
        self,
        info: dict[Any, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        self._log_step("acc_done", info)

        if self._session is None:
            _LOGGER.error("No account configured")
            return await self._async_abort("unknown_error")

        if self._login_state != LoginState.LOGGED_IN:
            # A half logged-in session would be stored and fail on every refresh.
            _LOGGER.warning("Login ended in state %s", self._login_state)
            return await self._async_restart_login("login_incomplete")

        try:
            account_data, account_name = await self._async_run(
                lambda session: session.async_serialise(),
                _LOCAL_TIMEOUT,
            )
        except Exception as err:
            if not _is_flow_error(err):
                raise
            return await self._async_restart_login(self._error(err, "saving the session"))

        if not _has_find_my_token(account_data):
            _LOGGER.warning("Apple did not return the Find My token during sign-in")
            return await self._async_restart_login("find_my_token_missing")

        # The entry restores its own session from the stored data; stopping the
        # worker closes the flow's account.
        await self._async_stop_worker()

        data = EntryDataAccount(type="account", account_data=account_data)
        if self.source == config_entries.SOURCE_REAUTH:
            # Re-authentication: refresh the stored session on the existing
            # entry rather than creating a duplicate account entry.
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data=data,
            )

        return self.async_create_entry(
            title=f"Account: {account_name}",
            data=data,
        )

    ###########################
    ### Re-authentication   ###
    ###########################

    async def async_step_reauth(
        self,
        _entry_data: dict[str, Any],
    ) -> config_entries.ConfigFlowResult:
        """Start re-authentication for an account whose session expired."""
        self._log_step("reauth", None)

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        info: LoginFlowInput | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Log the account in again, then reuse the normal 2FA steps."""
        self._log_step("reauth_confirm", info)

        if info is None:
            return self._show_login_form()

        # The e-mail field stays editable; logging into another account here
        # would silently replace the session of the entry being repaired.
        _ = await self.async_set_unique_id(info["email"].strip().lower())
        self._abort_if_unique_id_mismatch(reason="wrong_account")

        return await self._async_login(info)

    #########################
    ### Device Setup Flow ###
    #########################

    async def async_step_start_dev(
        self,
        info: dict[Any, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        self._log_step("start_dev", info)

        return self.async_show_form(
            step_id="dev_choose",
            data_schema=DATA_SCHEME_DEV_CHOOSE,
        )

    async def async_step_dev_choose(
        self,
        info: DeviceTypeInput | None = None,
    ) -> config_entries.ConfigFlowResult:
        self._log_step("dev_choose", info)

        dev_type = (info or {}).get("device_type", None)
        if dev_type == "static":
            return self.async_show_form(
                step_id="dev_static",
                data_schema=DATA_SCHEME_DEV_STATIC,
            )
        if dev_type == "rolling_derived":
            return self.async_show_form(
                step_id="dev_rolling_derived",
                data_schema=DATA_SCHEME_DEV_ROLLING_DERIVED,
            )
        if dev_type == "rolling_pre_generated":
            return self.async_show_form(
                step_id="dev_rolling_pre_generated",
                data_schema=DATA_SCHEME_DEV_ROLLING_PRE_GENERATED,
            )

        return self.async_show_form(
            step_id="dev_choose",
            data_schema=DATA_SCHEME_DEV_CHOOSE,
            errors={"base": "invalid_dev"},
        )

    async def async_step_dev_static(
        self,
        info: StaticDeviceInput | None = None,
    ) -> config_entries.ConfigFlowResult:
        self._log_step("dev_static", info)

        if not info:
            return self.async_show_form(
                step_id="dev_static",
                data_schema=DATA_SCHEME_DEV_STATIC,
                errors={"base": "invalid_dev"},
            )

        name = info.get("name", "Unknown")
        key = info.get("private_key", "")

        try:
            device = KeyPair.from_b64(key)
        except ValueError:
            return self.async_show_form(
                step_id="dev_static",
                data_schema=DATA_SCHEME_DEV_STATIC,
                errors={"base": "invalid_dev_key"},
            )
        device.name = name or "Unknown"

        data = EntryDataStaticDevice(
            type="device_static",
            data=device.to_json(),
        )

        return self.async_create_entry(
            title=f"Device (Static): {name}",
            data=data,
        )

    async def async_step_dev_rolling_derived(
        self,
        info: RollingDeviceInput | None = None,
    ) -> config_entries.ConfigFlowResult:
        self._log_step("dev_rolling_derived", info)

        if not info:
            return self.async_show_form(
                step_id="dev_rolling_derived",
                data_schema=DATA_SCHEME_DEV_ROLLING_DERIVED,
                errors={"base": "invalid_dev"},
            )

        name = info.get("name", None)
        file_id = info.get("file", "")

        device = await self.hass.async_add_executor_job(
            _get_derived_key_device_from_file,
            self.hass,
            file_id,
        )
        if device is None:
            return self.async_show_form(
                step_id="dev_rolling_derived",
                data_schema=DATA_SCHEME_DEV_ROLLING_DERIVED,
                errors={"base": "invalid_dev_key"},
            )
        device.name = name or device.name or "Unknown"

        data = EntryDataRollingDerivedDevice(
            type="device_rolling_derived",
            data=device.to_json(),
        )

        return self.async_create_entry(
            title=f"Device (Rolling, derived): {device.name}",
            data=data,
        )

    async def async_step_dev_rolling_pre_generated(
        self,
        info: RollingDeviceInput | None = None,
    ) -> config_entries.ConfigFlowResult:
        self._log_step("dev_rolling_pre_generated", info)

        if not info:
            return self.async_show_form(
                step_id="dev_rolling_pre_generated",
                data_schema=DATA_SCHEME_DEV_ROLLING_PRE_GENERATED,
                errors={"base": "invalid_dev"},
            )

        name = info.get("name", None)
        file_id = info.get("file", "")

        device = await self.hass.async_add_executor_job(
            _get_pre_generated_key_device_from_file,
            self.hass,
            file_id,
        )
        if device is None:
            return self.async_show_form(
                step_id="dev_rolling_pre_generated",
                data_schema=DATA_SCHEME_DEV_ROLLING_PRE_GENERATED,
                errors={"base": "invalid_dev_key"},
            )
        device.name = name or device.name or "Unknown"

        data = EntryDataRollingPreGeneratedDevice(
            type="device_rolling_pre_generated",
            data=device.to_json(),
        )

        return self.async_create_entry(
            title=f"Device (Rolling, pre-generated): {device.name}",
            data=data,
        )


# What the FindMy parsers raise for a file of the wrong kind or with missing
# fields: KeyError for an absent field (also a JSON file without "type"),
# AssertionError for a different "type", TypeError for unexpected value types,
# and ValueError for decode errors and a missing upload.
_KEY_FILE_ERRORS = (KeyError, ValueError, TypeError, AssertionError)


def _parse_key_file[T](path: Path, *parsers: Callable[[Path], T]) -> T | None:
    """Return the result of the first parser that accepts the file, if any."""
    for parser in parsers:
        try:
            return parser(path)
        except _KEY_FILE_ERRORS as err:
            # Only the error type: messages may quote parts of the key file.
            _LOGGER.debug("Key file rejected by %s: %s", parser.__qualname__, type(err).__name__)
    return None


def _get_derived_key_device_from_file(hass: HomeAssistant, file_id: str) -> FindMyAccessory | None:
    """Load a FindMyAccessory from an uploaded .plist or .json file."""
    try:
        with process_uploaded_file(hass, file_id) as path:
            return _parse_key_file(
                path,
                FindMyAccessory.from_plist,  # pyright: ignore[reportUnknownArgumentType]
                FindMyAccessory.from_json,
            )
    except ValueError:
        _LOGGER.warning("Uploaded key file is missing or expired")
        return None


def _get_pre_generated_key_device_from_file(
    hass: HomeAssistant,
    file_id: str,
) -> FixedRollingKeyPairAccessory | None:
    """Load a FixedRollingKeyPairAccessory from an uploaded .json file."""
    try:
        with process_uploaded_file(hass, file_id) as path:
            return _parse_key_file(path, FixedRollingKeyPairAccessory.from_json)
    except ValueError:
        _LOGGER.warning("Uploaded key file is missing or expired")
        return None


DATA_SCHEMA_ROLLING_OPTIONS = vol.Schema(
    {
        vol.Required(CONF_AWAY_TIMEOUT, default=DEFAULT_AWAY_TIMEOUT_MINUTES): NumberSelector(
            NumberSelectorConfig(
                min=1,
                max=240,
                step=1,
                unit_of_measurement="min",
                mode=NumberSelectorMode.BOX,
            ),
        ),
    },
)


@final
class RollingDeviceOptionsFlow(config_entries.OptionsFlow):
    """Local presence options of a rolling-key accessory."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_AWAY_TIMEOUT: int(cast("float", user_input[CONF_AWAY_TIMEOUT]))},
            )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                DATA_SCHEMA_ROLLING_OPTIONS,
                self.config_entry.options,
            ),
        )
