# ruff: noqa: SLF001, PLR2004, INP001
# pyright: basic
"""Tests for the shared location coordinator.

Covers account rotation, fetch scheduling and the bookkeeping the account sensors read. The
FindMy library is replaced by mocks: nothing here talks to Apple.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
import pytest_asyncio
from findmy import (
    AsyncAppleAccount,
    FindMyAccessory,
    InvalidCredentialsError,
    InvalidStateError,
    UnauthorizedError,
    UnhandledProtocolError,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.findmy.coordinator import (
    CoordinatorFetchStatus,
    FindMyCoordinator,
)
from custom_components.findmy.key_cache import CachedFindMyAccessory
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


@pytest.mark.asyncio
async def test_successful_fetch_is_logged_as_success_and_stores_reports(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    account = add_account(hass, storage, "a")
    accessory = make_accessory()
    report = MagicMock(name="report")
    account.fetch_location.return_value = {accessory: report}
    await coordinator.reload()
    listen(coordinator, accessory)

    await coordinator.async_refresh()

    account.fetch_location.assert_awaited_once_with([accessory])
    assert coordinator.last_update_success is True
    assert coordinator.data == {accessory: report}
    assert coordinator.get_account_last_fetch_result(account) is CoordinatorFetchStatus.SUCCESS
    assert coordinator.get_account_last_fetch_duration(account) is not None


@pytest.mark.asyncio
async def test_result_is_ongoing_while_the_fetch_runs(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    """R2: the result must not read SUCCESS before the fetch has returned."""
    account = add_account(hass, storage, "a")
    seen: list[CoordinatorFetchStatus | None] = []

    async def fetch(_devices: object) -> dict[object, object]:
        seen.append(coordinator.get_account_last_fetch_result(account))
        return {}

    account.fetch_location.side_effect = fetch
    await coordinator.reload()

    await coordinator.async_refresh()

    assert seen == [CoordinatorFetchStatus.ONGOING]


@pytest.mark.parametrize(
    ("error", "expected_message"),
    [
        (UnhandledProtocolError("Failed to fetch reports: 500"), "Unexpected answer from Apple"),
        (aiohttp.ClientConnectionError("connection reset"), "Could not reach Apple"),
        (TimeoutError(), "Timeout while fetching location reports"),
        (KeyError("searchPartyToken"), "missing 'searchPartyToken'"),
        (ValueError("bad payload"), "Could not fetch location reports: bad payload"),
    ],
)
@pytest.mark.asyncio
async def test_library_errors_become_update_failed_and_are_logged_as_error(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    error: Exception,
    expected_message: str,
) -> None:
    """R2: these errors used to be recorded as a successful fetch."""
    account = add_account(hass, storage, "a")
    account.fetch_location.side_effect = error
    await coordinator.reload()
    listen(coordinator)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert expected_message in str(coordinator.last_exception)
    assert coordinator.last_exception.__cause__ is error
    assert coordinator.get_account_last_fetch_result(account) is CoordinatorFetchStatus.ERROR
    # Not an authentication problem: no re-authentication, and the account is tried again.
    entry_of(hass, "a").async_start_reauth.assert_not_called()
    assert coordinator._unsub_refresh is not None
    assert coordinator.update_interval == timedelta(minutes=15)


@pytest.mark.parametrize(
    "error",
    [
        UnauthorizedError("Not authorized to fetch reports."),
        InvalidStateError("account is in state REQUIRE_2FA"),
        InvalidCredentialsError("Password authentication failed"),
        ValueError("No username or password specified"),
    ],
)
@pytest.mark.asyncio
async def test_auth_error_starts_reauth_for_that_account_only(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    error: Exception,
) -> None:
    """R1: the expired account asks for a new sign-in; the other account keeps fetching."""
    expired = add_account(hass, storage, "a")
    healthy = add_account(hass, storage, "b")
    expired.fetch_location.side_effect = error
    await coordinator.reload()
    listen(coordinator)

    await coordinator.async_refresh()

    entry_of(hass, "a").async_start_reauth.assert_called_once_with(hass)
    entry_of(hass, "b").async_start_reauth.assert_not_called()
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert coordinator.last_update_success is False
    assert coordinator.get_account_last_fetch_result(expired) is CoordinatorFetchStatus.ERROR
    assert coordinator.get_account_next_fetch(expired) is None
    # The next fetch is scheduled, and only the healthy account shares the 15 minutes now.
    assert coordinator._unsub_refresh is not None
    assert coordinator.update_interval == timedelta(minutes=15)

    for _ in range(3):
        await coordinator.async_refresh()

    assert expired.fetch_location.await_count == 1
    assert healthy.fetch_location.await_count == 3
    assert coordinator.last_update_success is True
    assert coordinator.get_account_last_fetch_result(healthy) is CoordinatorFetchStatus.SUCCESS
    # Skipped, so neither a second sign-in attempt at Apple nor a second flow.
    entry_of(hass, "a").async_start_reauth.assert_called_once_with(hass)


@pytest.mark.asyncio
async def test_reloaded_entry_returns_to_the_rotation(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    expired = add_account(hass, storage, "a")
    healthy = add_account(hass, storage, "b")
    expired.fetch_location.side_effect = UnauthorizedError("Not authorized to fetch reports.")
    await coordinator.reload()
    listen(coordinator)
    await coordinator.async_refresh()
    assert coordinator.update_interval == timedelta(minutes=15)

    # Re-authentication reloads the entry, which stores a new account object under the same id.
    renewed = add_account(hass, storage, "a")
    await coordinator.reload()

    assert coordinator.update_interval == timedelta(minutes=7, seconds=30)
    for _ in range(4):
        await coordinator.async_refresh()
    assert expired.fetch_location.await_count == 1
    assert renewed.fetch_location.await_count == 2
    assert healthy.fetch_location.await_count == 2


@pytest.mark.asyncio
async def test_all_accounts_expired_stops_the_schedule_and_fails_manual_refreshes(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    account = add_account(hass, storage, "a")
    account.fetch_location.side_effect = InvalidStateError("account is in state REQUIRE_2FA")
    await coordinator.reload()
    listen(coordinator)

    await coordinator.async_refresh()

    assert coordinator.update_interval is None
    assert coordinator._unsub_refresh is None

    await coordinator.async_refresh()

    assert account.fetch_location.await_count == 1
    assert coordinator.last_update_success is False
    assert "re-authenticated" in str(coordinator.last_exception)


@pytest.mark.asyncio
async def test_unexpected_error_is_logged_as_error_and_fetching_continues(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    """A bug in the library must not be recorded as success nor stop the schedule."""
    account = add_account(hass, storage, "a")
    account.fetch_location.side_effect = RuntimeError("boom")
    await coordinator.reload()
    listen(coordinator)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert coordinator.get_account_last_fetch_result(account) is CoordinatorFetchStatus.ERROR
    assert coordinator._unsub_refresh is not None


@pytest.mark.asyncio
async def test_fetch_log_stays_bounded(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    """R17: the log grew by two entries per fetch and was never trimmed."""
    accounts = [add_account(hass, storage, entry_id) for entry_id in ("a", "b")]
    await coordinator.reload()

    for _ in range(200):
        await coordinator.async_refresh()

    limit = FindMyCoordinator._MAX_LOG_ENTRIES_PER_ACCOUNT
    assert sum(len(logs) for logs in coordinator._logs.values()) <= limit * len(accounts)
    for account in accounts:
        assert coordinator.get_account_last_fetch_result(account) is CoordinatorFetchStatus.SUCCESS
        assert coordinator.get_account_last_fetch_duration(account) is not None


@pytest.mark.asyncio
async def test_resting_account_keeps_its_last_result(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    """Trimming must not erase the state of an account that is skipped for a long time."""
    expired = add_account(hass, storage, "a")
    add_account(hass, storage, "b")
    expired.fetch_location.side_effect = UnauthorizedError("Not authorized to fetch reports.")
    await coordinator.reload()

    for _ in range(200):
        await coordinator.async_refresh()

    assert coordinator.get_account_last_fetch_result(expired) is CoordinatorFetchStatus.ERROR
    assert coordinator.get_account_last_fetch(expired) is not None


@pytest.mark.asyncio
async def test_log_of_a_removed_account_is_dropped(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    removed = add_account(hass, storage, "a")
    kept = add_account(hass, storage, "b")
    await coordinator.reload()
    for _ in range(2):
        await coordinator.async_refresh()

    del storage._entries["a"]
    await coordinator.reload()

    assert list(coordinator._logs) == [kept]
    assert coordinator.get_account_last_fetch(removed) is None


def make_cached_accessory() -> CachedFindMyAccessory:
    accessory = CachedFindMyAccessory.from_json(make_accessory().to_json())
    assert isinstance(accessory, CachedFindMyAccessory)
    return accessory


@pytest.mark.asyncio
async def test_keys_are_warmed_outside_the_event_loop_before_the_fetch(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R20: the fetch must find finished keys instead of deriving them on the event loop."""
    account = add_account(hass, storage, "a")
    accessory = make_cached_accessory()
    loop_thread = threading.get_ident()
    warm_threads: list[int] = []
    original = CachedFindMyAccessory.warm_keys

    def warm_keys(self: CachedFindMyAccessory, start: datetime, end: datetime) -> int:
        warm_threads.append(threading.get_ident())
        return original(self, start, end)

    monkeypatch.setattr(CachedFindMyAccessory, "warm_keys", warm_keys)
    cached_at_fetch: list[int] = []

    async def fetch(_devices: object) -> dict[object, object]:
        cached_at_fetch.append(len(accessory._key_cache))
        return {}

    account.fetch_location.side_effect = fetch
    await coordinator.reload()
    listen(coordinator, accessory)

    await coordinator.async_refresh()

    assert len(warm_threads) == 1
    assert warm_threads[0] != loop_thread
    # A week of 15-minute indices was ready when the library was called.
    assert cached_at_fetch[0] >= 7 * 96
    assert coordinator.last_update_success is True


@pytest.mark.asyncio
async def test_failing_warm_up_ends_the_fetch_as_error(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fetch that never started must not leave the account in the state 'ongoing'."""
    account = add_account(hass, storage, "a")

    def warm_keys(_self: CachedFindMyAccessory, _start: datetime, _end: datetime) -> int:
        msg = "The key index must be non-negative"
        raise ValueError(msg)

    monkeypatch.setattr(CachedFindMyAccessory, "warm_keys", warm_keys)
    await coordinator.reload()
    listen(coordinator, make_cached_accessory())

    await coordinator.async_refresh()

    account.fetch_location.assert_not_awaited()
    assert coordinator.get_account_last_fetch_result(account) is CoordinatorFetchStatus.ERROR
    assert isinstance(coordinator.last_exception, UpdateFailed)
