# pyright: basic
"""Tests for the shared location coordinator.

Covers account rotation, fetch scheduling, the hand-over between the event loop and the FindMy
worker thread, and the state the account sensors read. The FindMy library is replaced by mocks:
nothing here talks to Apple.

An account in these tests is an ``AccountRuntime`` around a mocked library account that lives on
the worker thread, as in production. ``lib(account)`` is that mock.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
import pytest_asyncio
from findmy import (
    FindMyAccessory,
    InvalidCredentialsError,
    InvalidStateError,
    KeyPair,
    UnauthorizedError,
    UnhandledProtocolError,
)
from findmy.reports.reports import LocationReportsFetcher
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.findmy import coordinator as coordinator_module
from custom_components.findmy.account import AccountRuntime, FetchStatus
from custom_components.findmy.budget import account_budget_id
from custom_components.findmy.coordinator import FindMyCoordinator
from custom_components.findmy.key_cache import (
    CATCH_UP_INTERVAL,
    CachedFindMyAccessory,
    CatchUpWindow,
    SearchWindow,
)
from custom_components.findmy.storage import RuntimeStorage

from .conftest import (
    FetchClock,
    library_mock,
    make_account_state,
    make_mock_account,
    open_account,
    worker_thread_id,
)

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
    # Also stops the worker thread the storage started.
    await instance.async_stop(force=True)


@pytest.fixture
def storage(hass: HomeAssistant) -> RuntimeStorage:
    return RuntimeStorage(hass)


@pytest.fixture
def coordinator(storage: RuntimeStorage) -> FindMyCoordinator:
    return storage.coordinator


# The mocked library account behind an account of these tests.
lib = library_mock


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


async def add_account(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    entry_id: str,
    account: object | None = None,
) -> AccountRuntime:
    """Register an account and a mocked config entry under the given entry id."""
    mock = account or make_mock_account(email=f"{entry_id}@example.com", reports={})
    runtime = await open_account(storage, entry_id, cast("MagicMock", mock))

    entries: dict[str, MagicMock] = getattr(hass, "_test_entries", {})
    entries[entry_id] = MagicMock(
        entry_id=entry_id,
        data={
            "type": "account",
            "account_data": make_account_state(email=f"{entry_id}@example.com"),
        },
    )
    hass._test_entries = entries  # pyright: ignore[reportAttributeAccessIssue]
    cast("MagicMock", hass.config_entries).async_get_entry.side_effect = entries.get
    return runtime


def entry_of(hass: HomeAssistant, entry_id: str) -> MagicMock:
    return hass._test_entries[entry_id]  # pyright: ignore[reportAttributeAccessIssue]


def listen(coordinator: FindMyCoordinator, device: object | None = None) -> None:
    """Register a listener: the coordinator only schedules refreshes while it has one."""
    coordinator.async_add_listener(lambda: None, device)


@pytest.mark.asyncio
async def test_interval_is_divided_between_accounts(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    await add_account(hass, storage, "a")
    await coordinator.reload()
    assert coordinator.update_interval == timedelta(minutes=15)

    await add_account(hass, storage, "b")
    await add_account(hass, storage, "c")
    await coordinator.reload()
    assert coordinator.update_interval == timedelta(minutes=5)


@pytest.mark.asyncio
async def test_budget_id_comes_from_the_apple_id_then_the_unique_id_then_the_entry_id(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    named = await add_account(hass, storage, "a")
    assert coordinator._budget_id(named) == account_budget_id("a@example.com")

    nameless = make_mock_account(email="b@example.com", reports={})
    nameless.account_name = None
    account = await add_account(hass, storage, "b", nameless)
    assert account.account_name is None
    entry_of(hass, "b").unique_id = "b@example.com"
    assert coordinator._budget_id(account) == account_budget_id("b@example.com")

    entry_of(hass, "b").unique_id = None
    assert coordinator._budget_id(account) == account_budget_id("entry:b")


@pytest.mark.asyncio
async def test_two_entries_of_one_apple_id_share_its_limit(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    fetch_clock: FetchClock,
) -> None:
    first = await add_account(hass, storage, "a")
    second = await add_account(
        hass, storage, "b", make_mock_account(email="a@example.com", reports={})
    )
    await coordinator.reload()
    assert coordinator.update_interval == timedelta(minutes=15)

    for _ in range(3):
        await coordinator.async_refresh()
        fetch_clock.advance(timedelta(minutes=5))

    assert lib(first).fetch_location.await_count == 1
    assert lib(second).fetch_location.await_count == 0


@pytest.mark.asyncio
async def test_next_fetch_is_15_minutes_after_last_fetch_with_three_accounts(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    fetch_clock: FetchClock,
) -> None:
    """R16: the same account is fetched again after 15 minutes, however many accounts exist."""
    accounts = [await add_account(hass, storage, entry_id) for entry_id in ("a", "b", "c")]
    await coordinator.reload()
    listen(coordinator)

    await coordinator.async_refresh()

    fetched = accounts[0]
    assert fetched.last_fetch is not None
    # Counted from the start of the fetch: that is what the budget records.
    assert coordinator.get_account_next_fetch(fetched) == fetch_clock.now + timedelta(minutes=15)
    assert coordinator.get_account_next_fetch(accounts[1]) is None
    assert fetched.last_fetch_result is FetchStatus.SUCCESS
    # The next account follows 15/3 minutes later.
    assert coordinator.update_interval == timedelta(minutes=5)


@pytest.mark.asyncio
async def test_successful_fetch_is_logged_as_success_and_stores_reports(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    account = await add_account(hass, storage, "a")
    accessory = make_accessory()
    report = MagicMock(name="report")
    lib(account).fetch_location.return_value = {accessory: report}
    await coordinator.reload()
    listen(coordinator, accessory)

    await coordinator.async_refresh()

    # Equal to the accessory, but not the accessory itself: see the test below.
    lib(account).fetch_location.assert_awaited_once_with([accessory])
    assert coordinator.last_update_success is True
    assert coordinator.data == {accessory: report}
    assert list(coordinator.data or {}) == [accessory]
    assert next(iter(coordinator.data or {})) is accessory
    assert account.last_fetch_result is FetchStatus.SUCCESS
    assert account.last_fetch_duration is not None


@pytest.mark.asyncio
async def test_result_is_ongoing_while_the_fetch_runs(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    fetch_clock: FetchClock,
) -> None:
    """R2: the result must not read SUCCESS before the fetch has returned."""
    account = await add_account(hass, storage, "a")
    seen: list[tuple[FetchStatus | None, timedelta | None]] = []

    async def fetch(_devices: object) -> dict[object, object]:
        seen.append((account.last_fetch_result, account.last_fetch_duration))
        return {}

    lib(account).fetch_location.side_effect = fetch
    await coordinator.reload()

    await coordinator.async_refresh()
    fetch_clock.advance()
    await coordinator.async_refresh()

    # Also during the second fetch, when a duration of the first one exists.
    assert seen == [(FetchStatus.ONGOING, None), (FetchStatus.ONGOING, None)]
    assert account.last_fetch_result is FetchStatus.SUCCESS


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
async def test_library_errors_become_update_failed_and_are_logged_as_error(  # noqa: PLR0917
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    error: Exception,
    expected_message: str,
    fetch_clock: FetchClock,
) -> None:
    """R2: these errors used to be recorded as a successful fetch."""
    account = await add_account(hass, storage, "a")
    lib(account).fetch_location.side_effect = error
    await coordinator.reload()
    listen(coordinator)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert expected_message in str(coordinator.last_exception)
    # The very exception the library raised on the worker thread.
    assert coordinator.last_exception.__cause__ is error
    assert account.last_fetch_result is FetchStatus.ERROR
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
    fetch_clock: FetchClock,
) -> None:
    """R1: the expired account asks for a new sign-in; the other account keeps fetching."""
    expired = await add_account(hass, storage, "a")
    healthy = await add_account(hass, storage, "b")
    lib(expired).fetch_location.side_effect = error
    await coordinator.reload()
    listen(coordinator)

    await coordinator.async_refresh()

    entry_of(hass, "a").async_start_reauth.assert_called_once_with(hass)
    entry_of(hass, "b").async_start_reauth.assert_not_called()
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert coordinator.last_update_success is False
    assert expired.reauth_required is True
    assert expired.last_fetch_result is FetchStatus.ERROR
    assert coordinator.get_account_next_fetch(expired) is None
    # The next fetch is scheduled, and only the healthy account shares the 15 minutes now.
    assert coordinator._unsub_refresh is not None
    assert coordinator.update_interval == timedelta(minutes=15)

    for _ in range(3):
        fetch_clock.advance()
        await coordinator.async_refresh()

    assert lib(expired).fetch_location.await_count == 1
    assert lib(healthy).fetch_location.await_count == 3
    assert coordinator.last_update_success is True
    assert healthy.last_fetch_result is FetchStatus.SUCCESS
    # Skipped, so neither a second sign-in attempt at Apple nor a second flow.
    entry_of(hass, "a").async_start_reauth.assert_called_once_with(hass)


@pytest.mark.asyncio
async def test_expired_session_of_an_account_without_password_starts_reauth(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    """S1: with no password kept, FindMy.py itself reports an expired session as ValueError.

    Runs the library for real, from the stored state down to the request: Apple answers 401,
    the library tries to sign in again and finds no password to do it with.
    """
    state = make_account_state(email="user@example.com", password="hunter2")
    account = await AccountRuntime.async_load(storage.worker, "a", state)
    storage._entries["a"] = account
    entry = MagicMock(entry_id="a", data={"type": "account", "account_data": state})
    cast("MagicMock", hass.config_entries).async_get_entry.side_effect = {"a": entry}.get
    # Only the network is replaced.
    library_account = account._account
    library_account._http.post = AsyncMock(
        return_value=SimpleNamespace(status_code=401, ok=False, text=lambda: "Unauthorized")
    )
    library_account.get_anisette_headers = AsyncMock(return_value={})
    await coordinator.reload()
    listen(coordinator, KeyPair.new())

    await coordinator.async_refresh()

    library_account._http.post.assert_awaited_once()
    assert coordinator.last_exception is not None
    cause = coordinator.last_exception.__cause__
    assert isinstance(cause, ValueError)
    assert "No username or password specified" in str(cause)
    entry.async_start_reauth.assert_called_once_with(hass)
    assert account.reauth_required is True
    assert coordinator.update_interval is None


@pytest.mark.asyncio
async def test_reloaded_entry_returns_to_the_rotation(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    fetch_clock: FetchClock,
) -> None:
    expired = await add_account(hass, storage, "a")
    healthy = await add_account(hass, storage, "b")
    lib(expired).fetch_location.side_effect = UnauthorizedError("Not authorized to fetch reports.")
    await coordinator.reload()
    listen(coordinator)
    await coordinator.async_refresh()
    assert coordinator.update_interval == timedelta(minutes=15)

    # Re-authentication reloads the entry, which stores a new account object under the same id.
    renewed = await add_account(hass, storage, "a")
    await coordinator.reload()

    assert coordinator.update_interval == timedelta(minutes=7, seconds=30)
    # The renewed account is the same Apple ID: it waits for 15 minutes after the failed fetch,
    # which reached Apple too, and the healthy one goes first.
    for _ in range(4):
        fetch_clock.advance(timedelta(minutes=7, seconds=30))
        await coordinator.async_refresh()
    assert lib(expired).fetch_location.await_count == 1
    assert lib(renewed).fetch_location.await_count == 2
    assert lib(healthy).fetch_location.await_count == 2
    # The new object starts without the history of the old one.
    assert renewed.last_fetch_result is FetchStatus.SUCCESS


@pytest.mark.asyncio
async def test_all_accounts_expired_stops_the_schedule_and_fails_manual_refreshes(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    account = await add_account(hass, storage, "a")
    lib(account).fetch_location.side_effect = InvalidStateError("account is in state REQUIRE_2FA")
    await coordinator.reload()
    listen(coordinator)

    await coordinator.async_refresh()

    assert coordinator.update_interval is None
    assert coordinator._unsub_refresh is None

    await coordinator.async_refresh()

    assert lib(account).fetch_location.await_count == 1
    assert coordinator.last_update_success is False
    assert "re-authenticated" in str(coordinator.last_exception)


@pytest.mark.asyncio
async def test_unexpected_error_is_logged_as_error_and_fetching_continues(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    """A bug in the library must not be recorded as success nor stop the schedule."""
    account = await add_account(hass, storage, "a")
    lib(account).fetch_location.side_effect = RuntimeError("boom")
    await coordinator.reload()
    listen(coordinator)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert account.last_fetch_result is FetchStatus.ERROR
    assert coordinator._unsub_refresh is not None


@pytest.mark.asyncio
async def test_fetch_state_is_kept_per_account_over_many_fetches(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    fetch_clock: FetchClock,
) -> None:
    """R17: a log that grew with every fetch is replaced by the state of the latest one."""
    accounts = [await add_account(hass, storage, entry_id) for entry_id in ("a", "b")]
    await coordinator.reload()

    for _ in range(200):
        await coordinator.async_refresh()
        fetch_clock.advance(timedelta(minutes=7, seconds=30))

    for account in accounts:
        assert lib(account).fetch_location.await_count == 100
        assert account.last_fetch_result is FetchStatus.SUCCESS
        assert account.last_fetch_duration is not None
        assert account.last_fetch_duration < timedelta(seconds=5)


@pytest.mark.asyncio
async def test_resting_account_keeps_its_last_result(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    """The state of an account that is skipped for a long time must stay what it was."""
    expired = await add_account(hass, storage, "a")
    await add_account(hass, storage, "b")
    lib(expired).fetch_location.side_effect = UnauthorizedError("Not authorized to fetch reports.")
    await coordinator.reload()

    for _ in range(200):
        await coordinator.async_refresh()

    assert expired.last_fetch_result is FetchStatus.ERROR
    assert expired.last_fetch is not None


def make_cached_accessory() -> CachedFindMyAccessory:
    accessory = CachedFindMyAccessory.from_json(make_accessory().to_json())
    assert isinstance(accessory, CachedFindMyAccessory)
    return accessory


@pytest.mark.asyncio
async def test_fetch_runs_on_the_worker_thread_with_objects_made_for_it(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R20: neither the event loop nor its objects take part in the library's fetch."""
    account = await add_account(hass, storage, "a")
    cached = make_cached_accessory()
    plain = make_accessory()
    static = KeyPair.new()
    loop_thread = threading.get_ident()
    window_threads: list[int] = []
    original = CachedFindMyAccessory.search_window

    def search_window(self: CachedFindMyAccessory, now: datetime) -> SearchWindow:
        window_threads.append(threading.get_ident())
        return original(self, now)

    monkeypatch.setattr(CachedFindMyAccessory, "search_window", search_window)
    fetch_threads: list[int] = []
    derived_at_fetch: list[int] = []

    async def fetch(sources: list[object]) -> object:
        fetch_threads.append(threading.get_ident())
        derived_at_fetch.append(len(cached._key_cache))
        return dict.fromkeys(sources)

    lib(account).fetch_location.side_effect = fetch
    await coordinator.reload()
    for device in (cached, plain, static):
        listen(coordinator, device)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    # Three different threads: the keys are derived in an executor, the fetch runs on the
    # worker, and the event loop only waits.
    assert len({loop_thread, window_threads[0], fetch_threads[0]}) == 3
    assert fetch_threads[0] == worker_thread_id(storage)
    # A week of 15-minute indices was ready when the library was called.
    assert derived_at_fetch[0] >= 7 * 96

    sources = fetched_sources(account)
    assert len(sources) == 3
    by_type = {type(source): source for source in sources}
    # The cached accessory is searched through a window with the same bounds and keys.
    window = by_type[SearchWindow]
    assert isinstance(window, SearchWindow)
    now = datetime.now(tz=UTC)
    assert window.accessory is cached
    assert window.highest == cached.get_max_index(now)
    assert window.keys_at(window.highest) == cached.keys_at(window.highest)
    # An accessory without cache is searched through a copy.
    assert by_type[FindMyAccessory] == plain
    assert by_type[FindMyAccessory] is not plain
    # A static key is never changed by anyone, so it is shared.
    assert by_type[KeyPair] is static


