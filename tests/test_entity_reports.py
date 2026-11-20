"""Location reports, as the entities show them in Home Assistant's state machine.

Entries are set up through the real entity platforms (ha_platforms.py); the reports are
encrypted to the device's key and decrypted the way FindMy.py does it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiohttp
import pytest
from homeassistant.const import STATE_UNKNOWN
from homeassistant.helpers.storage import Store

from custom_components.findmy.last_reports import STORAGE_KEY, STORAGE_VERSION, RestoredReport

from .conftest import make_account_state, make_config_entry
from .ha_platforms import (
    FakeBluetooth,
    LibraryAccounts,
    answer_with,
    async_setup_findmy,
    dult_advertisement,
    get_state,
    make_location_report,
    primary_key,
    separated_advertisement,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import AccessoryFactory, FetchClock, KeyPairFactory

pytestmark = pytest.mark.requires_bluetooth

EMAIL = "jane@example.com"
# Battery bits 0b10 ("low"), counter 5.
LOW_BATTERY = 0b1000_0101


def _report_values(hass: HomeAssistant, name: str) -> dict[str, str]:
    return {
        suffix: get_state(hass, f"sensor.{name}_{suffix}").state
        for suffix in ("latitude", "longitude", "position", "battery_level", "battery")
    } | {"battery_low": get_state(hass, f"binary_sensor.{name}_battery_low").state}


def _account_entry(hass: HomeAssistant) -> None:
    make_config_entry(hass, {"type": "account", "account_data": make_account_state(email=EMAIL)})


async def test_a_fetched_report_reaches_tracker_and_sensors(
    platform_hass: HomeAssistant,
    library_accounts: LibraryAccounts,
    keypair_factory: KeyPairFactory,
) -> None:
    hass = platform_hass
    key = keypair_factory(name="Keys")
    make_config_entry(hass, {"type": "device_static", "data": key.to_json()})
    _account_entry(hass)
    storage = await async_setup_findmy(hass)
    # Nothing is known before the first fetch.
    assert get_state(hass, "device_tracker.findmy_keys").state == STATE_UNKNOWN
    assert set(_report_values(hass, "keys").values()) == {STATE_UNKNOWN}

    seen = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=3)
    report = make_location_report(
        key,
        timestamp=seen,
        latitude=48.137154,
        longitude=11.576124,
        horizontal_accuracy=35,
        status=LOW_BATTERY,
    )
    device = storage.get_entry(hass.config_entries.async_entries("findmy")[0])
    library_accounts.by_email[EMAIL].fetch_location.side_effect = answer_with({device: report})
    await storage.coordinator.async_refresh()
    await hass.async_block_till_done()

    tracker = get_state(hass, "device_tracker.findmy_keys")
    assert tracker.state == "not_home"
    assert tracker.attributes["latitude"] == pytest.approx(48.137154)
    assert tracker.attributes["longitude"] == pytest.approx(11.576124)
    assert tracker.attributes["gps_accuracy"] == 35
    assert tracker.attributes["source_type"] == "gps"
    assert tracker.attributes["detected_at"] == seen
    assert tracker.attributes["status"] == LOW_BATTERY
    assert tracker.attributes["mac_address"] == key.mac_address
    assert _report_values(hass, "keys") == {
        "latitude": "48.137154",
        "longitude": "11.576124",
        "position": "48.137154,11.576124",
        "battery_level": "low",
        "battery": "40",
        "battery_low": "on",
    }


async def test_a_report_at_home_puts_the_tracker_home(
    platform_hass: HomeAssistant,
    library_accounts: LibraryAccounts,
    keypair_factory: KeyPairFactory,
) -> None:
    """The state comes from Home Assistant's zones, which only work with a real tracker."""
    hass = platform_hass
    key = keypair_factory(name="Keys")
    make_config_entry(hass, {"type": "device_static", "data": key.to_json()})
    _account_entry(hass)
    storage = await async_setup_findmy(hass)
    report = make_location_report(
        key,
        timestamp=datetime.now(UTC),
        latitude=hass.config.latitude,
        longitude=hass.config.longitude,
        horizontal_accuracy=10,
        status=0,
    )
    device = storage.get_entry(hass.config_entries.async_entries("findmy")[0])
    library_accounts.by_email[EMAIL].fetch_location.side_effect = answer_with({device: report})

    await storage.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert get_state(hass, "device_tracker.findmy_keys").state == "home"


