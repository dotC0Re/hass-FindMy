"""A failed fetch does not take away what is known about a device.

``CoordinatorEntity`` ties availability to the success of the latest refresh. For this
integration that meant: Apple unreachable, a timeout or an expired session made every tracker
unavailable, including those heard locally through Bluetooth and those with a known location.
The state of the fetch belongs to the account sensors; a device stays available while anything
is known about it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiohttp
import pytest

from custom_components.findmy._entity import (
    device_unique_id,
    report_available,
    status_available,
)
from custom_components.findmy.const import DOMAIN
from custom_components.findmy.last_reports import RestoredReport
from custom_components.findmy.local_bluetooth import LocalObservation
from custom_components.findmy.storage import RuntimeStorage

from .conftest import KeyPairFactory, library_mock, open_account

if TYPE_CHECKING:
    from findmy import KeyPair
    from homeassistant.core import HomeAssistant

SEEN_AT = datetime(2026, 11, 19, 8, 30, tzinfo=UTC)
REPORT = RestoredReport(
    timestamp=SEEN_AT, latitude=52.5, longitude=13.4, horizontal_accuracy=35, status=0x40
)


def observation(*, battery_level: str | None) -> LocalObservation:
    return LocalObservation(
        mac_address="C0:11:22:33:44:55",
        detected_at=SEEN_AT,
        rssi=-60,
        state="nearby" if battery_level else "near_owner",
        battery_level=battery_level,
        status=0x40,
        key_index=5,
        can_align=True,
    )


async def storage_with_failed_fetch(
    hass: HomeAssistant, device: KeyPair, *, known: RestoredReport | None
) -> RuntimeStorage:
    """A storage whose only account just failed to reach Apple."""
    storage = RuntimeStorage(hass)
    hass.data[DOMAIN] = storage
    account = await open_account(storage, "account")
    library_mock(account).fetch_location.side_effect = aiohttp.ClientConnectionError("no route")
    coordinator = storage.coordinator
    _ = coordinator.async_add_listener(lambda: None, device)
    if known is not None:
        coordinator.data = {device: known}

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    return storage


async def test_device_with_a_known_location_stays_available(
    hass: HomeAssistant, keypair_factory: KeyPairFactory
) -> None:
    device = keypair_factory()
    storage = await storage_with_failed_fetch(hass, device, known=REPORT)

    assert report_available(storage.coordinator, device) is True
    assert status_available(hass, storage.coordinator, device) is True
    # The failed fetch left the report where it was.
    assert storage.coordinator.data == {device: REPORT}


async def test_device_nothing_is_known_about_is_unavailable_while_fetches_fail(
    hass: HomeAssistant, keypair_factory: KeyPairFactory
) -> None:
    device = keypair_factory()
    storage = await storage_with_failed_fetch(hass, device, known=None)

    assert report_available(storage.coordinator, device) is False
    assert status_available(hass, storage.coordinator, device) is False


@pytest.mark.parametrize("battery_level", ["Full", None], ids=["offline finding", "dult"])
async def test_status_entities_are_available_for_an_accessory_heard_locally(
    hass: HomeAssistant, keypair_factory: KeyPairFactory, battery_level: str | None
) -> None:
    """Also when only DULT advertisements arrive, which carry no status byte."""
    device = keypair_factory()
    storage = await storage_with_failed_fetch(hass, device, known=None)
    heard = observation(battery_level=battery_level)
    unique_id = device_unique_id(device)

    storage.local_observations[unique_id] = (heard, "proxy")
    if battery_level is not None:
        storage.local_status[unique_id] = (heard.status, heard.detected_at)

    assert status_available(hass, storage.coordinator, device) is True
    # The location entities have nothing to show for it: a local sighting is no position.
    assert report_available(storage.coordinator, device) is False


@pytest.mark.requires_bluetooth
async def test_tracker_stays_available_with_a_location_or_a_local_sighting(
    hass: HomeAssistant, keypair_factory: KeyPairFactory
) -> None:
    from custom_components.findmy import device_tracker  # noqa: PLC0415  # needs Bluetooth

    located, heard, unknown = keypair_factory(), keypair_factory(), keypair_factory()
    storage = await storage_with_failed_fetch(hass, located, known=REPORT)
    coordinator = storage.coordinator

    with_location = device_tracker.FindMyDeviceTracker(coordinator, located, "entry-1")
    heard_locally = device_tracker.FindMyDeviceTracker(coordinator, heard, "entry-2")
    heard_locally._local_observation = observation(battery_level=None)
    nothing_known = device_tracker.FindMyDeviceTracker(coordinator, unknown, "entry-3")

    assert with_location.available is True
    assert with_location.latitude == pytest.approx(52.5)
    assert heard_locally.available is True
    assert nothing_known.available is False


@pytest.mark.requires_bluetooth
async def test_location_and_battery_sensors_stay_available_after_a_failed_fetch(
    hass: HomeAssistant, keypair_factory: KeyPairFactory
) -> None:
    from custom_components.findmy import binary_sensor, sensor  # noqa: PLC0415  # needs Bluetooth

    device = keypair_factory()
    storage = await storage_with_failed_fetch(hass, device, known=REPORT)
    coordinator = storage.coordinator

    latitude = sensor.FindMyLatitudeSensor(coordinator, device)
    battery = sensor.FindMyBatteryLevelSensor(coordinator, device)
    battery_low = binary_sensor.FindMyBatteryLowBinarySensor(coordinator, device)
    for entity in (latitude, battery, battery_low):
        entity.hass = hass

    assert latitude.available is True
    assert latitude.native_value == pytest.approx(52.5)
    assert latitude._compute_available() is True
    assert battery.available is True
    assert battery._compute_available() is True
    assert battery_low.available is True
