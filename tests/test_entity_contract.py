"""The ids, classes and attribute keys of every entity: a tripwire for accidental renames.

Users have automations, scripts and dashboards on these entities, so what is pinned here is a
compatibility contract with the original integration (malmeloo/hass-FindMy). Every value is a
literal on purpose: a test that computed its expectations the way the code does would follow
the code into any rename. A change here needs a migration or a note for users.

Known and intended differences from the original integration:

* The latitude and longitude sensors have no ``state_class`` (sensor.py says why).
* The tracker has no ``battery_level`` attribute: Home Assistant deprecated overriding the
  property. The battery sensors carry the value (see test_tracker_battery.py).

One entry of each device type and one account entry are set up through Home Assistant's real
entity platforms (ha_platforms.py). The key material is fixed, because the unique id of a static
key is derived from it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from findmy import FindMyAccessory, FixedRollingKeyPairAccessory, KeyPair, KeyPairType
from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .conftest import make_account_state, make_config_entry
from .ha_platforms import (
    LibraryAccounts,
    VirtualClock,
    answer_with,
    async_setup_findmy,
    make_location_report,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.requires_bluetooth

STATIC_UNIQUE_ID = "/vOKuaTt4YQhx94Ge/rHR9KDiKnuhcZ7CrxlJFsRUzg="
ROLLING_UNIQUE_ID = "ROLLING-1"
FIXED_UNIQUE_ID = "FIXED-1"
ACCOUNT_EMAIL = "jane.doe@example.com"

DIAGNOSTIC = EntityCategory.DIAGNOSTIC

# Attribute keys Home Assistant adds to every state of a kind of entity.
_TRACKER_BASE = {
    "friendly_name",
    "source_type",
    "in_zones",
    "gps_accuracy",
    "latitude",
    "longitude",
    "tracking_type",
}
_TRACKER = _TRACKER_BASE | {"detected_at", "status", "mac_address"}
_ROLLING_TRACKER = _TRACKER | {
    "alignment_index",
    "alignment_date",
    "local_key_candidates",
    "local_detected_at",
    "local_rssi",
    "local_source",
    "local_state",
    "local_battery",
}
_PRESENCE = {
    "friendly_name",
    "device_class",
    "last_seen",
    "rssi",
    "source",
    "mac_address",
    "local_state",
    "battery",
    "max_gap",
    "away_timeout",
}
_COORDINATE = {"friendly_name", "unit_of_measurement"}
_MEASUREMENT = {"friendly_name", "device_class", "unit_of_measurement", "state_class"}


def _device_entities(
    platform_id: str, unique_id: str, name: str, *, local: bool
) -> dict[str, tuple[Any, ...]]:
    """Expected registry entries of one device, by entity id.

    Each value: unique id, device identifier, entity name, device class, unit, state class,
    options, entity category, enabled by default, attribute keys of the state.
    """
    entities: dict[str, tuple[Any, ...]] = {
        f"device_tracker.findmy_{platform_id}": (
            unique_id,
            unique_id,
            None,
            None,
            None,
            None,
            None,
            DIAGNOSTIC,
            True,
            _ROLLING_TRACKER if local else _TRACKER,
        ),
        f"sensor.{platform_id}_latitude": (
            f"{unique_id}_latitude",
            unique_id,
            "Latitude",
            None,
            "°",
            None,
            None,
            None,
            True,
            _COORDINATE,
        ),
        f"sensor.{platform_id}_longitude": (
            f"{unique_id}_longitude",
            unique_id,
            "Longitude",
            None,
            "°",
            None,
            None,
            None,
            True,
            _COORDINATE,
        ),
        f"sensor.{platform_id}_position": (
            f"{unique_id}_position",
            unique_id,
            "Position",
            None,
            None,
            None,
            None,
            None,
            True,
            {"friendly_name"},
        ),
        f"sensor.{platform_id}_battery_level": (
            f"{unique_id}_battery_level",
            unique_id,
            "Battery level",
            "enum",
            None,
            None,
            ["ok", "medium", "low", "critical"],
            None,
            True,
            {"friendly_name", "device_class", "options"},
        ),
        f"sensor.{platform_id}_battery": (
            f"{unique_id}_battery_percent",
            unique_id,
            "Battery",
            "battery",
            "%",
            "measurement",
            None,
            None,
            True,
            _MEASUREMENT,
        ),
        f"sensor.{platform_id}_battery_voltage": (
            f"{unique_id}_battery_voltage",
            unique_id,
            "Battery voltage",
            "voltage",
            "mV",
            "measurement",
            None,
            None,
            False,
            _MEASUREMENT,
        ),
        f"sensor.{platform_id}_status_counter": (
            f"{unique_id}_status_counter",
            unique_id,
            "Status counter",
            None,
            None,
            "measurement",
            None,
            None,
            False,
            {"friendly_name", "state_class"},
        ),
        f"binary_sensor.{platform_id}_battery_low": (
            f"{unique_id}_battery_low",
            unique_id,
            "Battery low",
            "battery",
            None,
            None,
            None,
            None,
            True,
            {"friendly_name", "device_class"},
        ),
    }
    if local:
        entities[f"binary_sensor.{platform_id}_bluetooth_presence"] = (
            f"{unique_id}_present",
            unique_id,
            "Bluetooth presence",
            "presence",
            None,
            None,
            None,
            None,
            True,
            _PRESENCE,
        )
        entities[f"sensor.{platform_id}_signal_strength"] = (
            f"{unique_id}_signal_strength",
            unique_id,
            "Signal strength",
            "signal_strength",
            "dBm",
            "measurement",
            None,
            DIAGNOSTIC,
            True,
            _MEASUREMENT,
        )
    return entities


def _account_entities() -> dict[str, tuple[Any, ...]]:
    prefix = "sensor.apple_account_jane_doe"
    unique = f"apple_account_{ACCOUNT_EMAIL}"
    timestamp = {"friendly_name", "device_class"}
    return {
        f"{prefix}_state": (
            f"{unique}_state",
            "jane.doe",
            "State",
            "enum",
            None,
            None,
            None,
            None,
            True,
            {"friendly_name", "device_class"},
        ),
        f"{prefix}_last_fetch": (
            f"{unique}_last_fetch",
            "jane.doe",
            "Last fetch",
            "timestamp",
            None,
            None,
            None,
            None,
            True,
            timestamp,
        ),
        f"{prefix}_last_fetch_result": (
            f"{unique}_last_fetch_result",
            "jane.doe",
            "Last fetch result",
            "enum",
            None,
            None,
            None,
            None,
            True,
            {"friendly_name", "device_class"},
        ),
        f"{prefix}_last_fetch_duration": (
            f"{unique}_last_fetch_duration",
            "jane.doe",
            "Last fetch duration",
            "duration",
            "s",
            "measurement",
            None,
            None,
            True,
            _MEASUREMENT,
        ),
        f"{prefix}_next_fetch": (
            f"{unique}_next_fetch",
            "jane.doe",
            "Next fetch",
            "timestamp",
            None,
            None,
            None,
            None,
            True,
            timestamp,
        ),
    }


EXPECTED_ENTITIES = {
    **_device_entities("static_tag", STATIC_UNIQUE_ID, "Static Tag", local=False),
    **_device_entities("rolling_tag", ROLLING_UNIQUE_ID, "Rolling Tag", local=True),
    **_device_entities("fixed_tag", FIXED_UNIQUE_ID, "Fixed Tag", local=False),
    **_account_entities(),
}

EXPECTED_DEVICES = {
    frozenset({("findmy", STATIC_UNIQUE_ID)}): "Static Tag",
    frozenset({("findmy", ROLLING_UNIQUE_ID)}): "Rolling Tag",
    frozenset({("findmy", FIXED_UNIQUE_ID)}): "Fixed Tag",
    frozenset({("findmy", "jane.doe")}): "Apple: Jane Doe <jane.doe@example.com>",
}


@pytest.fixture
async def contract_hass(
    platform_hass: HomeAssistant, library_accounts: LibraryAccounts
) -> HomeAssistant:
    """One entry of each kind, set up with a location report for every device."""
    hass = platform_hass
    now = datetime.now(UTC)
    static = KeyPair(bytes(range(1, 29)), name="Static Tag")
    rolling = FindMyAccessory(
        master_key=bytes(range(28)),
        skn=bytes(range(32)),
        sks=bytes(range(32, 64)),
        paired_at=now - timedelta(days=2),
        name="Rolling Tag",
        identifier=ROLLING_UNIQUE_ID,
        alignment_date=now,
        alignment_index=192,
    )
    fixed_key = bytes(range(2, 30))
    fixed = FixedRollingKeyPairAccessory(
        private_keys=[fixed_key], name="Fixed Tag", identifier=FIXED_UNIQUE_ID
    )
    make_config_entry(hass, {"type": "device_static", "data": static.to_json()})
    make_config_entry(hass, {"type": "device_rolling_derived", "data": rolling.to_json()})
    make_config_entry(hass, {"type": "device_rolling_pre_generated", "data": fixed.to_json()})
    make_config_entry(
        hass, {"type": "account", "account_data": make_account_state(email=ACCOUNT_EMAIL)}
    )
    storage = await async_setup_findmy(hass)

    rolling_key = next(k for k in rolling.keys_at(192) if k.key_type == KeyPairType.PRIMARY)
    keys = {
        storage.get_entry(entry): key
        for entry, key in zip(
            hass.config_entries.async_entries("findmy")[:3],
            (static, rolling_key, KeyPair(fixed_key)),
            strict=True,
        )
    }
    reports = {
        device: make_location_report(
            key,
            timestamp=now - timedelta(minutes=5),
            latitude=52.52,
            longitude=13.405,
            horizontal_accuracy=20,
            status=0x40,
        )
        for device, key in keys.items()
    }
    library_accounts.by_email[ACCOUNT_EMAIL].fetch_location.side_effect = answer_with(reports)
    await storage.coordinator.async_refresh()
    await hass.async_block_till_done()
    return hass


def _registry_contract(hass: HomeAssistant) -> dict[str, tuple[Any, ...]]:
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    contract: dict[str, tuple[Any, ...]] = {}
    for entry in registry.entities.values():
        device = devices.async_get(entry.device_id) if entry.device_id else None
        assert device is not None, entry.entity_id
        ((_domain, identifier),) = device.identifiers
        capabilities = entry.capabilities or {}
        state_class = capabilities.get("state_class")
        contract[entry.entity_id] = (
            entry.unique_id,
            identifier,
            entry.original_name,
            entry.original_device_class,
            entry.unit_of_measurement,
            None if state_class is None else str(state_class),
            capabilities.get("options"),
            entry.entity_category,
            entry.disabled_by is None,
        )
    return contract


async def test_registry_entries_keep_their_ids_and_classes(contract_hass: HomeAssistant) -> None:
    expected = {entity_id: values[:9] for entity_id, values in EXPECTED_ENTITIES.items()}

    assert _registry_contract(contract_hass) == expected


async def test_devices_keep_their_identifiers_and_names(contract_hass: HomeAssistant) -> None:
    devices = {
        frozenset(device.identifiers): device.name for device in dr.async_get(contract_hass).devices
    }

    assert devices == EXPECTED_DEVICES


async def test_states_keep_their_attribute_keys(
    contract_hass: HomeAssistant, virtual_clock: VirtualClock
) -> None:
    hass = contract_hass
    registry = er.async_get(hass)
    # The opt-in sensors have no state until they are enabled. Home Assistant then reloads
    # their entries, 30 seconds later.
    for entry in list(registry.entities.values()):
        if entry.disabled_by is not None:
            registry.async_update_entity(entry.entity_id, disabled_by=None)
    await virtual_clock.async_advance(hass, timedelta(seconds=31))
    assert all(state.state for state in hass.states.async_all())
    # The presence sensor writes its attributes with its first refresh.
    await virtual_clock.async_advance(hass, timedelta(seconds=30))

    # Without zone.home, which device_tracker sets up.
    keys = {
        state.entity_id: {str(key) for key in state.attributes}
        for state in hass.states.async_all()
        if state.entity_id in registry.entities
    }

    assert keys == {entity_id: values[9] for entity_id, values in EXPECTED_ENTITIES.items()}


async def test_states_show_the_report(contract_hass: HomeAssistant) -> None:
    """The values behind the keys, for one device; the other tests cover the rest."""
    tracker = contract_hass.states.get("device_tracker.findmy_static_tag")
    assert tracker is not None
    assert tracker.state == "not_home"
    assert tracker.attributes["latitude"] == pytest.approx(52.52)
    assert tracker.attributes["longitude"] == pytest.approx(13.405)
    assert tracker.attributes["gps_accuracy"] == 20
    assert tracker.attributes["status"] == 0x40
    assert tracker.attributes["mac_address"] == "E2:7B:7C:0B:3A:2F"
    assert "battery_level" not in tracker.attributes

    states = {
        suffix: contract_hass.states.get(f"sensor.static_tag_{suffix}")
        for suffix in ("latitude", "longitude", "position", "battery_level", "battery")
    }
    assert {suffix: state.state if state else None for suffix, state in states.items()} == {
        "latitude": "52.52",
        "longitude": "13.405",
        "position": "52.520000,13.405000",
        "battery_level": "medium",
        "battery": "65",
    }
