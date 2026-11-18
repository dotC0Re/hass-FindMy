# pyright: reportUnknownParameterType=false, reportExplicitAny=false, reportUnknownMemberType=false

"""Integration config flow."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Literal, TypedDict, cast, final, override
from urllib.parse import urlsplit

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.core import callback
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

from .const import (
    CONF_AWAY_TIMEOUT,
    CONFIG_FLOW_VERSION_MAJOR,
    CONFIG_FLOW_VERSION_MINOR,
    DEFAULT_AWAY_TIMEOUT_MINUTES,
    DOMAIN,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from typing import Any

    from homeassistant.core import HomeAssistant

type AsyncSecondFactorMethod = AsyncSmsSecondFactor | AsyncTrustedDeviceSecondFactor

_LOGGER = logging.getLogger(__name__)

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
)


def _error_key(err: Exception, action: str, *, remote_anisette: bool = False) -> str:
    """Map an expected FindMy failure to a form error key and log it."""
    if isinstance(err, InvalidCredentialsError):
        return "invalid_auth"
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

        self._account: AsyncAppleAccount | None = None
        self._2fa_method: AsyncSecondFactorMethod | None = None

        self._error: Exception | None = None
        self._2fa_methods: list[AsyncSecondFactorMethod] = []
        # Kept so the method selection can be shown again after a failed request.
        self._2fa_schema: vol.Schema | None = None

        # Set while re-authenticating an existing entry (see async_step_reauth).
        self._reauth_entry: config_entries.ConfigEntry | None = None

    def _log_step(self, step: str, user_input: Mapping[str, object] | None) -> None:
        _LOGGER.debug("%s Step: %s - %s", self.__class__.__name__, step, _redact(user_input))

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

        return self.async_show_form(
            step_id="acc_login",
            data_schema=DATA_SCHEMA_ACC_LOGIN,
        )

    def _reauth_email(self) -> str:
        entry = self._reauth_entry
        return (entry.unique_id or "") if entry else ""

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

    async def _async_login(self, info: LoginFlowInput) -> config_entries.ConfigFlowResult:
        """Log in with the form input, then continue with 2FA or finish.

        Shared by the initial setup and re-authentication. Failures the user can
        fix or retry are shown on the login form instead of aborting the flow.
        """
        anisette_url = (info.get("advanced_options") or {}).get("anisette_url") or None
        _LOGGER.debug(
            "Logging into Apple account %s (anisette: %s)",
            _mask_email(info["email"]),
            anisette_url or "<integrated>",
        )
        if anisette_url is not None and not _is_valid_anisette_url(anisette_url):
            return self._show_login_form({"base": "invalid_anisette_url"})

        if anisette_url is None:
            anisette = LocalAnisetteProvider()
        else:
            anisette = RemoteAnisetteProvider(anisette_url)
        self._account = AsyncAppleAccount(anisette=anisette)

        try:
            await self._account.login(info["email"], info["password"])
        except _FLOW_ERRORS as err:
            error = _error_key(err, "login", remote_anisette=anisette_url is not None)
            return self._show_login_form({"base": error})

        _LOGGER.debug("State after login: %s", self._account.login_state)

        if self._account.login_state == LoginState.REQUIRE_2FA:
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

        if self._account is None or self._account.login_state != LoginState.REQUIRE_2FA:
            _LOGGER.error("2FA step called but account configured incorrectly")
            return self.async_abort(reason="unknown_error")

        try:
            self._2fa_methods = cast(
                "list[AsyncSecondFactorMethod]",
                await self._account.get_2fa_methods(),
            )
        except _FLOW_ERRORS as err:
            # The account cannot be reused for another login attempt, so the user
            # starts over from the login form.
            return self._show_login_form({"base": _error_key(err, "2FA method lookup")})

        menu_options: list[SelectOptionDict] = []
        for i, method in enumerate(self._2fa_methods):
            if isinstance(method, TrustedDeviceSecondFactorMethod):
                menu_options.append(
                    {
                        "label": "Trusted Device",
                        "value": str(i),
                    },
                )
            elif isinstance(method, SmsSecondFactorMethod):  # pyright: ignore[reportUnnecessaryIsInstance]
                menu_options.append(
                    {
                        "label": f"SMS - {method.phone_number}",
                        "value": str(i),
                    },
                )
            else:
                _LOGGER.warning("Unknown 2FA method: %s", method)
                continue

        if not menu_options:
            return self._show_login_form({"base": "2fa_unavailable"})

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
            return self.async_abort(reason="unknown_error")

        try:
            method_id = int(cast("int", info["method_id"]))
        except KeyError, TypeError, ValueError:
            method_id = -1
        # A range check instead of catching IndexError: negative ids would
        # silently pick a method from the end of the list.
        if not 0 <= method_id < len(self._2fa_methods):
            _LOGGER.warning("Unknown 2FA method ID: %s", info.get("method_id"))
            return self.async_show_form(
                step_id="acc_2fa_request",
                data_schema=self._2fa_schema,
                errors={"base": "invalid_2fa_method"},
            )
        self._2fa_method = self._2fa_methods[method_id]

        try:
            await self._2fa_method.request()
        except _FLOW_ERRORS as err:
            return self.async_show_form(
                step_id="acc_2fa_request",
                data_schema=self._2fa_schema,
                errors={"base": _error_key(err, "2FA code request")},
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

        if self._account is None or self._2fa_method is None:
            _LOGGER.error("No active 2FA method in the flow")
            return self.async_abort(reason="unknown_error")

        try:
            _ = await self._2fa_method.submit(code)
        except UnhandledProtocolError:
            # Apple answers a wrong code with an unexpected status code.
            _LOGGER.warning("2FA code was rejected", exc_info=True)
            error = "2fa_invalid"
        except _FLOW_ERRORS as err:
            error = _error_key(err, "2FA submit")
        else:
            error = None

        state = self._account.login_state
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
        return self._show_login_form({"base": error or "login_incomplete"})

    async def async_step_acc_done(
        self,
        info: dict[Any, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        self._log_step("acc_done", info)

        if self._account is None:
            _LOGGER.error("No account configured")
            return self.async_abort(reason="unknown_error")

        if self._account.login_state != LoginState.LOGGED_IN:
            # A half logged-in session would be stored and fail on every refresh.
            _LOGGER.warning("Login ended in state %s", self._account.login_state)
            return self._show_login_form({"base": "login_incomplete"})

        data = EntryDataAccount(
            type="account",
            account_data=self._account.to_json(),
        )

        if self._reauth_entry is not None:
            # Re-authentication: refresh the stored session on the existing
            # entry rather than creating a duplicate account entry.
            return self.async_update_reload_and_abort(
                self._reauth_entry,
                data=data,
            )

        return self.async_create_entry(
            title=f"Account: {self._account.account_name}",
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

        entry_id = self.context.get("entry_id")
        if entry_id is not None:
            self._reauth_entry = self.hass.config_entries.async_get_entry(entry_id)

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        info: LoginFlowInput | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Log the account in again, then reuse the normal 2FA steps."""
        self._log_step("reauth_confirm", info)

        if info is None:
            return self._show_login_form()

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
