# ruff: noqa: SLF001, PLR2004, INP001, T201
# pyright: basic
"""Measures how long a location fetch keeps the event loop busy (R20).

Uses FindMy.py's real `LocationReportsFetcher`, so the measured work is the key handling a real
fetch does. Only the network is replaced: the account answers every request at once with no
reports, which makes the fetch walk its whole search window.

How it is measured: a task sleeps 10 ms in a loop and notes how late it wakes up. While the
fetch computes between two awaits nothing else runs, so the lateness is the length of the stall.

Machines differ a lot in speed, so the tests compare a cached accessory with a plain
`FindMyAccessory` on the same machine instead of checking against fixed milliseconds. Run with
`-s` to see the numbers.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import findmy.keys
import pytest
import pytest_asyncio
from findmy import AsyncAppleAccount, FindMyAccessory
from findmy.reports.reports import LocationReportsFetcher
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

from custom_components.findmy.key_cache import CachedFindMyAccessory
from custom_components.findmy.storage import RuntimeStorage

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from pathlib import Path

    from findmy import FindMyAccessoryMapping, RollingKeyPairSource

# The acceptance criterion: a fetch from warm keys stalls the loop at least this much less.
MIN_SPEEDUP = 10
# With empty caches the keys are derived in a worker thread while the loop keeps running.
COLD_START_MIN_SPEEDUP = 4
TICK = 0.01


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


async def warm(accessory: CachedFindMyAccessory) -> float:
    """Warm the keys the way the coordinator does, in a worker thread; returns the duration."""
    now = datetime.now(tz=UTC)
    started = time.perf_counter()
    _ = await asyncio.get_running_loop().run_in_executor(
        None, accessory.warm_keys, now - timedelta(days=7), now + accessory.interval
    )
    return time.perf_counter() - started


@pytest.mark.asyncio
async def test_fetch_from_warm_cache_barely_stalls_the_event_loop() -> None:
    data = accessory_data(paired_days=400, aligned_hours_ago=1)
    plain = FindMyAccessory.from_json(data)
    cached = cached_accessory(data)

    # The first fetch of a plain accessory also walks its secret chain since pairing; the
    # second one shows what every later fetch costs.
    plain_first = await measure_fetch(plain)
    plain_again = await measure_fetch(plain)
    warm_up = await warm(cached)
    cached_fetch = await measure_fetch(cached, runs=3)

    print()
    print(f"  plain, first fetch      : {plain_first}")
    print(f"  plain, second fetch     : {plain_again}")
    print(f"  cached, warm-up (thread): total {warm_up * 1000:7.1f} ms")
    print(f"  cached, fetch           : {cached_fetch}")

    assert cached_fetch.requests == plain_again.requests
    assert cached_fetch.total * MIN_SPEEDUP <= plain_again.total
    assert cached_fetch.longest_stall * MIN_SPEEDUP <= plain_again.longest_stall


@pytest.mark.asyncio
async def test_fetch_from_warm_cache_derives_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cause, independent of timing: no elliptic-curve work is left for the event loop."""
    cached = cached_accessory(accessory_data(paired_days=400, aligned_hours_ago=1))
    _ = await warm(cached)
    derivations = 0
    original = findmy.keys.ec.derive_private_key

    def counting(*args: object, **kwargs: object) -> object:
        nonlocal derivations
        derivations += 1
        return original(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(findmy.keys.ec, "derive_private_key", counting)
    account = FakeAccount()
    fetcher = LocationReportsFetcher(account)  # pyright: ignore[reportArgumentType]

    reports = await fetcher._fetch_accessory_reports(cached, only_latest=True)

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
    warm_up = await warm(cached)
    cached_fetch = await measure_fetch(cached, runs=3)

    print()
    print(f"  plain, 30 days unlocated : {plain_fetch}")
    print(f"  cached, warm-up (thread) : total {warm_up * 1000:7.1f} ms")
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
    yield instance
    await instance.async_stop(force=True)


async def measure_cold_refresh(hass: HomeAssistant, accessories: int) -> Measurement:
    """One coordinator refresh for new accessories, whose key caches are still empty."""
    raw_account = FakeAccount()
    fetcher = LocationReportsFetcher(raw_account)  # pyright: ignore[reportArgumentType]

    async def fetch_location(devices: list[FindMyAccessory]) -> object:
        history = await fetcher.fetch_location_history(devices)
        return dict.fromkeys(history)

    account = MagicMock(spec=AsyncAppleAccount)
    account.account_name = "test@example.com"
    account.first_name = account.last_name = "Test"
    account.fetch_location = AsyncMock(side_effect=fetch_location)
    storage = RuntimeStorage(hass)
    storage._entries["account"] = account
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
    hass: HomeAssistant,
) -> None:
    """The whole path through the coordinator, starting with empty caches.

    Nothing is warm after a restart, so the keys of every accessory have to be derived. That
    happens in a worker thread. The event loop still feels it, because both threads share the
    interpreter lock, but in short pauses instead of one long stall. Hence the lower factor.
    """
    plain = FindMyAccessory.from_json(accessory_data(paired_days=400, aligned_hours_ago=1))

    plain_fetch = await measure_fetch(plain)
    refreshes = [await measure_cold_refresh(hass, accessories=3) for _ in range(2)]
    refresh = min(refreshes, key=lambda measured: measured.longest_stall)

    print()
    print(f"  plain, one accessory                   : {plain_fetch}")
    print(f"  coordinator refresh, 3 cold accessories: {refresh}")

    assert refresh.requests >= 3
    # Three accessories from cold caches stall the loop far less than one plain accessory.
    assert refresh.longest_stall * COLD_START_MIN_SPEEDUP <= plain_fetch.longest_stall
