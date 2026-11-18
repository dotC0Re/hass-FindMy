"""FindMy service registrations - bulk management helpers.

Only `findmy.delete_devices` lives here.  It removes device config entries in
bulk and is deliberately hard to call by accident: admin-only, a mandatory
`filter` and an explicit `confirm`.  Account entries are never touched.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import voluptuous as vol
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse
    from homeassistant.util.json import JsonValueType

_LOGGER = logging.getLogger(__name__)

SERVICE_DELETE_DEVICES = "delete_devices"

# Entry types as stored in `entry.data["type"]`.  Kept here as plain strings
# because the entry-type constants are owned by other modules.
_TYPE_STATIC = "device_static"
_TYPE_ROLLING_DERIVED = "device_rolling_derived"
_TYPE_ROLLING_PRE_GENERATED = "device_rolling_pre_generated"
_DEVICE_TYPE_PREFIX = "device_"

# Filter value -> entry types it matches.  `all` is handled by prefix instead
# so a future device type is not silently skipped; account entries never use
# the prefix.
_FILTER_TYPES: dict[str, frozenset[str]] = {
    "static": frozenset({_TYPE_STATIC}),
    "rolling": frozenset({_TYPE_ROLLING_DERIVED, _TYPE_ROLLING_PRE_GENERATED}),
    "rolling_derived": frozenset({_TYPE_ROLLING_DERIVED}),
    "rolling_pre_generated": frozenset({_TYPE_ROLLING_PRE_GENERATED}),
}
FILTER_ALL = "all"
FILTER_OPTIONS = [FILTER_ALL, *_FILTER_TYPES]

# No default for `filter` and `confirm`: a bare call must fail instead of
# wiping every device (the removed key files cannot be re-obtained easily).
DELETE_SCHEMA = vol.Schema(
    {
        vol.Required("filter"): vol.In(FILTER_OPTIONS),
        vol.Required("confirm"): cv.boolean,
    },
)


def _matches(entry_type: object, filter_type: str) -> bool:
    if not isinstance(entry_type, str) or not entry_type.startswith(_DEVICE_TYPE_PREFIX):
        return False
    if filter_type == FILTER_ALL:
        return True
    return entry_type in _FILTER_TYPES[filter_type]


async def _async_delete_devices(call: ServiceCall) -> ServiceResponse:
    """Remove FindMy device config entries and report what was removed.

    Never touches account entries.  Required fields:

    - `filter`: which device entries to remove
        - `all`: every device entry (static and both rolling kinds)
        - `static`: only `device_static` entries
        - `rolling`: both `device_rolling_derived` and `device_rolling_pre_generated`
        - `rolling_derived` / `rolling_pre_generated`: just that one kind
    - `confirm`: must be `true`

    Returns the number and titles of the removed entries.  Matching nothing is
    not an error; the response then reports a count of zero.
    """
    if call.data["confirm"] is not True:
        msg = "delete_devices permanently removes device entries; set `confirm` to true to proceed"
        raise ServiceValidationError(msg)

    hass = call.hass
    filter_type = cast("str", call.data["filter"])

    # Collect first: removing entries while iterating the live list is unsafe.
    matching = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if _matches((entry.data or {}).get("type"), filter_type)
    ]

    removed: list[JsonValueType] = []
    for entry in matching:
        _LOGGER.info("delete_devices: removing entry %s (%s)", entry.title, entry.entry_id)
        _ = await hass.config_entries.async_remove(entry.entry_id)
        removed.append({"entry_id": entry.entry_id, "title": entry.title})

    _LOGGER.info("delete_devices: removed %d entries (filter=%s)", len(removed), filter_type)
    response: ServiceResponse = {"count": len(removed), "removed": removed}
    return response


def async_register(hass: HomeAssistant) -> None:
    """Register all services once - idempotent."""
    if hass.services.has_service(DOMAIN, SERVICE_DELETE_DEVICES):
        return

    # Admin service: the generic `call_service` WebSocket command does not
    # check permissions, only this wrapper does.
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_DELETE_DEVICES,
        _async_delete_devices,
        schema=DELETE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
