"""Tests for the sensors of an Apple account.

The sensors are built from an ``AccountRuntime``, plain data on Home Assistant's loop, instead
of the library account, which lives on the worker thread. Their ids must stay what they were.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from custom_components.findmy.const import DOMAIN
from custom_components.findmy.storage import RuntimeStorage

from .conftest import library_mock, make_mock_account, open_account

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# The sensor platform imports Home Assistant's Bluetooth integration.
pytestmark = pytest.mark.requires_bluetooth


async def test_account_sensors_keep_their_ids(hass: HomeAssistant) -> None:
    from custom_components.findmy import sensor  # noqa: PLC0415  # needs Bluetooth, see above

    storage = RuntimeStorage(hass)
    account = await open_account(
        storage,
        "entry-1",
        make_mock_account(email="jane.doe@example.com", first_name="Jane", last_name="Doe"),
    )

    sensors = {
        "state": sensor.FindMyAccountStateSensor(storage.coordinator, account),
        "last_fetch": sensor.FindMyAccountLastFetchSensor(storage.coordinator, account),
        "last_fetch_result": sensor.FindMyAccountLastFetchResultSensor(
            storage.coordinator, account
        ),
        "last_fetch_duration": sensor.FindMyAccountLastFetchDurationSensor(
            storage.coordinator, account
        ),
        "next_fetch": sensor.FindMyAccountNextFetchSensor(storage.coordinator, account),
    }

    for suffix, entity in sensors.items():
        assert entity.entity_id == f"sensor.apple_account_jane_doe_{suffix}"
        assert entity.unique_id == f"apple_account_jane.doe@example.com_{suffix}"
        assert entity.device_info == {
            "identifiers": {(DOMAIN, "jane.doe")},
            "name": "Apple: Jane Doe <jane.doe@example.com>",
        }


async def test_account_sensors_follow_the_fetch_of_their_account(hass: HomeAssistant) -> None:
    from custom_components.findmy import sensor  # noqa: PLC0415  # needs Bluetooth, see above

    storage = RuntimeStorage(hass)
    coordinator = storage.coordinator
    account = await open_account(storage, "entry-1")
    state = sensor.FindMyAccountStateSensor(coordinator, account)
    last_fetch = sensor.FindMyAccountLastFetchSensor(coordinator, account)
    result = sensor.FindMyAccountLastFetchResultSensor(coordinator, account)
    duration = sensor.FindMyAccountLastFetchDurationSensor(coordinator, account)
    next_fetch = sensor.FindMyAccountNextFetchSensor(coordinator, account)
    during_fetch: list[tuple[str | None, str | None]] = []

    async def fetch(_sources: object) -> dict[object, object]:
        # Read on the worker thread, which is fine for plain values in a test.
        during_fetch.append((state.native_value, result.native_value))
        return {}

    library_mock(account).fetch_location.side_effect = fetch
    await coordinator.reload()

    assert state.native_value == "idle"
    assert [entity.native_value for entity in (last_fetch, result, duration, next_fetch)] == [
        None,
        None,
        None,
        None,
    ]

    await coordinator.async_refresh()

    assert during_fetch == [("fetching", "ongoing")]
    assert state.native_value == "idle"
    assert result.native_value == "success"
    assert last_fetch.native_value == account.last_fetch
    assert last_fetch.native_value is not None
    assert next_fetch.native_value == last_fetch.native_value + timedelta(minutes=15)
    assert duration.native_value is not None
    assert 0 <= duration.native_value < 5

    library_mock(account).fetch_location.side_effect = TimeoutError()
    await coordinator.async_refresh()

    assert result.native_value == "error"
    assert state.native_value == "idle"
