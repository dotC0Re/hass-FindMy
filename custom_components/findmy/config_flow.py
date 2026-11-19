# pyright: reportUnknownParameterType=false, reportExplicitAny=false, reportUnknownMemberType=false

"""Integration config flow."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
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
from .const import (
    CONF_AWAY_TIMEOUT,
    CONFIG_FLOW_VERSION_MAJOR,
    CONFIG_FLOW_VERSION_MINOR,
    DEFAULT_AWAY_TIMEOUT_MINUTES,
    DOMAIN,
)
from .worker import FindMyWorker, WorkerNotRunningError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path
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
# The first sign-in with the local Anisette emulation also downloads and provisions it.
_LOGIN_TIMEOUT = 120.0
# Requesting or submitting a code; submitting also finishes the sign-in.
_2FA_TIMEOUT = 60.0
# Reading or serialising the session, closing its HTTP sessions.
_LOCAL_TIMEOUT = 30.0
# Stopping runs the account's close on the worker, itself limited to 10 s by the worker.
_WORKER_STOP_TIMEOUT = 15.0

# Flow input that must never reach a log: the account password, the private key
# of a static tag (it grants location access forever, since static keys never
# rotate), the one-time 2FA code and the upload id of a key file.
_SECRET_FIELDS = frozenset({"password", "private_key", "code", "file"})
_REDACTED = "**REDACTED**"


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


def _is_flow_error(err: BaseException) -> bool:
    """Whether a failure is one the user can fix or retry, see ``_error_key``."""
    return isinstance(err, _FLOW_ERRORS)


def _error_key(err: Exception, action: str, *, remote_anisette: bool = False) -> str:
    """Map an expected FindMy failure to a form error key and log it."""
    if isinstance(err, InvalidCredentialsError):
        return "invalid_auth"
    if isinstance(err, WorkerNotRunningError):
        _LOGGER.warning("Sign-in was stopped during %s: %s", action, err)
        return "login_incomplete"
    if isinstance(err, aiohttp.InvalidURL) or (
        remote_anisette and isinstance(err, json.JSONDecodeError)
    ):
        # During login only the remote Anisette server answers in JSON (Apple
        # uses plist), so a parse error means the URL points at something else.
        _LOGGER.warning("Anisette server not usable during %s: %r", action, err)
        return "invalid_anisette_url"
    if isinstance(err, aiohttp.ClientError | TimeoutError):
        _LOGGER.warning("Connection error during %s: %r", action, err)
        return "cannot_connect"
    _LOGGER.error("Unexpected response from Apple during %s", action, exc_info=err)
    return "protocol_error"


def _url_without_credentials(url: str) -> str:
    """Drop ``user:password@`` from a URL before it is logged."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return _REDACTED
    if parsed.username is None and parsed.password is None:
        return url
    host = parsed.hostname or ""
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return parsed._replace(netloc=f"{_REDACTED}@{netloc}").geturl()


def _is_valid_anisette_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


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

    async def async_login(
        self,
        email: str,
        password: str,
        anisette_url: str | None,
    ) -> LoginState:
        # Each attempt needs a fresh account; closing the previous one first keeps
        # every wrong password from leaking a client session.
        await self.async_close()
        if anisette_url is None:
            anisette = LocalAnisetteProvider()
        else:
            anisette = RemoteAnisetteProvider(anisette_url)
        self._account = AsyncAppleAccount(anisette=anisette)
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
        self._2fa_labels: list[str] = []
        self._2fa_method_index: int | None = None
        # Kept so the method selection can be shown again after a failed request.
        self._2fa_schema: vol.Schema | None = None

    def _log_step(self, step: str, user_input: Mapping[str, object] | None) -> None:
        _LOGGER.debug("%s Step: %s - %s", self.__class__.__name__, step, _redact(user_input))

    ##########################
    ### Sign-in worker     ###
    ##########################

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

    def _show_login_form(
        self,
        errors: dict[str, str] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show the login form of this flow, which differs for re-authentication."""
        if self.source != config_entries.SOURCE_REAUTH:
            return self.async_show_form(
                step_id="acc_login",
                data_schema=DATA_SCHEMA_ACC_LOGIN,
                errors=errors,
            )

        # The unique_id is the account email, so the address can be pre-filled
        # and the user only has to supply the password and 2FA code.
        email = self._reauth_email()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                DATA_SCHEMA_ACC_LOGIN,
                {"email": email},
            ),
            errors=errors,
            description_placeholders={"account": email},
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

        email, password = info["email"], info["password"]
        self._reset_login()
        try:
            self._login_state = await self._async_run(
                lambda session: session.async_login(email, password, anisette_url),
                _LOGIN_TIMEOUT,
            )
        except Exception as err:
            if not _is_flow_error(err):
                raise
            error = _error_key(err, "login", remote_anisette=anisette_url is not None)
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

        _ = await self.async_set_unique_id(info["email"].lower())
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
            return await self._async_restart_login(_error_key(err, "2FA method lookup"))

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
            error = _error_key(err, "2FA code request")
            if isinstance(err, WorkerNotRunningError):
                # The account went with the worker; only a fresh login can continue.
                return await self._async_restart_login(error)
            return self.async_show_form(
                step_id="acc_2fa_request",
                data_schema=self._2fa_schema,
                errors={"base": error},
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
                error = _error_key(err, "2FA submit")
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
            return await self._async_restart_login(_error_key(err, "saving the session"))

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
        _ = await self.async_set_unique_id(info["email"].lower())
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
