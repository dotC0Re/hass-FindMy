"""Tests for setting up and unloading config entries.

Covers what `__init__.py` and the storage do around an entry. Forwarding to the entity platforms
is mocked: the platforms themselves are tested elsewhere, and the device platforms need Home
Assistant's Bluetooth integration. Accounts are restored by FindMy.py for real, which opens no
connection; nothing here talks to Apple.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from findmy import AsyncAppleAccount, KeyPair, LoginState
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError

from custom_components.findmy import (
    ACCOUNT_PLATFORMS,
    DEVICE_PLATFORMS,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.findmy.account import AccountRuntime
from custom_components.findmy.storage import RuntimeStorage

from .conftest import make_account_state, make_config_entry, worker_thread_id

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


@pytest.fixture
def storage(hass: HomeAssistant) -> RuntimeStorage:
    """The storage as `async_setup` attaches it; its worker stops with the ``hass`` fixture."""
    return RuntimeStorage.attach(hass)


@pytest.fixture
def platforms(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stands in for the entity platforms; records what is set up and unloaded, in order."""
    calls = MagicMock()
    calls.setup = AsyncMock(return_value=None)
    calls.unload = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", calls.setup)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", calls.unload)
    return calls


@pytest.fixture
def closed(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The threads on which library accounts were closed, one item per call."""
    threads: list[int] = []
    original = AsyncAppleAccount.close

    async def close(self: AsyncAppleAccount) -> None:
        threads.append(threading.get_ident())
        await original(self)

    monkeypatch.setattr(AsyncAppleAccount, "close", close)
    return threads


def account_entry(
    hass: HomeAssistant, email: str = "user@example.com", **state: Any
) -> ConfigEntry:
    data = {"type": "account", "account_data": make_account_state(email=email, **state)}
    return make_config_entry(hass, data)


def static_device_entry(hass: HomeAssistant) -> ConfigEntry:
    return make_config_entry(hass, {"type": "device_static", "data": KeyPair.new().to_json()})


async def test_account_entry_is_set_up_with_the_account_platforms(
    hass: HomeAssistant, storage: RuntimeStorage, platforms: MagicMock
) -> None:
    entry = account_entry(hass, "jane@example.com")

    assert await async_setup_entry(hass, entry) is True

    account = storage.get_entry(entry)
    assert isinstance(account, AccountRuntime)
    assert account.entry_id == entry.entry_id
    assert account.account_name == "jane@example.com"
    assert storage.accounts == [account]
    platforms.setup.assert_awaited_once_with(entry, ACCOUNT_PLATFORMS)
    assert storage.coordinator.update_interval == timedelta(minutes=15)


async def test_device_entry_is_set_up_with_the_device_platforms(
    hass: HomeAssistant, storage: RuntimeStorage, platforms: MagicMock
) -> None:
    entry = static_device_entry(hass)

    assert await async_setup_entry(hass, entry) is True

    assert isinstance(storage.get_entry(entry), KeyPair)
    platforms.setup.assert_awaited_once_with(entry, DEVICE_PLATFORMS)
    assert Platform.DEVICE_TRACKER in DEVICE_PLATFORMS
    # A device alone starts neither the worker thread nor the schedule.
    assert storage._worker is None
    assert storage.coordinator.update_interval is None


async def test_setup_of_damaged_account_data_fails_cleanly(
    hass: HomeAssistant, storage: RuntimeStorage, platforms: MagicMock
) -> None:
    """R10: a clear error instead of a traceback, and nothing left behind."""
    entry = make_config_entry(hass, {"type": "account", "account_data": {"type": "account"}})

    with pytest.raises(ConfigEntryError, match="cannot be restored"):
        _ = await async_setup_entry(hass, entry)

    assert storage._entries == {}
    assert storage.worker._stop_callbacks == {}
    platforms.setup.assert_not_awaited()
    assert storage.coordinator.update_interval is None


async def test_setup_of_an_account_entry_without_account_data_fails_cleanly(
    hass: HomeAssistant, storage: RuntimeStorage, platforms: MagicMock
) -> None:
    entry = make_config_entry(hass, {"type": "account"})

    with pytest.raises(ConfigEntryError, match="cannot be restored"):
        _ = await async_setup_entry(hass, entry)

    assert storage._entries == {}


async def test_setup_of_a_signed_out_account_asks_for_reauthentication(
    hass: HomeAssistant, storage: RuntimeStorage, platforms: MagicMock, closed: list[int]
) -> None:
    """R10: Home Assistant starts the re-authentication of an entry that raises this."""
    entry = account_entry(hass, login_state=LoginState.REQUIRE_2FA)

    with pytest.raises(ConfigEntryAuthFailed):
        _ = await async_setup_entry(hass, entry)

    assert storage._entries == {}
    assert storage.worker._stop_callbacks == {}
    assert closed == [worker_thread_id(storage)]
    platforms.setup.assert_not_awaited()


async def test_failing_platform_setup_leaves_nothing_behind(
    hass: HomeAssistant, storage: RuntimeStorage, platforms: MagicMock, closed: list[int]
) -> None:
    """R10: the account stayed in the storage, was fetched, and was never closed."""
    entry = account_entry(hass)
    platforms.setup.side_effect = RuntimeError("platform failed")

    with pytest.raises(RuntimeError, match="platform failed"):
        _ = await async_setup_entry(hass, entry)

    assert storage._entries == {}
    assert storage.accounts == []
    assert storage.worker._stop_callbacks == {}
    assert closed == [worker_thread_id(storage)]
    assert storage.coordinator.update_interval is None


async def test_unloading_an_account_closes_it_on_the_worker_after_its_entities(
    hass: HomeAssistant, storage: RuntimeStorage, platforms: MagicMock, closed: list[int]
) -> None:
    """R13: the account was closed first, and on the wrong loop."""
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True
    closed_before_unload: list[int] = []
    platforms.unload.side_effect = lambda *_args: closed_before_unload.extend(closed) or True

    assert await async_unload_entry(hass, entry) is True

    platforms.unload.assert_awaited_once_with(entry, ACCOUNT_PLATFORMS)
    assert closed_before_unload == []
    assert closed == [worker_thread_id(storage)]
    assert storage._entries == {}
    assert storage.worker._stop_callbacks == {}
    # The worker goes on for the next entry; it stops with Home Assistant.
    assert storage.worker.is_running


async def test_entry_stays_loaded_if_its_entities_cannot_be_unloaded(
    hass: HomeAssistant, storage: RuntimeStorage, platforms: MagicMock, closed: list[int]
) -> None:
    """R13: the result of unloading the platforms was ignored."""
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True
    platforms.unload.return_value = False

    assert await async_unload_entry(hass, entry) is False

    assert isinstance(storage.get_entry(entry), AccountRuntime)
    assert closed == []
    assert storage.coordinator.update_interval == timedelta(minutes=15)


async def test_reloading_an_account_entry_replaces_its_account(
    hass: HomeAssistant, storage: RuntimeStorage, platforms: MagicMock, closed: list[int]
) -> None:
    """What a re-authentication does: the new account starts fresh, the old one is closed."""
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True
    first = storage.get_entry(entry)
    assert isinstance(first, AccountRuntime)
    first.reauth_required = True
    await storage.coordinator.reload()
    assert storage.coordinator.update_interval is None

    assert await async_unload_entry(hass, entry) is True
    assert await async_setup_entry(hass, entry) is True

    second = storage.get_entry(entry)
    assert isinstance(second, AccountRuntime)
    assert second is not first
    assert second.reauth_required is False
    assert len(closed) == 1
    assert len(storage.worker._stop_callbacks) == 1
    assert storage.coordinator.update_interval == timedelta(minutes=15)


async def test_unloading_an_account_restores_the_interval_of_the_remaining_one(
    hass: HomeAssistant, storage: RuntimeStorage, platforms: MagicMock
) -> None:
    """R3: the interval stayed at 7:30 minutes after one of two accounts was removed."""
    kept = account_entry(hass, "a@example.com")
    removed = account_entry(hass, "b@example.com")
    assert await async_setup_entry(hass, kept) is True
    assert await async_setup_entry(hass, removed) is True
    assert storage.coordinator.update_interval == timedelta(minutes=7, seconds=30)

    assert await async_unload_entry(hass, removed) is True

    assert storage.accounts == [storage.get_entry(kept)]
    assert storage.coordinator.update_interval == timedelta(minutes=15)


async def test_unloading_the_last_account_disables_the_schedule(
    hass: HomeAssistant, storage: RuntimeStorage, platforms: MagicMock
) -> None:
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True

    assert await async_unload_entry(hass, entry) is True

    assert storage.coordinator.update_interval is None


async def test_unloading_an_unknown_entry_fails_without_touching_the_interval(
    hass: HomeAssistant, storage: RuntimeStorage, platforms: MagicMock
) -> None:
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True

    assert await async_unload_entry(hass, MagicMock(entry_id="missing")) is False

    platforms.unload.assert_not_awaited()
    assert storage.coordinator.update_interval == timedelta(minutes=15)


async def test_accounts_are_closed_when_home_assistant_stops(
    hass: HomeAssistant, storage: RuntimeStorage, platforms: MagicMock, closed: list[int]
) -> None:
    """Home Assistant does not unload entries when it stops; the worker closes what is left."""
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True
    worker = storage.worker
    worker_thread = worker._thread
    assert worker_thread is not None

    await hass.async_stop(force=True)

    assert closed == [worker_thread.ident]
    assert not worker.is_running
    assert not worker_thread.is_alive()


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

    assert sensor.FindMyLatitudeSensor(coordinator, device).state_class is None
    assert sensor.FindMyLongitudeSensor(coordinator, device).state_class is None
    # Sensors that are meant to be graphed keep theirs.
    assert sensor.FindMyBatteryPercentSensor(coordinator, device).state_class is not None
