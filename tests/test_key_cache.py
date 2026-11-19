# pyright: basic
"""Tests for the key cache of rolling-key accessories.

Checks that the cache hands out exactly the keys FindMy.py derives, derives each of them once,
stays bounded, survives concurrent use, and is shared by the fetch and the Bluetooth matching.
Also covers the search windows a fetch gets instead of the accessory, and that the alignment
stays consistent between threads. Keys are random; nothing here talks to Apple.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import findmy.keys
import pytest
from findmy import FindMyAccessory, KeyPair, KeyPairType
from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

from custom_components.findmy import key_cache
from custom_components.findmy.key_cache import (
    CATCH_UP_INTERVAL,
    CachedFindMyAccessory,
    CachedKeyPair,
    CatchUpWindow,
    SearchWindow,
)
from custom_components.findmy.local_bluetooth import build_candidate_key_lookup
from custom_components.findmy.storage import RuntimeStorage

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from findmy import FindMyAccessoryMapping

WEEK = 7 * 96


def make_pair(
    *, paired_days: int = 30, aligned_hours_ago: float = 1.0
) -> tuple[CachedFindMyAccessory, FindMyAccessory]:
    """A cached accessory and a plain one with the same random keys and alignment."""
    now = datetime.now(tz=UTC)
    data: FindMyAccessoryMapping = {
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
    cached = CachedFindMyAccessory.from_json(data)
    assert isinstance(cached, CachedFindMyAccessory)
    return cached, FindMyAccessory.from_json(data)


def describe(keys: set[KeyPair]) -> set[tuple[bytes, KeyPairType, str]]:
    return {(key.private_key_bytes, key.key_type, key.hashed_adv_key_b64) for key in keys}


@pytest.fixture
def derivations(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Counts elliptic-curve derivations, the operation the cache exists to avoid."""
    calls: list[int] = []
    original = findmy.keys.ec.derive_private_key

    def counting(*args: object, **kwargs: object) -> object:
        calls.append(threading.get_ident())
        return original(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(findmy.keys.ec, "derive_private_key", counting)
    return calls  # pyright: ignore[reportReturnType]


def test_cached_keys_match_the_library() -> None:
    cached, plain = make_pair()
    top = cached.get_max_index(datetime.now(tz=UTC))

    # Spans several days, so the change of secondary keys at multiples of 96 is covered.
    for ind in range(top - 300, top + 1):
        assert describe(cached.keys_at(ind)) == describe(plain.keys_at(ind)), ind

    assert cached.keys_at(-1) == set()


def test_cached_key_pair_equals_and_hashes_like_a_plain_one() -> None:
    plain = KeyPair(os.urandom(28), KeyPairType.PRIMARY, "name")
    cached = CachedKeyPair.from_keypair(plain)
    built = CachedKeyPair(plain.private_key_bytes, KeyPairType.PRIMARY, "name")

    for candidate in (cached, built):
        assert candidate == plain
        assert plain == candidate
        assert hash(candidate) == hash(plain)
        assert candidate.adv_key_bytes == plain.adv_key_bytes
        assert candidate.hashed_adv_key_bytes == plain.hashed_adv_key_bytes
        assert candidate.private_key_bytes == plain.private_key_bytes
        assert candidate.key_type is KeyPairType.PRIMARY
        assert candidate.name == "name"
    assert len({plain, cached, built}) == 1
    assert cached != KeyPair(os.urandom(28))


def test_wrapping_a_key_pair_does_not_derive_again(derivations: list[int]) -> None:
    plain = KeyPair(os.urandom(28))
    derivations.clear()

    cached = CachedKeyPair.from_keypair(plain)
    _ = (cached.adv_key_bytes, cached.hashed_adv_key_b64, hash(cached), cached == plain)

    assert derivations == []


def test_a_week_of_keys_is_derived_once_per_key(derivations: list[int]) -> None:
    """R20: FindMy.py derives 3 keys per index, 2016 per week; 2 of 3 are repeats."""
    cached, plain = make_pair()
    now = datetime.now(tz=UTC)

    window = cached.search_window(now)
    derived = window.highest - window.lowest + 1

    assert derived in (WEEK, WEEK + 1)
    assert len(cached._key_cache) == derived
    # One primary key per index, and two secondary keys per started day in the window.
    assert len(derivations) <= derived + 2 * (derived // 96 + 2)

    derivations.clear()
    top = plain.get_max_index(now)
    for ind in range(top, top - WEEK, -1):
        _ = plain.keys_at(ind)
    assert len(derivations) == 3 * WEEK


def test_search_window_only_derives_what_is_new(derivations: list[int]) -> None:
    cached, _ = make_pair()
    now = datetime.now(tz=UTC)
    first = cached.search_window(now)
    assert derivations
    derivations.clear()

    again = cached.search_window(now)
    assert derivations == []
    assert again._keys == first._keys

    # A fetch 15 minutes later needs a single new index.
    later = cached.search_window(now + cached.interval)
    assert later.highest == first.highest + 1
    assert 1 <= len(derivations) <= 3


def test_search_window_holds_the_range_and_the_keys_findmy_would_search(
    derivations: list[int],
) -> None:
    cached, plain = make_pair()
    now = datetime.now(tz=UTC)
    start = now - timedelta(days=7)

    window = cached.search_window(now)

    assert type(window) is SearchWindow
    assert window.accessory is cached
    assert (window.lowest, window.highest) == (plain.get_min_index(start), plain.get_max_index(now))
    # Whatever moment FindMy.py asks about, the bounds are those the window was cut with.
    assert window.get_min_index(now - timedelta(days=30)) == window.lowest
    assert window.get_max_index(now + timedelta(days=30)) == window.highest
    for ind in (window.lowest, (window.lowest + window.highest) // 2, window.highest):
        assert describe(window.keys_at(ind)) == describe(plain.keys_at(ind))
    assert window.keys_at(window.lowest - 1) == set()
    assert window.keys_at(window.highest + 1) == set()

    # Searching it derives nothing, and the caller owns the sets it gets.
    derivations.clear()
    for ind in range(window.highest, window.lowest - 1, -1):
        window.keys_at(ind).clear()
        assert len(window.keys_at(ind)) == 3
    assert derivations == []


def test_search_window_is_independent_of_the_accessory_it_was_cut_from() -> None:
    """R20: the worker thread searches the window while the event loop uses the accessory."""
    cached, _ = make_pair()
    now = datetime.now(tz=UTC)
    window = cached.search_window(now)
    bounds = (window.lowest, window.highest)
    keys = dict(window._keys)
    before = cached.alignment

    # The fetch finds reports; FindMy.py reports the alignment to what it searched.
    nothing_found = window.found_alignment
    assert nothing_found is None
    window.update_alignment(now - timedelta(minutes=30), window.highest - 2)
    window.update_alignment(now - timedelta(minutes=10), window.highest - 1)
    # Older or lower than what is known: ignored, as FindMyAccessory does.
    window.update_alignment(now - timedelta(minutes=20), window.highest)
    window.update_alignment(now, window.highest - 3)

    found = window.found_alignment
    assert found is not None
    assert found == (now - timedelta(minutes=10), window.highest - 1)
    assert cached.alignment == before

    # Meanwhile the Bluetooth matching realigns the accessory and the cache moves on.
    cached.update_alignment(now, window.highest + 40)
    for ind in range(window.highest + 1000, window.highest + 2000):
        _ = cached.keys_at(ind)

    assert (window.lowest, window.highest) == bounds
    assert window._keys == keys
    assert len(window.keys_at(window.lowest)) == 3
    # Applied afterwards, the older alignment of the window does not undo the newer one.
    cached.update_alignment(*found)
    assert cached.alignment == (now, bounds[1] + 40)


def test_report_that_changes_nothing_is_no_found_alignment() -> None:
    cached, _ = make_pair()
    now = datetime.now(tz=UTC)
    window = cached.search_window(now)
    date, index = cached.alignment

    window.update_alignment(date - timedelta(hours=1), index + 1)
    window.update_alignment(date + timedelta(hours=1), index - 1)
    window.update_alignment(date, index)

    assert window.found_alignment is None


def test_alignment_is_never_seen_half_updated_from_another_thread() -> None:
    """Executor threads compute bounds while the event loop realigns the accessory.

    FindMy.py stores date and index one after the other. Read in between, a new date with an old
    index gives bounds that are too low by exactly what the index moved. Every pair written here
    yields the same highest index, so any other value is a torn read.
    """
    cached, _ = make_pair()
    date, index = cached.alignment
    probe = date + timedelta(days=30)
    expected = cached.get_max_index(probe)
    stop = threading.Event()
    seen: set[int] = set()
    pairs: set[tuple[datetime, int]] = set()

    def read() -> None:
        while not stop.is_set():
            seen.add(cached.get_max_index(probe))
            pairs.add(cached.alignment)
            _ = cached.to_json()

    readers = [threading.Thread(target=read) for _ in range(4)]
    switch_interval = sys.getswitchinterval()
    # Switch threads as often as possible, so that a reader does get in between the two stores.
    sys.setswitchinterval(1e-6)
    for reader in readers:
        reader.start()
    try:
        # Forwards along the line on which the index keeps pace with the clock, and not past
        # the probed moment.
        for step in range(1, 2000):
            cached.update_alignment(date + step * cached.interval, index + step)
    finally:
        stop.set()
        for reader in readers:
            reader.join()
        sys.setswitchinterval(switch_interval)

    assert seen == {expected}
    assert all((pair[0] - date) // cached.interval == pair[1] - index for pair in pairs)


def test_reading_derived_keys_derives_nothing(derivations: list[int]) -> None:
    cached, _ = make_pair()
    now = datetime.now(tz=UTC)
    _ = cached.search_window(now)
    derivations.clear()

    top = cached.get_max_index(now)
    for ind in range(top, cached.get_min_index(now - timedelta(days=7)) - 1, -1):
        keys = cached.keys_at(ind)
        assert len(keys) == 3
        assert all(isinstance(key, CachedKeyPair) for key in keys)

    assert derivations == []


def test_keys_at_returns_a_set_the_caller_may_change() -> None:
    cached, _ = make_pair()

    first = cached.keys_at(100)
    first.clear()

    assert len(cached.keys_at(100)) == 3


def test_cache_stays_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(key_cache, "MAX_CACHED_INDICES", 200)
    monkeypatch.setattr(key_cache, "_MAX_CACHED_SECONDARY_SPANS", 200 // 96 + 2)
    cached, _ = make_pair()

    for ind in range(1000, 2000):
        _ = cached.keys_at(ind)

    assert sorted(cached._key_cache) == list(range(1800, 2000))
    assert len(cached._secondary_cache) <= 200 // 96 + 2


def test_cache_follows_a_window_that_moves_down(
    monkeypatch: pytest.MonkeyPatch, derivations: list[int]
) -> None:
    """An accessory whose index lags realigns downwards; the lower window must be kept too."""
    monkeypatch.setattr(key_cache, "MAX_CACHED_INDICES", 200)
    cached, _ = make_pair()
    for ind in range(2000, 1800, -1):
        _ = cached.keys_at(ind)

    # Same order as a fetch: newest first.
    for ind in range(1200, 1000, -1):
        _ = cached.keys_at(ind)
    assert sorted(cached._key_cache) == list(range(1001, 1201))

    derivations.clear()
    for ind in range(1200, 1000, -1):
        _ = cached.keys_at(ind)
    assert derivations == []


def test_concurrent_use_derives_every_index_once(derivations: list[int]) -> None:
    cached, plain = make_pair()
    indices = list(range(2000, 2200))
    barrier = threading.Barrier(8)

    def worker(offset: int) -> list[set[tuple[bytes, KeyPairType, str]]]:
        barrier.wait()
        # Overlapping ranges in different orders, so the threads collide on the same indices.
        order = indices[offset:] + indices[:offset] if offset % 2 else indices[::-1]
        return [describe(cached.keys_at(ind)) for ind in sorted(order)] + [
            describe(cached.keys_at(ind)) for ind in order[:10]
        ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [future.result() for future in [pool.submit(worker, i * 20) for i in range(8)]]

    expected = [describe(plain.keys_at(ind)) for ind in indices]
    for result in results:
        assert result[: len(indices)] == expected
    assert sorted(cached._key_cache) == indices
    # 200 primary keys plus the secondary keys of the three days the range touches.
    assert len(derivations) - 3 * len(indices) <= 200 + 2 * 3


def test_search_window_is_limited_for_an_accessory_that_was_never_located() -> None:
    """R21: without a limit, every index since pairing is searched on every fetch."""
    now = datetime.now(tz=UTC)
    cached = CachedFindMyAccessory(
        master_key=os.urandom(28),
        skn=os.urandom(32),
        sks=os.urandom(32),
        paired_at=now - timedelta(days=1000),
    )
    plain_min = FindMyAccessory.get_min_index(cached, now - timedelta(days=7))
    newest = cached.get_max_index(now)
    assert plain_min == 0
    assert newest == 1000 * 96

    lowest = cached.get_min_index(now - timedelta(days=7))

    assert key_cache.MAX_SEARCH_INDICES == WEEK
    assert newest - lowest + 1 == WEEK
    # The searched range and the 12 hours the Bluetooth matching looks ahead fit the cache.
    assert key_cache.MAX_SEARCH_INDICES + 48 <= key_cache.MAX_CACHED_INDICES


def test_accessory_lagging_more_than_a_week_is_outside_the_top_window() -> None:
    """The top window alone does not find it; the catch-up windows below are what does."""
    cached, plain = make_pair(paired_days=60, aligned_hours_ago=30 * 24)
    now = datetime.now(tz=UTC)
    start = now - timedelta(days=7)
    newest = cached.get_max_index(now)
    # Ten days without power since the alignment: the index is 960 behind the clock.
    lagging_index = newest - 10 * 96

    assert plain.get_min_index(start) <= lagging_index
    assert cached.get_min_index(start) > lagging_index
    # A lag of less than a week is still covered.
    assert cached.get_min_index(start) <= newest - 6 * 96


def test_bluetooth_window_is_limited_too() -> None:
    cached, _ = make_pair(paired_days=1000, aligned_hours_ago=900 * 24)
    now = datetime.now(tz=UTC)

    candidates = build_candidate_key_lookup(cached, now)

    indices = {key.index for keys in candidates.values() for key in keys}
    assert max(indices) - min(indices) + 1 <= WEEK + 48
    assert len(cached._key_cache) <= key_cache.MAX_CACHED_INDICES


def test_search_window_of_a_recently_located_accessory_is_unchanged() -> None:
    cached, plain = make_pair(aligned_hours_ago=1.0)
    start = datetime.now(tz=UTC) - timedelta(days=7)

    assert cached.get_min_index(start) == plain.get_min_index(start)


def planned_window(accessory: CachedFindMyAccessory, now: datetime) -> CatchUpWindow:
    """Plan a catch-up and mark it as searched, without spending time on deriving its keys."""
    planned = accessory.plan_catch_up(now)
    assert planned is not None
    window = CatchUpWindow(accessory, *planned, {}, accessory.alignment)
    accessory.catch_up_searched(window)
    return window


def test_recently_located_accessory_needs_no_catch_up() -> None:
    cached, _ = make_pair(aligned_hours_ago=1.0)
    now = datetime.now(tz=UTC)

    # Whatever the rounding of the two halves of the week, there is no gap to catch up on.
    for minutes in range(0, 180, 7):
        assert cached.plan_catch_up(now + timedelta(minutes=minutes)) is None


def test_first_catch_up_window_lies_directly_below_the_top_window() -> None:
    cached, _ = make_pair(paired_days=60, aligned_hours_ago=30 * 24)
    now = datetime.now(tz=UTC)
    newest = cached.get_max_index(now)

    planned = cached.plan_catch_up(now)

    assert planned == (newest - 2 * WEEK + 1, newest - WEEK)
    # An accessory that is ten days behind the clock is in it.
    assert newest - 2 * WEEK + 1 <= newest - 10 * 96 <= newest - WEEK


def test_catch_up_is_planned_at_most_once_per_interval() -> None:
    cached, _ = make_pair(paired_days=60, aligned_hours_ago=30 * 24)
    now = datetime.now(tz=UTC)

    assert cached.plan_catch_up(now) is not None
    assert cached.plan_catch_up(now + timedelta(minutes=1)) is None
    assert cached.plan_catch_up(now + CATCH_UP_INTERVAL - timedelta(seconds=1)) is None
    assert cached.plan_catch_up(now + CATCH_UP_INTERVAL) is not None


def test_sweep_covers_the_whole_gap_and_starts_over() -> None:
    cached, plain = make_pair(paired_days=60, aligned_hours_ago=30 * 24)
    now = datetime.now(tz=UTC)
    alignment_index = plain.get_min_index(now - timedelta(days=7))
    gap_top = cached.get_max_index(now) - WEEK

    windows: list[CatchUpWindow] = []
    while not windows or windows[-1].lowest > alignment_index:
        windows.append(planned_window(cached, now + len(windows) * CATCH_UP_INTERVAL))
        assert len(windows) < 10

    assert windows[0].highest == gap_top
    assert windows[-1].lowest == alignment_index
    for window in windows:
        assert window.highest - window.lowest + 1 <= WEEK
    for upper, lower in pairwise(windows):
        # Overlap: the indices an accessory can advance within an hour, and one to spare.
        assert lower.highest == upper.lowest - 1 + 4 + 1
    # 30 days without alignment are 2880 indices, 2208 of them below the top window.
    assert len(windows) == 4

    # The bottom is reached; the next window starts at the top again, where the top window of
    # three hours ago has moved on by 12 indices.
    restarted = planned_window(cached, now + len(windows) * CATCH_UP_INTERVAL)
    assert restarted.highest == gap_top + 4 * len(windows)


def test_advancing_accessory_cannot_slip_between_two_windows() -> None:
    """The accessory moves up while the windows move down; the overlap has to catch it."""
    now = datetime.now(tz=UTC)

    for lag in range(WEEK, 29 * 96, 13):
        cached, _ = make_pair(paired_days=60, aligned_hours_ago=30 * 24)
        true_index = cached.get_max_index(now) - lag
        found_after: int | None = None
        for hour in range(8):
            window = planned_window(cached, now + hour * CATCH_UP_INTERVAL)
            # A running accessory advances four indices per hour, as the clock does.
            if window.lowest <= true_index + 4 * hour <= window.highest:
                found_after = hour
                break

        assert found_after is not None, lag
        assert found_after <= (lag - WEEK) // (WEEK - 5) + 1, lag


def test_overlap_grows_with_the_pause_between_two_catch_ups() -> None:
    """No fetch for days (Home Assistant busy, account signed out): the accessory moved on."""
    cached, _ = make_pair(paired_days=60, aligned_hours_ago=30 * 24)
    now = datetime.now(tz=UTC)
    first = planned_window(cached, now)

    # Two days are 192 indices the accessory can have advanced from below the first window.
    resumed = planned_window(cached, now + timedelta(days=2))
    assert resumed.highest == first.lowest - 1 + 192 + 1
    assert resumed.highest - resumed.lowest + 1 == WEEK


def test_catch_up_window_has_its_own_keys(derivations: list[int]) -> None:
    cached, plain = make_pair(paired_days=60, aligned_hours_ago=30 * 24)
    now = datetime.now(tz=UTC)
    _ = cached.search_window(now)
    top_keys = dict(cached._key_cache)
    top_secondary = dict(cached._secondary_cache)
    planned = cached.plan_catch_up(now)
    assert planned is not None
    lowest, highest = planned
    derivations.clear()

    window = cached.catch_up_window(lowest, highest)

    # Every key once: a primary key per index and two secondary keys per started day.
    assert len(derivations) <= WEEK + 2 * (WEEK // 96 + 2)
    # The cache of the top window is neither extended nor displaced.
    assert cached._key_cache == top_keys
    assert cached._secondary_cache == top_secondary

    start = now - timedelta(days=7)
    assert (window.get_min_index(start), window.get_max_index(now)) == (lowest, highest)
    assert window.accessory is cached
    for ind in (lowest, (lowest + highest) // 2, highest):
        assert describe(window.keys_at(ind)) == describe(plain.keys_at(ind))
    assert window.keys_at(lowest - 1) == set()
    assert window.keys_at(highest + 1) == set()

    # Searching the window and the top window afterwards finds finished keys only.
    derivations.clear()
    for ind in range(highest, lowest - 1, -1):
        assert len(window.keys_at(ind)) == 3
    for ind in top_keys:
        _ = cached.keys_at(ind)
    assert derivations == []


def test_report_in_a_catch_up_window_closes_the_gap() -> None:
    cached, _ = make_pair(paired_days=60, aligned_hours_ago=30 * 24)
    now = datetime.now(tz=UTC)
    true_index = cached.get_max_index(now) - 10 * 96
    window = planned_window(cached, now)

    # What FindMy.py does with a report it found under a key of the window, on the worker
    # thread, and what the coordinator does with the result on the event loop.
    window.update_alignment(now - timedelta(hours=1), true_index)
    assert cached._alignment_index != true_index
    found = window.found_alignment
    assert found is not None
    assert found == (now - timedelta(hours=1), true_index)
    cached.update_alignment(*found)

    assert cached._alignment_index == true_index
    assert cached.get_min_index(now - timedelta(days=7)) <= true_index <= cached.get_max_index(now)
    assert cached.plan_catch_up(now + CATCH_UP_INTERVAL) is None


def test_bluetooth_candidates_come_from_the_shared_cache(derivations: list[int]) -> None:
    cached, _ = make_pair()
    now = datetime.now(tz=UTC)

    from_cache = build_candidate_key_lookup(cached, now)

    plain = FindMyAccessory.from_json(cached.to_json())
    assert from_cache == build_candidate_key_lookup(plain, now)
    assert from_cache

    # The next refresh, and a fetch of the same range, find the keys instead of deriving them.
    derivations.clear()
    assert build_candidate_key_lookup(cached, now) == from_cache
    newest = cached.get_max_index(now)
    for ind in range(newest, newest - 36 * 4, -1):
        _ = cached.keys_at(ind)
    assert derivations == []


@pytest.mark.asyncio
async def test_storage_creates_cached_accessories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cached, _ = make_pair()
    data = cached.to_json()
    hass = HomeAssistant(str(tmp_path))
    frame.async_setup(hass)
    monkeypatch.setattr(frame, "report_usage", lambda *_args, **_kwargs: None)
    try:
        storage = RuntimeStorage(hass)
        entry = MagicMock(entry_id="tag", data={"type": "device_rolling_derived", "data": data})

        item = await storage.add_entry(entry)

        assert isinstance(item, CachedFindMyAccessory)
        # Nothing of the cache leaks into what is written back to the config entry.
        assert item.to_json() == data
        assert item == cached
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_realignment_refresh_is_debounced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replayed advertisements must not rebuild the candidates more than once per minute."""
    builds: list[datetime] = []

    def build(_accessory: object, observed_at: datetime) -> dict[bytes, tuple[object, ...]]:
        builds.append(observed_at)
        return {}

    cached, _ = make_pair()
    hass = HomeAssistant(str(tmp_path))
    frame.async_setup(hass)
    # Defining the tracker class makes Home Assistant look up the integration, to word a
    # deprecation warning about its `battery_level` property. The lookup needs the loader's data.
    loader.async_setup(hass)
    monkeypatch.setattr(frame, "report_usage", lambda *_args, **_kwargs: None)
    try:
        # The device tracker imports the Bluetooth integration, which is not importable on
        # Windows.
        device_tracker = pytest.importorskip(
            "custom_components.findmy.device_tracker",
            reason="needs homeassistant.components.bluetooth",
            exc_type=ImportError,
        )
        monkeypatch.setattr(device_tracker, "build_candidate_key_lookup", build)
        monkeypatch.setattr(
            device_tracker.bluetooth, "async_discovered_service_info", lambda *_a, **_kw: []
        )
        monkeypatch.setattr(device_tracker, "_LOCAL_REALIGNMENT_REFRESH_COOLDOWN", 0.2)
        storage = RuntimeStorage(hass)
        tracker = device_tracker.FindMyDeviceTracker(storage.coordinator, cached, "entry")
        tracker.hass = hass
        tracker.async_write_ha_state = lambda: None
        tracker._local_active = True

        for _ in range(20):
            tracker._realignment_refresh.async_schedule_call()
            await asyncio.sleep(0)
        await hass.async_block_till_done(wait_background_tasks=True)

        # The first request refreshes at once; the other 19 wait for the cooldown.
        assert len(builds) == 1

        await asyncio.sleep(0.3)
        await hass.async_block_till_done(wait_background_tasks=True)

        # They collapse into a single refresh, so the last realignment is not lost.
        assert len(builds) == 2

        tracker._realignment_refresh.async_cancel()
    finally:
        await hass.async_stop(force=True)
