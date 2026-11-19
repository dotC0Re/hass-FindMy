# ruff: noqa: T201
# pyright: basic
"""Measures how long a location fetch keeps the event loop busy (R20).

Two things are measured. The first is what the key cache saves: the tests use FindMy.py's real
`LocationReportsFetcher`, so the measured work is the key handling a real fetch does. Only the
network is replaced: the account answers every request at once with no reports, which makes the
fetch walk its whole search window. These fetches run on the measuring loop, to show the cost of
the library call itself. The second is what the worker thread saves: a fetch through the
coordinator, including one that computes for half a second without a single await.

How it is measured: a task sleeps 10 ms in a loop and notes how late it wakes up. While the
fetch computes between two awaits nothing else runs, so the lateness is the length of the stall.

Machines differ a lot in speed, so the tests compare a cached accessory with a plain
`FindMyAccessory` on the same machine instead of checking against fixed milliseconds. The one
exception is the limit for a fetch on the worker thread, which is what was asked for. Run with
`-s` to see the numbers.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import findmy.keys
import pytest
import pytest_asyncio
from findmy import FindMyAccessory
from findmy.reports.reports import LocationReportsFetcher
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

from custom_components.findmy.key_cache import CachedFindMyAccessory
from custom_components.findmy.storage import RuntimeStorage

from .conftest import FetchClock, library_mock, open_account

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from pathlib import Path

    from findmy import FindMyAccessoryMapping, RollingKeyPairSource

    from custom_components.findmy.key_cache import SearchWindow

# The acceptance criterion: a fetch from finished keys stalls the loop at least this much less.
MIN_SPEEDUP = 10
# With empty caches the keys are derived in an executor thread while the loop keeps running.
COLD_START_MIN_SPEEDUP = 4
TICK = 0.01
# How long the fake fetch computes without yielding, like the decryption of many reports.
CPU_BURN = 0.5
# The longest pause of the event loop that is acceptable while a fetch computes on the worker.
MAX_STALL_DURING_WORKER_FETCH = 0.1


class FakeAccount:
    """Answers at once and without reports, like an account whose accessories were not seen."""

    def __init__(self) -> None:
        self.requests = 0

    async def fetch_raw_reports(self, _devices: object) -> list[object]:
        self.requests += 1
        await asyncio.sleep(0)
        return []


@dataclass(frozen=True)
class Measurement:
    total: float
    longest_stall: float
    requests: int = 0

    def __str__(self) -> str:
        return (
            f"total {self.total * 1000:7.1f} ms, longest stall {self.longest_stall * 1000:7.1f} ms"
            f", {self.requests} request(s)"
        )


async def measure_stall(work: Callable[[], Awaitable[object]]) -> Measurement:
    """Run `work` on the event loop and measure how long it kept the loop from running."""
    stalls: list[float] = []
    running = True

    async def ticker() -> None:
        while running:
            started = time.perf_counter()
            await asyncio.sleep(TICK)
            stalls.append(time.perf_counter() - started - TICK)

    task = asyncio.create_task(ticker())
    # Let the ticker settle, then forget what happened before the work started.
    await asyncio.sleep(5 * TICK)
    stalls.clear()

    started = time.perf_counter()
    _ = await work()
    total = time.perf_counter() - started

    # A stall is only noticed when the ticker wakes up again.
    await asyncio.sleep(3 * TICK)
    running = False
    await task
    return Measurement(total, max(stalls, default=0.0))


async def measure_fetch(accessory: RollingKeyPairSource, runs: int = 1) -> Measurement:
    """Best of `runs` fetches. The minimum is what the code costs; the rest is other load."""
    best: Measurement | None = None
    for _ in range(runs):
        account = FakeAccount()
        fetcher = LocationReportsFetcher(account)  # pyright: ignore[reportArgumentType]

        async def fetch(fetcher: LocationReportsFetcher = fetcher) -> object:
            return await fetcher._fetch_accessory_reports(accessory, only_latest=True)

        measured = await measure_stall(fetch)
        measured = Measurement(measured.total, measured.longest_stall, account.requests)
        if best is None or measured.total < best.total:
            best = measured
    assert best is not None
    return best


def accessory_data(*, paired_days: int, aligned_hours_ago: float) -> FindMyAccessoryMapping:
    now = datetime.now(tz=UTC)
    return {
        "type": "accessory",
        "master_key": os.urandom(28).hex(),
        "skn": os.urandom(32).hex(),
        "sks": os.urandom(32).hex(),
        "paired_at": (now - timedelta(days=paired_days)).isoformat(),
        "name": "Test tag",
        "model": None,
        "identifier": "test-accessory",
        "group_identifier": None,
        "serial_number": None,
        "alignment_date": (now - timedelta(hours=aligned_hours_ago)).isoformat(),
        "alignment_index": int((paired_days * 24 - aligned_hours_ago) * 4),
    }


def cached_accessory(data: FindMyAccessoryMapping) -> CachedFindMyAccessory:
    accessory = CachedFindMyAccessory.from_json(data)
    assert isinstance(accessory, CachedFindMyAccessory)
    return accessory


async def cut_window(accessory: CachedFindMyAccessory) -> tuple[SearchWindow, float]:
    """Cut the window of a fetch the way the coordinator does, in an executor thread.

    Returns the window, which is what FindMy.py searches, and how long deriving its keys took.
    """
    now = datetime.now(tz=UTC)
    started = time.perf_counter()
    window = await asyncio.get_running_loop().run_in_executor(None, accessory.search_window, now)
    return window, time.perf_counter() - started


@pytest.mark.asyncio
async def test_fetch_from_warm_cache_barely_stalls_the_event_loop() -> None:
    data = accessory_data(paired_days=400, aligned_hours_ago=1)
    plain = FindMyAccessory.from_json(data)
    cached = cached_accessory(data)

    # The first fetch of a plain accessory also walks its secret chain since pairing; the
    # second one shows what every later fetch costs.
    plain_first = await measure_fetch(plain)
    plain_again = await measure_fetch(plain)
    window, derive = await cut_window(cached)
    cached_fetch = await measure_fetch(window, runs=3)

    print()
    print(f"  plain, first fetch      : {plain_first}")
    print(f"  plain, second fetch     : {plain_again}")
    print(f"  cached, window (thread) : total {derive * 1000:7.1f} ms")
    print(f"  cached, fetch           : {cached_fetch}")

    assert cached_fetch.requests == plain_again.requests
    assert cached_fetch.total * MIN_SPEEDUP <= plain_again.total
    assert cached_fetch.longest_stall * MIN_SPEEDUP <= plain_again.longest_stall


@pytest.mark.asyncio
async def test_fetch_from_warm_cache_derives_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cause, independent of timing: no elliptic-curve work is left for the fetch."""
    cached = cached_accessory(accessory_data(paired_days=400, aligned_hours_ago=1))
    window, _ = await cut_window(cached)
    derivations = 0
    original = findmy.keys.ec.derive_private_key

    def counting(*args: object, **kwargs: object) -> object:
        nonlocal derivations
        derivations += 1
        return original(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(findmy.keys.ec, "derive_private_key", counting)
    account = FakeAccount()
    fetcher = LocationReportsFetcher(account)  # pyright: ignore[reportArgumentType]

    reports = await fetcher._fetch_accessory_reports(window, only_latest=True)

    assert reports == []
    assert account.requests > 0
    assert derivations == 0


@pytest.mark.asyncio
async def test_accessory_without_recent_location_costs_no_more_than_any_other() -> None:
    """R21: a month without a report meant ten requests and seconds of stall per fetch."""
    data = accessory_data(paired_days=400, aligned_hours_ago=30 * 24)
    plain = FindMyAccessory.from_json(data)
    cached = cached_accessory(data)

    plain_fetch = await measure_fetch(plain)
    window, derive = await cut_window(cached)
    cached_fetch = await measure_fetch(window, runs=3)

    print()
    print(f"  plain, 30 days unlocated : {plain_fetch}")
    print(f"  cached, window (thread)  : total {derive * 1000:7.1f} ms")
    print(f"  cached, 30 days unlocated: {cached_fetch}")

    # A week of indices at 290 keys per request.
    assert cached_fetch.requests <= 3
    assert plain_fetch.requests > cached_fetch.requests
    assert cached_fetch.total * MIN_SPEEDUP <= plain_fetch.total
    assert cached_fetch.longest_stall * MIN_SPEEDUP <= plain_fetch.longest_stall


@pytest.mark.asyncio
async def test_catch_up_window_is_searched_without_stalling_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extra search for an accessory below its top window must not bring the stall back."""
    # A week of indices, the same amount of work as the catch-up window below.
    plain = FindMyAccessory.from_json(accessory_data(paired_days=400, aligned_hours_ago=1))
    cached = cached_accessory(accessory_data(paired_days=400, aligned_hours_ago=30 * 24))
    planned = cached.plan_catch_up(datetime.now(tz=UTC))
    assert planned is not None

    started = time.perf_counter()
    window = await asyncio.get_running_loop().run_in_executor(
        None, cached.catch_up_window, *planned
    )
    build = time.perf_counter() - started
    derivations = 0
    original = findmy.keys.ec.derive_private_key

    def counting(*args: object, **kwargs: object) -> object:
        nonlocal derivations
        derivations += 1
        return original(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(findmy.keys.ec, "derive_private_key", counting)
    window_fetch = await measure_fetch(window, runs=3)
    assert derivations == 0
    plain_fetch = await measure_fetch(plain)

    print()
    print(f"  plain, a week of indices     : {plain_fetch}")
    print(f"  catch-up window, build (thread): total {build * 1000:7.1f} ms")
    print(f"  catch-up window, fetch       : {window_fetch}")

    assert window.highest - window.lowest + 1 == 7 * 96
    assert window_fetch.requests <= 3
    assert window_fetch.total * MIN_SPEEDUP <= plain_fetch.total
    assert window_fetch.longest_stall * MIN_SPEEDUP <= plain_fetch.longest_stall


@pytest_asyncio.fixture
async def hass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[HomeAssistant]:
    instance = HomeAssistant(str(tmp_path))
    frame.async_setup(instance)
    # See test_coordinator.py: the frame helper raises for custom integrations on Windows.
    monkeypatch.setattr(frame, "report_usage", lambda *_args, **_kwargs: None)
    instance.config_entries = MagicMock()
    instance.config_entries.async_get_entry.return_value = None
    yield instance
    # Also stops the worker thread the storage of a test started.
    await instance.async_stop(force=True)


async def measure_cold_refresh(
    hass: HomeAssistant, clock: FetchClock, accessories: int
) -> Measurement:
    """One coordinator refresh for new accessories, whose key caches are still empty."""
    raw_account = FakeAccount()
    fetcher = LocationReportsFetcher(raw_account)  # pyright: ignore[reportArgumentType]

    async def fetch_location(devices: list[FindMyAccessory]) -> object:
        history = await fetcher.fetch_location_history(devices)
        return dict.fromkeys(history)

    # Every measurement starts Home Assistant anew on the same configuration, whose fetch
    # budget remembers the measurement before.
    clock.advance()
    storage = RuntimeStorage(hass)
    account = await open_account(storage, "account")
    library_mock(account).fetch_location.side_effect = fetch_location
    coordinator = storage.coordinator
    for _ in range(accessories):
        device = cached_accessory(accessory_data(paired_days=400, aligned_hours_ago=1))
        coordinator.async_add_listener(lambda: None, device)
    await coordinator.reload()

    measured = await measure_stall(coordinator.async_refresh)

    assert coordinator.last_update_success is True
    return Measurement(measured.total, measured.longest_stall, raw_account.requests)


@pytest.mark.asyncio
async def test_first_refresh_after_a_restart_keeps_the_event_loop_free(
    hass: HomeAssistant, fetch_clock: FetchClock
) -> None:
    """The whole path through the coordinator, starting with empty caches.

    Nothing is derived yet after a restart, so the keys of every accessory have to be. That
    happens in an executor thread, and the fetch itself on the FindMy worker thread. The event
    loop still feels both, because all threads share the interpreter lock, but in short pauses
    instead of one long stall. Hence the lower factor.
    """
    plain = FindMyAccessory.from_json(accessory_data(paired_days=400, aligned_hours_ago=1))

    plain_fetch = await measure_fetch(plain)
    refreshes = [await measure_cold_refresh(hass, fetch_clock, accessories=3) for _ in range(2)]
    refresh = min(refreshes, key=lambda measured: measured.longest_stall)

    print()
    print(f"  plain, one accessory                   : {plain_fetch}")
    print(f"  coordinator refresh, 3 cold accessories: {refresh}")

    assert refresh.requests >= 3
    # Three accessories from cold caches stall the loop far less than one plain accessory.
    assert refresh.longest_stall * COLD_START_MIN_SPEEDUP <= plain_fetch.longest_stall


async def burn_cpu(seconds: float) -> None:
    """Compute without yielding: no await, and nothing that releases the interpreter lock."""
    end = time.perf_counter() + seconds
    count = 0
    while time.perf_counter() < end:
        count += 1


async def measure_burning_refresh(hass: HomeAssistant, clock: FetchClock) -> Measurement:
    """One coordinator refresh whose fetch computes for CPU_BURN seconds in one piece."""

    async def fetch_location(devices: list[object]) -> object:
        await burn_cpu(CPU_BURN)
        return dict.fromkeys(devices)

    # Every measurement starts Home Assistant anew on the same configuration, whose fetch
    # budget remembers the measurement before.
    clock.advance()
    storage = RuntimeStorage(hass)
    account = await open_account(storage, "account")
    library_mock(account).fetch_location.side_effect = fetch_location
    coordinator = storage.coordinator
    await coordinator.reload()

    measured = await measure_stall(coordinator.async_refresh)

    assert coordinator.last_update_success is True
    library_mock(account).fetch_location.assert_awaited_once()
    return measured


@pytest.mark.asyncio
async def test_cpu_bound_fetch_does_not_stall_the_event_loop(
    hass: HomeAssistant, fetch_clock: FetchClock
) -> None:
    """R20 stage 2: decrypting reports and emulating Anisette no longer freeze Home Assistant.

    The fetch of the fake account computes for half a second without an await. On the event
    loop that is half a second in which Home Assistant does nothing else. On the worker thread
    the loop keeps running: the interpreter hands over between the threads every few
    milliseconds.
    """
    on_the_loop = await measure_stall(lambda: burn_cpu(CPU_BURN))
    # The best of three: a pause of the whole machine is not a property of the code.
    refreshes = [await measure_burning_refresh(hass, fetch_clock) for _ in range(3)]
    refresh = min(refreshes, key=lambda measured: measured.longest_stall)

    print()
    print(f"  computing on the event loop    : {on_the_loop}")
    for measured in refreshes:
        print(f"  refresh, computing on the worker: {measured}")

    # The measurement itself works: the same computation on the loop is seen as one stall.
    assert on_the_loop.longest_stall >= 0.8 * CPU_BURN
    # The fetch did compute for that long, on another thread.
    assert refresh.total >= CPU_BURN
    assert refresh.longest_stall <= MAX_STALL_DURING_WORKER_FETCH
