"""A custom integration for Home Assistant to track your Find My-enabled devices.

Every config entry stands for one Apple account or one device. This module migrates stored
entries, and sets entries up and unloads them: the object behind an entry is kept in
:class:`~.storage.RuntimeStorage`, its entities come from the platforms listed below.
"""

import logging
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.helpers import config_validation as cv

from .account import AccountRuntime
from .const import CONFIG_FLOW_VERSION_MAJOR, CONFIG_FLOW_VERSION_MINOR, DOMAIN
from .services import async_register as _async_register_services
from .storage import RuntimeStorage

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

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


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entry to the current format."""
    if config_entry.version >= CONFIG_FLOW_VERSION_MAJOR:
        return True

    _LOGGER.info(
        "Migrating configuration from version %s.%s to version %s.%s",
        config_entry.version,
        config_entry.minor_version,
        CONFIG_FLOW_VERSION_MAJOR,
        CONFIG_FLOW_VERSION_MINOR,
    )

    if config_entry.version == 1 and config_entry.data.get("type") == "device_rolling":
        new_data = {**config_entry.data}
        _LOGGER.info(
            "Migrating entry %s from 'device_rolling' to 'device_rolling_derived'",
            config_entry.entry_id,
        )
        new_data["type"] = "device_rolling_derived"

        _ = hass.config_entries.async_update_entry(
            config_entry,
            data=new_data,
            version=CONFIG_FLOW_VERSION_MAJOR,
            minor_version=CONFIG_FLOW_VERSION_MINOR,
        )

    _LOGGER.info(
        "Migration to configuration version %s.%s successful",
        config_entry.version,
        config_entry.minor_version,
    )

    return True


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    _ = RuntimeStorage.attach(hass)
    _async_register_services(hass)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry[EntryData]) -> bool:
    _LOGGER.debug("Setting up FindMy entry: %s", entry.entry_id)

    storage = RuntimeStorage.get(hass)

    # An account that cannot be restored, or is not signed in, raises ConfigEntryError or
    # ConfigEntryAuthFailed here and stores nothing.
    item = await storage.add_entry(entry)

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