@pytest.mark.asyncio
async def test_failing_key_derivation_ends_the_fetch_as_error(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fetch that never started must not leave the account in the state 'ongoing'."""
    account = await add_account(hass, storage, "a")

    def search_window(_self: CachedFindMyAccessory, _now: datetime) -> SearchWindow:
        msg = "The key index must be non-negative"
        raise ValueError(msg)

    monkeypatch.setattr(CachedFindMyAccessory, "search_window", search_window)
    await coordinator.reload()
    listen(coordinator, make_cached_accessory())

    await coordinator.async_refresh()

    lib(account).fetch_location.assert_not_awaited()
    assert account.last_fetch_result is FetchStatus.ERROR
    assert isinstance(coordinator.last_exception, UpdateFailed)


@pytest.mark.asyncio
async def test_fetch_that_does_not_return_times_out_and_frees_the_worker(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a limit, one hanging request would block every later fetch of every account."""
    monkeypatch.setattr(coordinator_module, "_FETCH_TIMEOUT", 0.05)
    hanging = await add_account(hass, storage, "a")
    healthy = await add_account(hass, storage, "b")
    cancelled = threading.Event()

    async def hang(_sources: object) -> dict[object, object]:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {}

    lib(hanging).fetch_location.side_effect = hang
    await coordinator.reload()
    listen(coordinator)

    await coordinator.async_refresh()

    assert cancelled.is_set()
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert "Timeout" in str(coordinator.last_exception)
    assert isinstance(coordinator.last_exception.__cause__, TimeoutError)
    assert hanging.last_fetch_result is FetchStatus.ERROR
    # A timeout says nothing about the session: no re-authentication, and it is tried again.
    assert hanging.reauth_required is False
    entry_of(hass, "a").async_start_reauth.assert_not_called()

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert healthy.last_fetch_result is FetchStatus.SUCCESS


@pytest.mark.asyncio
async def test_fetch_interrupted_by_the_shutdown_fails_quietly(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    account = await add_account(hass, storage, "a")
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def hang(_sources: object) -> dict[object, object]:
        loop.call_soon_threadsafe(started.set)
        await asyncio.sleep(60)
        return {}

    lib(account).fetch_location.side_effect = hang
    await coordinator.reload()
    refresh = asyncio.create_task(coordinator.async_refresh())
    await started.wait()

    await storage.worker.async_stop()
    await refresh

    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert "shutdown" in str(coordinator.last_exception)
    assert account.last_fetch_result is FetchStatus.ERROR
    # The worker closed the account when it stopped.
    lib(account).close.assert_awaited_once()


@dataclass(frozen=True, order=True)
class FakeReport:
    """Stands in for a decrypted location report: all FindMy.py's fetch looks at."""

    timestamp: datetime
    hashed_adv_key_bytes: bytes = field(compare=False)

    def decrypt(self, _key: object) -> None:
        return None


class FakeApple:
    """The report endpoint. Knows under which keys accessories were seen, and counts requests."""

    def __init__(self) -> None:
        self.requests = 0
        self.sightings: dict[str, FakeReport] = {}

    def see(self, accessory: FindMyAccessory, index: int, timestamp: datetime) -> FakeReport:
        """Record that someone reported the accessory while it was at the given key index."""
        key = FindMyAccessory.from_json(accessory.to_json())._primary_key_at(index)
        report = FakeReport(timestamp, key.hashed_adv_key_bytes)
        self.sightings[key.hashed_adv_key_b64] = report
        return report

    async def fetch_raw_reports(self, devices: list[tuple[list[str], list[str]]]) -> list[object]:
        self.requests += 1
        asked = {key_id for primary, secondary in devices for key_id in (*primary, *secondary)}
        return [report for key_id, report in self.sightings.items() if key_id in asked]


async def add_fetching_account(
    hass: HomeAssistant, storage: RuntimeStorage, entry_id: str, apple: FakeApple
) -> AccountRuntime:
    """An account that runs FindMy.py's real report fetcher against the fake endpoint."""
    account = await add_account(hass, storage, entry_id)
    fetcher = LocationReportsFetcher(apple)  # pyright: ignore[reportArgumentType]

    async def fetch_location(devices: list[object]) -> object:
        # Same as AsyncAppleAccount.fetch_location, without the login check.
        history = await fetcher.fetch_location_history(devices)  # pyright: ignore[reportArgumentType, reportCallIssue]
        return {device: max(reports) if reports else None for device, reports in history.items()}

    lib(account).fetch_location.side_effect = fetch_location
    return account


def make_unlocated_accessory(days_without_report: int = 30) -> CachedFindMyAccessory:
    """A cached accessory whose alignment is old, so that a gap opened below its top window."""
    now = datetime.now(tz=UTC)
    return CachedFindMyAccessory(
        master_key=os.urandom(28),
        skn=os.urandom(32),
        sks=os.urandom(32),
        paired_at=now - timedelta(days=days_without_report + 30),
        alignment_date=now - timedelta(days=days_without_report),
        alignment_index=30 * 96,
        identifier="unlocated-accessory",
        name="Unlocated tag",
    )


def fetched_sources(account: AccountRuntime) -> list[object]:
    """What the latest fetch asked the account for."""
    return list(lib(account).fetch_location.await_args.args[0])


def source_types(account: AccountRuntime) -> list[type]:
    return [type(source) for source in fetched_sources(account)]


@pytest.mark.asyncio
async def test_accessory_ten_days_behind_the_clock_is_found_again(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    fetch_clock: FetchClock,
) -> None:
    """An accessory below the weekly window must come back on its own, not stay lost."""
    apple = FakeApple()
    account = await add_fetching_account(hass, storage, "a", apple)
    accessory = make_unlocated_accessory()
    now = datetime.now(tz=UTC)
    # Its battery was empty for ten days: the index is 960 behind what the clock suggests.
    true_index = accessory.get_max_index(now) - 10 * 96
    assert true_index < accessory.get_min_index(now - timedelta(days=7))
    report = apple.see(accessory, true_index, now - timedelta(hours=1))
    await coordinator.reload()
    listen(coordinator, accessory)

    await coordinator.async_refresh()

    # First cycle: the top window finds nothing, the first catch-up window does.
    assert source_types(account) == [SearchWindow, CatchUpWindow]
    windows = cast("list[SearchWindow]", fetched_sources(account))
    assert all(window.accessory is accessory for window in windows)
    assert coordinator.last_update_success is True
    assert coordinator.data == {accessory: report}
    # At most the three requests of the top window and three of the catch-up window.
    assert apple.requests <= 6
    # The report realigned the accessory: it is back in the window searched on every fetch.
    assert accessory._alignment_index == true_index
    assert accessory.get_min_index(now - timedelta(days=7)) <= true_index
    assert true_index <= accessory.get_max_index(now)

    apple.requests = 0
    accessory._last_catch_up = None  # even if a catch-up were due again
    fetch_clock.advance()
    await coordinator.async_refresh()

    # Second cycle: found in the top window, with no catch-up and no extra request.
    assert source_types(account) == [SearchWindow]
    assert apple.requests <= 3
    assert coordinator.data == {accessory: report}


@pytest.mark.asyncio
async def test_accessory_is_only_realigned_on_the_event_loop(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Bluetooth matching works with the same alignment, on the event loop, unlocked."""
    apple = FakeApple()
    account = await add_fetching_account(hass, storage, "a", apple)
    accessory = make_cached_accessory()
    now = datetime.now(tz=UTC)
    seen_index = accessory.get_max_index(now) - 2
    seen_at = now - timedelta(minutes=20)
    apple.see(accessory, seen_index, seen_at)
    realigned_on: list[int] = []
    original = CachedFindMyAccessory.update_alignment

    def update_alignment(self: CachedFindMyAccessory, dt: datetime, index: int) -> None:
        realigned_on.append(threading.get_ident())
        original(self, dt, index)

    monkeypatch.setattr(CachedFindMyAccessory, "update_alignment", update_alignment)
    alignment_during_fetch: list[tuple[datetime, int]] = []
    fetch_location = lib(account).fetch_location.side_effect

    async def fetch(sources: list[object]) -> object:
        reports = await fetch_location(sources)
        # The library has told the window by now; the accessory must not know yet.
        alignment_during_fetch.append(accessory.alignment)
        return reports

    lib(account).fetch_location.side_effect = fetch
    before = accessory.alignment
    await coordinator.reload()
    listen(coordinator, accessory)

    await coordinator.async_refresh()

    assert alignment_during_fetch == [before]
    assert accessory.alignment == (seen_at, seen_index)
    assert realigned_on == [threading.get_ident()]


@pytest.mark.asyncio
async def test_alignment_found_before_a_failure_is_kept(
    hass: HomeAssistant, storage: RuntimeStorage, coordinator: FindMyCoordinator
) -> None:
    """One accessory was found, then a request for the next one failed: do not forget it."""
    account = await add_account(hass, storage, "a")
    accessory = make_cached_accessory()
    plain = make_accessory()
    now = datetime.now(tz=UTC)
    seen_index = accessory.get_max_index(now) - 1
    plain_index = plain.get_max_index(now)

    async def fetch(sources: list[object]) -> dict[object, object]:
        for source in sources:
            if isinstance(source, SearchWindow):
                source.update_alignment(now, seen_index)
            else:
                cast("FindMyAccessory", source).update_alignment(now, plain_index)
        raise aiohttp.ClientConnectionError

    lib(account).fetch_location.side_effect = fetch
    await coordinator.reload()
    listen(coordinator, accessory)
    listen(coordinator, plain)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert accessory.alignment == (now, seen_index)
    assert (plain._alignment_date, plain._alignment_index) == (now, plain_index)


@pytest.mark.asyncio
async def test_located_accessory_causes_no_extra_requests(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    fetch_clock: FetchClock,
) -> None:
    apple = FakeApple()
    account = await add_fetching_account(hass, storage, "a", apple)
    accessory = make_cached_accessory()
    await coordinator.reload()
    listen(coordinator, accessory)

    for _ in range(3):
        apple.requests = 0
        await coordinator.async_refresh()
        fetch_clock.advance()

        assert source_types(account) == [SearchWindow]
        # A week of indices at 290 keys per request.
        assert apple.requests == 3


@pytest.mark.asyncio
async def test_catch_up_costs_at_most_one_window_per_interval(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    fetch_clock: FetchClock,
) -> None:
    """An accessory that nobody sees any more: the extra cost has to stay bounded."""
    apple = FakeApple()
    await add_fetching_account(hass, storage, "a", apple)
    accessory = make_unlocated_accessory(days_without_report=400)
    await coordinator.reload()
    listen(coordinator, accessory)

    requests: list[int] = []
    for _ in range(4):
        apple.requests = 0
        await coordinator.async_refresh()
        fetch_clock.advance()
        requests.append(apple.requests)

    # Three for the top window on every cycle; three more for the one catch-up window.
    assert requests == [6, 3, 3, 3]

    # An hour later the next window is due, further down.
    assert accessory._last_catch_up is not None
    first_cursor = accessory._catch_up_cursor
    accessory._last_catch_up -= CATCH_UP_INTERVAL
    apple.requests = 0
    await coordinator.async_refresh()

    assert apple.requests == 6
    assert accessory._catch_up_cursor is not None
    assert first_cursor is not None
    assert accessory._catch_up_cursor < first_cursor


@pytest.mark.asyncio
async def test_failed_fetch_searches_the_same_catch_up_window_again(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    fetch_clock: FetchClock,
) -> None:
    account = await add_account(hass, storage, "a")
    accessory = make_unlocated_accessory()
    lib(account).fetch_location.side_effect = TimeoutError()
    await coordinator.reload()
    listen(coordinator, accessory)

    await coordinator.async_refresh()

    failed_window = fetched_sources(account)[1]
    assert isinstance(failed_window, CatchUpWindow)
    assert accessory._catch_up_cursor is None

    # Not retried at once: the failed fetch may have sent its requests already.
    fetch_clock.advance()
    await coordinator.async_refresh()
    assert source_types(account) == [SearchWindow]

    assert accessory._last_catch_up is not None
    accessory._last_catch_up -= CATCH_UP_INTERVAL
    lib(account).fetch_location.side_effect = None
    fetch_clock.advance()
    await coordinator.async_refresh()

    retried_window = fetched_sources(account)[1]
    assert isinstance(retried_window, CatchUpWindow)
    assert retried_window.highest >= failed_window.highest
    assert accessory._catch_up_cursor == retried_window.lowest - 1


@pytest.mark.parametrize(
    ("top_age_hours", "window_age_hours", "expected"),
    [(1, 5, "top"), (5, 1, "window"), (None, 5, "window"), (1, None, "top")],
)
@pytest.mark.asyncio
async def test_newer_report_of_top_window_and_catch_up_window_wins(  # noqa: PLR0917
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    top_age_hours: int | None,
    window_age_hours: int | None,
    expected: str,
) -> None:
    account = await add_account(hass, storage, "a")
    accessory = make_unlocated_accessory()
    now = datetime.now(tz=UTC)
    reports = {
        name: None if age is None else FakeReport(now - timedelta(hours=age), b"")
        for name, age in (("top", top_age_hours), ("window", window_age_hours))
    }

    async def fetch(sources: list[object]) -> dict[object, object]:
        return {sources[0]: reports["top"], sources[1]: reports["window"]}

    lib(account).fetch_location.side_effect = fetch
    await coordinator.reload()
    listen(coordinator, accessory)

    await coordinator.async_refresh()

    assert coordinator.data == {accessory: reports[expected]}


@pytest.mark.asyncio
async def test_translated_error_keeps_its_traceback_in_the_debug_log(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    coordinator: FindMyCoordinator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A KeyError may be Apple's answer or a bug of ours; the short message cannot tell."""
    account = await add_account(hass, storage, "a")

    async def fetch(_devices: object) -> dict[object, object]:
        return {}["searchPartyToken"]

    lib(account).fetch_location.side_effect = fetch
    await coordinator.reload()
    caplog.set_level(logging.DEBUG, logger="custom_components.findmy.coordinator")

    await coordinator.async_refresh()

    assert isinstance(coordinator.last_exception, UpdateFailed)
    records = [r for r in caplog.records if r.getMessage() == "Fetching location reports failed"]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert records[0].exc_info is not None
    assert records[0].exc_info[0] is KeyError
    # The frame that raised, on the worker thread, is part of what gets logged.
    assert 'return {}["searchPartyToken"]' in caplog.text
