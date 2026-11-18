# ruff: noqa: SLF001, INP001
# pyright: basic
"""Tests for the shared location coordinator.

Covers account rotation, fetch scheduling and the bookkeeping the account sensors read. The
FindMy library is replaced by mocks: nothing here talks to Apple.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from findmy import AsyncAppleAccount, FindMyAccessory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

from custom_components.findmy.coordinator import (
    CoordinatorFetchStatus,
    FindMyCoordinator,
)
from custom_components.findmy.storage import RuntimeStorage

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


def _noop(*_args: object, **_kwargs: object) -> None:
    return None


@pytest_asyncio.fixture
async def hass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[HomeAssistant]:
    """A bare Home Assistant core, enough to run a coordinator."""
    instance = HomeAssistant(str(tmp_path))
    frame.async_setup(instance)
    # The shared coordinator has no config entry, which Home Assistant reports through the frame
    # helper. On Windows that helper does not recognise the custom integration path and raises.
    monkeypatch.setattr(frame, "report_usage", _noop)
    # Stands in for the config entry registry; tests set the entries they need.
    instance.config_entries = MagicMock()
    instance.config_entries.async_get_entry.return_value = None
    yield instance
    await instance.async_stop(force=True)


@pytest.fixture
def storage(hass: HomeAssistant) -> RuntimeStorage:
    return RuntimeStorage(hass)


@pytest.fixture
def coordinator(storage: RuntimeStorage) -> FindMyCoordinator:
    return storage.coordinator


def make_account(email: str) -> MagicMock:
    account = MagicMock(spec=AsyncAppleAccount)
    account.account_name = email
    account.first_name = "Test"
    account.last_name = "User"
    account.fetch_location = AsyncMock(return_value={})
    return account


def make_accessory() -> FindMyAccessory:
    now = datetime.now(tz=UTC)
    return FindMyAccessory(
        master_key=os.urandom(28),
        skn=os.urandom(32),
        sks=os.urandom(32),
        paired_at=now - timedelta(days=30),
        alignment_date=now - timedelta(hours=1),
        alignment_index=30 * 96,
        identifier="test-accessory",
        name="Test tag",
    )


def add_account(hass: HomeAssistant, storage: RuntimeStorage, entry_id: str) -> MagicMock:
    """Register a mocked account and a mocked config entry under the given entry id."""
    account = make_account(f"{entry_id}@example.com")
    storage._entries[entry_id] = account

    entries: dict[str, MagicMock] = getattr(hass, "_test_entries", {})
    entries[entry_id] = MagicMock(entry_id=entry_id)
    hass._test_entries = entries  # pyright: ignore[reportAttributeAccessIssue]
    cast("MagicMock", hass.config_entries).async_get_entry.side_effect = entries.get
    return account


def entry_of(hass: HomeAssistant, entry_id: str) -> MagicMock:
    return hass._test_entries[entry_id]  # pyright: ignore[reportAttributeAccessIssue]


def listen(coordinator: FindMyCoordinator, device: object | None = None) -> None:
    """Register a listener: the coordinator only schedules refreshes while it has one."""
    coordinator.async_add_listener(lambda: None, device)


@pytest.mark.asyncio
async def test_interval_is_divided_between_accounts(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    add_account(hass, storage, "a")
    await coordinator.reload()
    assert coordinator.update_interval == timedelta(minutes=15)

    add_account(hass, storage, "b")
    add_account(hass, storage, "c")
    await coordinator.reload()
    assert coordinator.update_interval == timedelta(minutes=5)


@pytest.mark.asyncio
async def test_next_fetch_is_15_minutes_after_last_fetch_with_three_accounts(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    """R16: the same account is fetched again after 15 minutes, however many accounts exist."""
    accounts = [add_account(hass, storage, entry_id) for entry_id in ("a", "b", "c")]
    await coordinator.reload()
    listen(coordinator)

    await coordinator.async_refresh()

    fetched = accounts[0]
    last_fetch = coordinator.get_account_last_fetch(fetched)
    assert last_fetch is not None
    assert coordinator.get_account_next_fetch(fetched) == last_fetch + timedelta(minutes=15)
    assert coordinator.get_account_next_fetch(accounts[1]) is None
    assert coordinator.get_account_last_fetch_result(fetched) is CoordinatorFetchStatus.SUCCESS
