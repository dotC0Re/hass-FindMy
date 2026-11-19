"""The tracker no longer overrides Home Assistant's deprecated ``battery_level`` property.

Home Assistant logs a warning for every tracker class that overrides it, when the class is
defined (``BaseTrackerEntity.__init_subclass__``), and drops the property in 2027.7. The
tracker's state therefore has no ``battery_level`` attribute any more; the battery sensors of
the device carry the value.
"""

from __future__ import annotations

import importlib.util
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, override

import pytest
from homeassistant.components.device_tracker.entity import TrackerEntity

from custom_components.findmy import device_tracker

from .conftest import make_account_state, make_config_entry
from .ha_platforms import answer_with, async_setup_findmy, get_state, make_location_report

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import KeyPairFactory
    from .ha_platforms import LibraryAccounts

pytestmark = pytest.mark.requires_bluetooth

_TRACKER_LOGGER = "homeassistant.components.device_tracker.entity"


def _deprecation_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and "deprecated" in record.getMessage()
    ]


def test_the_warning_is_noticed_for_a_class_that_overrides_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The check below would see the warning: Home Assistant logs it for custom code."""
    with caplog.at_level(logging.WARNING, logger=_TRACKER_LOGGER):

        class _Overriding(TrackerEntity):
            __module__ = "custom_components.example.device_tracker"

            @property
            @override
            def battery_level(self) -> int | None:  # pyright: ignore[reportIncompatibleVariableOverride]
                return 50

    assert _Overriding is not None
    assert any("battery_level" in message for message in _deprecation_warnings(caplog))


def test_defining_the_tracker_class_logs_no_deprecation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defines the class again, as importing the module at startup does."""
    assert "battery_level" not in device_tracker.FindMyDeviceTracker.__dict__
    spec = importlib.util.spec_from_file_location(
        "custom_components.findmy._device_tracker_again", device_tracker.__file__
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    with caplog.at_level(logging.WARNING):
        spec.loader.exec_module(module)

    assert module.FindMyDeviceTracker is not device_tracker.FindMyDeviceTracker
    assert _deprecation_warnings(caplog) == []


async def test_the_battery_reaches_the_sensors_not_the_tracker(
    platform_hass: HomeAssistant,
    library_accounts: LibraryAccounts,
    keypair_factory: KeyPairFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hass = platform_hass
    key = keypair_factory(name="Keys")
    make_config_entry(hass, {"type": "device_static", "data": key.to_json()})
    make_config_entry(
        hass, {"type": "account", "account_data": make_account_state(email="a@example.com")}
    )
    with caplog.at_level(logging.WARNING):
        storage = await async_setup_findmy(hass)
        device = storage.get_entry(hass.config_entries.async_entries("findmy")[0])
        report = make_location_report(
            key,
            timestamp=datetime.now(UTC),
            latitude=1.0,
            longitude=2.0,
            horizontal_accuracy=10,
            # Battery bits 0b10: "low", about 40 %.
            status=0b1000_0000,
        )
        library_accounts.by_email["a@example.com"].fetch_location.side_effect = answer_with(
            {device: report}
        )
        await storage.coordinator.async_refresh()
        await hass.async_block_till_done()

    assert _deprecation_warnings(caplog) == []
    tracker = get_state(hass, "device_tracker.findmy_keys")
    assert tracker.attributes["status"] == 0b1000_0000
    assert "battery_level" not in tracker.attributes
    assert "battery" not in tracker.attributes
    # The value the tracker's attribute had, and the same level in other forms.
    assert get_state(hass, "sensor.keys_battery").state == "40"
    assert get_state(hass, "sensor.keys_battery").attributes["unit_of_measurement"] == "%"
    assert get_state(hass, "sensor.keys_battery_level").state == "low"
    assert get_state(hass, "binary_sensor.keys_battery_low").state == "on"
