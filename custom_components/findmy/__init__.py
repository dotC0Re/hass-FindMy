"""A custom integration for Home Assistant to track your Find My-enabled devices.

Every config entry stands for one Apple account or one device. This module migrates stored
entries, and sets entries up and unloads them: the object behind an entry is kept in
:class:`~.storage.RuntimeStorage`, its entities come from the platforms listed below.
"""

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from homeassistant.const import Platform
from homeassistant.helpers import config_validation as cv

from .account import AccountRuntime, without_password
from .const import CONFIG_FLOW_VERSION_MAJOR, CONFIG_FLOW_VERSION_MINOR, DOMAIN
from .last_reports import LastReportStore
from .services import async_register as _async_register_services
from .storage import RuntimeStorage

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

    from findmy import AccountStateMapping

    from .config_flow import EntryData

_LOGGER = logging.getLogger(__name__)

# Set up from config entries only; there is no YAML configuration to validate.
# Home Assistant types the returned validator with bare dicts.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]

ACCOUNT_PLATFORMS = [
    Platform.SENSOR,
]
DEVICE_PLATFORMS = [
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]


def _without_stored_password(data: dict[str, object]) -> dict[str, object]:
    """Remove the Apple ID password from the data of an account entry (finding S1).

    Data that does not look like account state is returned as it is. Setting the entry up
    reports that as damaged; a migration that fails would only hide the reason.
    """
    account_data = data.get("account_data")
    if not isinstance(account_data, Mapping):
        return data
    credentials = cast("Mapping[str, object]", account_data).get("account")
    if not isinstance(credentials, Mapping):
        return data
    if cast("Mapping[str, object]", credentials).get("password") is None:
        return data
    stripped = without_password(cast("AccountStateMapping", account_data))
    return {**data, "account_data": stripped}


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entry to the current format.

    * 1.x -> 2.0: the entry type 'device_rolling' was renamed to 'device_rolling_derived'.
    * 2.0 -> 2.1: account entries no longer contain the Apple ID password.
    """
    stored = (config_entry.version, config_entry.minor_version)
    current = (CONFIG_FLOW_VERSION_MAJOR, CONFIG_FLOW_VERSION_MINOR)
    if stored >= current:
        # Home Assistant also asks for a migration when the stored minor version is the
        # higher one, after a downgrade. Minor versions are compatible in both directions.
        return True

    _LOGGER.info("Migrating configuration from version %s.%s to version %s.%s", *stored, *current)

    data: dict[str, object] = {**config_entry.data}

    if config_entry.version == 1 and data.get("type") == "device_rolling":
        _LOGGER.info(
            "Migrating entry %s from 'device_rolling' to 'device_rolling_derived'",
            config_entry.entry_id,
        )
        data["type"] = "device_rolling_derived"

    if data.get("type") == "account":
        stripped = _without_stored_password(data)
        if stripped is not data:
            _LOGGER.info(
                "Removing the stored Apple ID password from entry %s", config_entry.entry_id
            )
            data = stripped

    # Always, also when the data stayed the same: an entry left at its old version would be
    # migrated again on every start.
    _ = hass.config_entries.async_update_entry(
        config_entry,
        data=data,
        version=CONFIG_FLOW_VERSION_MAJOR,
        minor_version=CONFIG_FLOW_VERSION_MINOR,
    )

    _LOGGER.info(
        "Migration to configuration version %s.%s successful",
        config_entry.version,
        config_entry.minor_version,
    )

    return True


def _remove_stored_passwords(hass: HomeAssistant) -> None:
    """Remove the Apple ID password from every account entry, whatever its state or version.

    The migration only reaches entries Home Assistant sets up. A disabled account entry is never
    migrated, and would keep its password in plain text for as long as it stays disabled. Going
    by content also cleans an entry that got a password again under another version of the
    integration. The version of an entry is left to the migration.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        data: dict[str, object] = {**entry.data}
        if data.get("type") != "account":
            continue
        stripped = _without_stored_password(data)
        if stripped is not data:
            _LOGGER.info("Removing the stored Apple ID password from entry %s", entry.entry_id)
            _ = hass.config_entries.async_update_entry(entry, data=stripped)


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    _remove_stored_passwords(hass)
    storage = RuntimeStorage.attach(hass)
    # Before any entry is set up, so that the first refresh already knows when each account
    # last fetched, also before this start of Home Assistant.
    await storage.budget.async_load()
    # Likewise, so that the entities of a device start with its last known location.
    await storage.reports.async_load()
    _async_register_services(hass)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry[EntryData]) -> bool:
    _LOGGER.debug("Setting up FindMy entry: %s", entry.entry_id)

    storage = RuntimeStorage.get(hass)

    # An account that cannot be restored, or is not signed in, raises ConfigEntryError or
    # ConfigEntryAuthFailed here and stores nothing.
    item = await storage.add_entry(entry)

    if not isinstance(item, AccountRuntime):
        # Before the entities exist, so that they start with the last known location instead
        # of none until the first fetch, which the fetch limit may put off by 15 minutes.
        storage.coordinator.restore_report(item, entry.entry_id)

    # only initialize device tracker entities for actual devices
    platforms = ACCOUNT_PLATFORMS if isinstance(item, AccountRuntime) else DEVICE_PLATFORMS
    try:
        await hass.config_entries.async_forward_entry_setups(entry, platforms)
    except Exception:
        # Home Assistant does not unload an entry whose setup failed. Without this, the item
        # would stay in the storage (an account would keep being fetched, and would never be
        # closed) although the entry has no entities.
        _ = await storage.del_entry(entry)
        raise

    await storage.coordinator.reload()
    # All entries share one coordinator. Delay the first refresh so multiple setup calls can
    # settle, then coalesce them into one refresh instead of a startup storm.
    storage.coordinator.schedule_refresh()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry[EntryData]) -> bool:
    _LOGGER.debug("Unloading FindMy entry: %s", entry.entry_id)

    storage = RuntimeStorage.get(hass)
    try:
        item = storage.get_entry(entry)
    except KeyError:
        _LOGGER.warning(
            "Entry %s not found in storage during unload",
            entry.entry_id,
        )
        return False

    # The entities go first, while what they show still exists. If they cannot be unloaded,
    # the entry stays loaded as a whole.
    platforms = ACCOUNT_PLATFORMS if isinstance(item, AccountRuntime) else DEVICE_PLATFORMS
    if not await hass.config_entries.async_unload_platforms(entry, platforms):
        return False

    # Closes an account on the worker.
    _ = await storage.del_entry(entry)

    # The interval is shared between the accounts. Without this, the accounts that remain after
    # one was removed would keep being fetched more often than once per 15 minutes.
    await storage.coordinator.reload()

    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry[EntryData]) -> None:
    """Forget what is kept for an entry outside of it: the last location of its device."""
    if DOMAIN in hass.data:
        RuntimeStorage.get(hass).reports.forget(entry.entry_id)
        return
    # Removed while the integration is not loaded (all its entries disabled, say): the stored
    # reports still have to forget the device, so open them just for that.
    reports = LastReportStore(hass)
    await reports.async_load()
    reports.forget(entry.entry_id)