async def test_the_restored_report_is_shown_before_any_fetch(
    platform_hass: HomeAssistant,
    library_accounts: LibraryAccounts,
    accessory_factory: AccessoryFactory,
) -> None:
    hass = platform_hass
    seen = datetime(2026, 11, 18, 17, 45, tzinfo=UTC)
    restored = RestoredReport(
        timestamp=seen, latitude=50.1, longitude=8.68, horizontal_accuracy=12, status=0x40
    )
    store: Store[dict[str, object]] = Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True)
    await store.async_save({"reports": {"bag-entry": restored.as_dict()}})
    accessory = accessory_factory(name="Bag")
    make_config_entry(
        hass,
        {"type": "device_rolling_derived", "data": accessory.to_json()},
        entry_id="bag-entry",
    )
    _account_entry(hass)

    await async_setup_findmy(hass)

    library_accounts.by_email[EMAIL].fetch_location.assert_not_awaited()
    tracker = get_state(hass, "device_tracker.findmy_bag")
    assert tracker.state == "not_home"
    assert tracker.attributes["latitude"] == pytest.approx(50.1)
    assert tracker.attributes["detected_at"] == seen
    assert tracker.attributes["status"] == 0x40
    assert _report_values(hass, "bag") == {
        "latitude": "50.1",
        "longitude": "8.68",
        "position": "50.100000,8.680000",
        "battery_level": "medium",
        "battery": "65",
        "battery_low": "off",
    }


async def test_a_failed_fetch_changes_no_state_of_any_device(  # noqa: PLR0917
    platform_hass: HomeAssistant,
    library_accounts: LibraryAccounts,
    fake_bluetooth: FakeBluetooth,
    fetch_clock: FetchClock,
    keypair_factory: KeyPairFactory,
    accessory_factory: AccessoryFactory,
) -> None:
    """A report or a local sighting stays valid after a failed fetch; the rest stays unknown.

    A fetch says something about the account, not about a device. Replaces the test of the
    rule that a device nothing is known about goes unavailable while fetches fail.
    """
    hass = platform_hass
    located = keypair_factory(name="Located")
    unknown = keypair_factory(name="Unknown")
    heard = accessory_factory(name="Heard")
    dult_only = accessory_factory(name="Dult")
    for data in (
        {"type": "device_static", "data": located.to_json()},
        {"type": "device_static", "data": unknown.to_json()},
        {"type": "device_rolling_derived", "data": heard.to_json()},
        {"type": "device_rolling_derived", "data": dult_only.to_json()},
    ):
        make_config_entry(hass, data)
    _account_entry(hass)
    storage = await async_setup_findmy(hass)
    located_device = storage.get_entry(hass.config_entries.async_entries("findmy")[0])
    report = make_location_report(
        located,
        timestamp=datetime.now(UTC),
        latitude=52.5,
        longitude=13.4,
        horizontal_accuracy=20,
        status=0x40,
    )
    library_accounts.by_email[EMAIL].fetch_location.side_effect = answer_with(
        {located_device: report}
    )
    await storage.coordinator.async_refresh()
    index = 2 * 96
    heard_key = primary_key(heard, index)
    fake_bluetooth.advertise(
        heard_key.mac_address, manufacturer_data=separated_advertisement(heard_key, LOW_BATTERY)
    )
    fake_bluetooth.advertise(
        primary_key(dult_only, index).mac_address,
        service_data=dult_advertisement(near_owner=True),
    )
    await hass.async_block_till_done()

    library_accounts.by_email[EMAIL].fetch_location.side_effect = aiohttp.ClientConnectionError()
    # An account fetches at most once per 15 minutes.
    fetch_clock.advance()
    await storage.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert storage.coordinator.last_update_success is False

    assert get_state(hass, "device_tracker.findmy_located").state == "not_home"
    assert _report_values(hass, "located")["latitude"] == "52.5"
    assert _report_values(hass, "located")["battery"] == "65"

    assert get_state(hass, "device_tracker.findmy_unknown").state == STATE_UNKNOWN
    assert set(_report_values(hass, "unknown").values()) == {STATE_UNKNOWN}

    # Heard locally: the battery is there, a position is not.
    assert get_state(hass, "device_tracker.findmy_heard").state == STATE_UNKNOWN
    assert _report_values(hass, "heard") == {
        "latitude": STATE_UNKNOWN,
        "longitude": STATE_UNKNOWN,
        "position": STATE_UNKNOWN,
        "battery_level": "low",
        "battery": "40",
        "battery_low": "on",
    }
    # DULT advertisements carry no battery state: unknown.
    assert get_state(hass, "device_tracker.findmy_dult").state == STATE_UNKNOWN
    assert _report_values(hass, "dult") == {
        "latitude": STATE_UNKNOWN,
        "longitude": STATE_UNKNOWN,
        "position": STATE_UNKNOWN,
        "battery_level": STATE_UNKNOWN,
        "battery": STATE_UNKNOWN,
        "battery_low": STATE_UNKNOWN,
    }

    # The next fetch that works brings everything back.
    library_accounts.by_email[EMAIL].fetch_location.side_effect = answer_with({})
    fetch_clock.advance()
    await storage.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert get_state(hass, "device_tracker.findmy_unknown").state == STATE_UNKNOWN
    assert set(_report_values(hass, "unknown").values()) == {STATE_UNKNOWN}
