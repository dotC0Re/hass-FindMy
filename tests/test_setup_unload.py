# pyright: basic
"""Tests for unloading config entries.

Only the bookkeeping around the shared coordinator is covered. Forwarding to the entity platforms
is mocked, because the device platforms import Home Assistant's Bluetooth integration, which
cannot be imported on Windows.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from findmy import AsyncAppleAccount, KeyPair
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

from custom_components.findmy import async_unload_entry
from custom_components.findmy.storage import RuntimeStorage

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


def _noop(*_args: object, **_kwargs: object) -> None:
    return None


@pytest_asyncio.fixture
async def hass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[HomeAssistant]:
    """A bare Home Assistant core with a mocked config entry registry."""
    instance = HomeAssistant(str(tmp_path))
    frame.async_setup(instance)
    # See test_coordinator.py: the frame helper raises for custom integrations on Windows.
    monkeypatch.setattr(frame, "report_usage", _noop)
    instance.config_entries = MagicMock()
    instance.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    yield instance
    await instance.async_stop(force=True)


def add_account(storage: RuntimeStorage, entry_id: str) -> MagicMock:
    account = MagicMock(spec=AsyncAppleAccount)
    account.account_name = f"{entry_id}@example.com"
    storage._entries[entry_id] = account
    return account


@pytest.mark.asyncio
async def test_unloading_an_account_restores_the_interval_of_the_remaining_one(
    hass: HomeAssistant,
) -> None:
    """R3: the interval stayed at 7:30 minutes after one of two accounts was removed."""
    storage = RuntimeStorage.attach(hass)
    add_account(storage, "a")
    removed = add_account(storage, "b")
    await storage.coordinator.reload()
    assert storage.coordinator.update_interval == timedelta(minutes=7, seconds=30)

    assert await async_unload_entry(hass, MagicMock(entry_id="b")) is True

    removed.close.assert_awaited_once()
    assert storage.accounts == [storage._entries["a"]]
    assert storage.coordinator.update_interval == timedelta(minutes=15)


@pytest.mark.asyncio
async def test_unloading_the_last_account_disables_the_schedule(hass: HomeAssistant) -> None:
    storage = RuntimeStorage.attach(hass)
    add_account(storage, "a")
    await storage.coordinator.reload()

    assert await async_unload_entry(hass, MagicMock(entry_id="a")) is True

    assert storage.coordinator.update_interval is None


@pytest.mark.asyncio
async def test_unloading_an_unknown_entry_fails_without_touching_the_interval(
    hass: HomeAssistant,
) -> None:
    storage = RuntimeStorage.attach(hass)
    add_account(storage, "a")
    await storage.coordinator.reload()

    assert await async_unload_entry(hass, MagicMock(entry_id="missing")) is False

    assert storage.coordinator.update_interval == timedelta(minutes=15)


def test_coordinate_sensors_do_not_feed_long_term_statistics() -> None:
    """S6: a state class turns the coordinates into statistics that are never purged."""
    # The sensor platform imports the Bluetooth integration, which is not importable on Windows.
    sensor = pytest.importorskip(
        "custom_components.findmy.sensor",
        reason="needs homeassistant.components.bluetooth",
        exc_type=ImportError,
    )

    coordinator = MagicMock(last_update_success=True)
    device = KeyPair.new()

    assert sensor.FindMyLatitudeSensor(coordinator, device, "entry").state_class is None
    assert sensor.FindMyLongitudeSensor(coordinator, device, "entry").state_class is None
    # Sensors that are meant to be graphed keep theirs.
    assert sensor.FindMyBatteryPercentSensor(coordinator, device, "entry").state_class is not None
