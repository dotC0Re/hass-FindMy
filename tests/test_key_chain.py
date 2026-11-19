"""The key chains of rolling-key accessories: checkpoints, and how derivation is paced (M4, G6).

After a restart FindMy.py walks the secret chain of an accessory from its pairing, 96 steps per
day. Checkpoints let a new object start close to where the keys are needed. They rely on private
attributes of FindMy.py's key generator, so the tests also break those attributes and check that
everything falls back to the slow path with the same keys. Key derivation pauses regularly and
never runs in two threads at once, so that the event loop keeps getting the interpreter lock.
Keys are random or fixed test values; nothing here talks to Apple.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import findmy.util.crypto
import pytest
from findmy import FindMyAccessory
from findmy.accessory import _AccessoryKeyGenerator

from custom_components.findmy import key_cache
from custom_components.findmy.key_cache import (
    PRIMARY_CHAIN,
    SECONDARY_CHAIN,
    CachedFindMyAccessory,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from findmy import FindMyAccessoryMapping

PAIRED_DAYS = 400
# Chain steps a restart from checkpoints may take: up to a week from the nearest checkpoint, and
# a few steps per key of the window, since FindMy.py keeps every fourth secret of the chain.
MAX_STEPS_FROM_CHECKPOINTS = 4 * 7 * 96


def accessory_data(*, paired_days: int = PAIRED_DAYS, aligned_hours_ago: float = 1.0) -> Any:
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
    return data


def cached(data: FindMyAccessoryMapping) -> CachedFindMyAccessory:
    accessory = CachedFindMyAccessory.from_json(data)
    assert isinstance(accessory, CachedFindMyAccessory)
    return accessory


def window_keys(accessory: CachedFindMyAccessory, now: datetime) -> dict[int, set[bytes]]:
    window = accessory.search_window(now)
    return {
        ind: {key.private_key_bytes for key in window.keys_at(ind)}
        for ind in range(window.lowest, window.highest + 1)
    }


def library_keys(data: FindMyAccessoryMapping, indices: range) -> dict[int, set[bytes]]:
    plain = FindMyAccessory.from_json(data)
    return {ind: {key.private_key_bytes for key in plain.keys_at(ind)} for ind in indices}


@pytest.fixture
def chain_steps(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Counts the steps of secret chains, one key derivation function call each."""
    steps: list[None] = []
    original = findmy.util.crypto.x963_kdf

    def counting(value: bytes, si: bytes, length: int) -> bytes:
        if si == b"update":
            steps.append(None)
        return original(value, si, length)

    monkeypatch.setattr(findmy.util.crypto, "x963_kdf", counting)
    return steps


@pytest.fixture
def broken_library(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A FindMy.py whose accessories keep their key generators under other names."""
    key_cache._chain_access_works.cache_clear()
    monkeypatch.setattr(
        key_cache,
        "_GENERATOR_ATTRIBUTES",
        {PRIMARY_CHAIN: "_primary_generator", SECONDARY_CHAIN: "_secondary_generator"},
    )
    yield
    key_cache._chain_access_works.cache_clear()


# --- Checkpoints ----------------------------------------------------------------------------


def test_restart_from_checkpoints_walks_a_fraction_of_the_chain(chain_steps: list[None]) -> None:
    data = accessory_data()
    now = datetime.now(tz=UTC)
    before = cached(data)
    _ = before.search_window(now)
    cold = len(chain_steps)
    checkpoints = before.chain_checkpoints()

    chain_steps.clear()
    after = cached(data)
    assert after.restore_chain_checkpoints(checkpoints) is True
    keys = window_keys(after, now)

    assert cold > PAIRED_DAYS * 96
    assert len(chain_steps) < MAX_STEPS_FROM_CHECKPOINTS
    assert keys == library_keys(data, range(min(keys), max(keys) + 1))


def test_checkpoints_hold_chain_secrets_only_and_few_of_them() -> None:
    data = accessory_data()
    accessory = cached(data)
    _ = accessory.search_window(datetime.now(tz=UTC))

    checkpoints = accessory.chain_checkpoints()

    assert set(checkpoints) == {PRIMARY_CHAIN, SECONDARY_CHAIN}
    primary = checkpoints[PRIMARY_CHAIN]
    assert 1 <= len(primary) <= 3
    # Nothing lower than a week below the alignment: what a fetch or the matching can need.
    assert min(primary) >= data["alignment_index"] - 2 * 7 * 96
    secrets = [secret for chain in checkpoints.values() for secret in chain.values()]
    assert all(len(secret) == 32 for secret in secrets)
    # No private key of the accessory.
    plain = FindMyAccessory.from_json(data)
    window = accessory.search_window(datetime.now(tz=UTC))
    private = {
        key.private_key_bytes
        for ind in range(window.lowest, window.highest + 1)
        for key in plain.keys_at(ind)
    }
    assert not private & set(secrets)
    assert bytes.fromhex(data["master_key"]) not in secrets


def test_accessory_never_located_keeps_a_checkpoint_per_week() -> None:
    """Its catch-up sweeps the whole range; each window starts from a checkpoint below it."""
    now = datetime.now(tz=UTC)
    data = accessory_data(paired_days=60)
    data["alignment_date"] = data["paired_at"]
    data["alignment_index"] = 0
    accessory = cached(data)
    _ = accessory.search_window(now)

    primary = accessory.chain_checkpoints()[PRIMARY_CHAIN]

    assert len(primary) >= 60 // 7
    assert all(ind % (7 * 96) == 0 for ind in primary)


def test_checkpoints_of_the_local_gap_are_kept_as_well(chain_steps: list[None]) -> None:
    """A tag ten days behind its alignment: the Bluetooth matching derives keys above it."""
    data = accessory_data(aligned_hours_ago=10 * 24)
    before = cached(data)
    covered_from = before.get_min_index(datetime.now(tz=UTC))
    gap = before.local_gap_keys(covered_from)
    assert gap

    after = cached(data)
    assert after.restore_chain_checkpoints(before.chain_checkpoints()) is True
    chain_steps.clear()

    assert after.local_gap_keys(covered_from) == gap
    assert len(chain_steps) < MAX_STEPS_FROM_CHECKPOINTS


def test_broken_checkpoints_are_ignored() -> None:
    data = accessory_data()
    accessory = cached(data)

    taken = accessory.restore_chain_checkpoints(
        {PRIMARY_CHAIN: {0: bytes(32), 8: b"short"}, "unknown": {4: bytes(32)}}
    )

    assert taken is False
    keys = window_keys(accessory, datetime.now(tz=UTC))
    assert keys == library_keys(data, range(min(keys), max(keys) + 1))


def test_changed_library_falls_back_to_the_slow_path(
    broken_library: None, chain_steps: list[None]
) -> None:
    data = accessory_data()
    now = datetime.now(tz=UTC)

    assert key_cache._chain_access_works() is False
    before = cached(data)
    _ = before.search_window(now)
    assert before.chain_checkpoints() == {}

    after = cached(data)
    assert after.restore_chain_checkpoints({PRIMARY_CHAIN: {8: os.urandom(32)}}) is False
    chain_steps.clear()
    keys = window_keys(after, now)

    assert len(chain_steps) > PAIRED_DAYS * 96
    assert keys == library_keys(data, range(min(keys), max(keys) + 1))


def test_library_that_keeps_other_secrets_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attributes of the same name that mean something else must not produce wrong keys."""
    original = _AccessoryKeyGenerator._update_caches

    def different(self: Any, ind: int, sk: bytes) -> None:
        original(self, ind, sk[::-1])

    key_cache._chain_access_works.cache_clear()
    monkeypatch.setattr(_AccessoryKeyGenerator, "_update_caches", different)
    try:
        assert key_cache._chain_access_works() is False
    finally:
        key_cache._chain_access_works.cache_clear()


def test_library_that_ignores_seeded_secrets_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    key_cache._chain_access_works.cache_clear()
    monkeypatch.setattr(
        _AccessoryKeyGenerator, "_find_best_cached_sk", lambda self, _ind: (0, self._initial_sk)
    )
    try:
        assert key_cache._chain_access_works() is False
    finally:
        key_cache._chain_access_works.cache_clear()


# --- Sharing the interpreter ----------------------------------------------------------------


def test_accessories_never_derive_in_parallel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Executor threads take turns: one thread's pause is no other thread's working time."""
    running = 0
    most = 0
    guard = threading.Lock()
    original = _AccessoryKeyGenerator._get_sk

    def tracked(self: Any, ind: int) -> bytes:
        nonlocal running, most
        with guard:
            running += 1
            most = max(most, running)
        try:
            time.sleep(0)  # give another thread every chance to come in
            return original(self, ind)
        finally:
            with guard:
                running -= 1

    monkeypatch.setattr(_AccessoryKeyGenerator, "_get_sk", tracked)
    accessories = [cached(accessory_data(paired_days=60)) for _ in range(4)]
    now = datetime.now(tz=UTC)

    with ThreadPoolExecutor(max_workers=4) as pool:
        windows = list(pool.map(lambda accessory: accessory.search_window(now), accessories))

    assert len(windows) == 4
    assert most == 1


def test_derivation_pauses_regularly_in_a_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    pauses: list[float] = []
    monkeypatch.setattr(key_cache, "_sleep", pauses.append)
    accessory = cached(accessory_data())

    started = time.perf_counter()
    thread = threading.Thread(target=accessory.search_window, args=(datetime.now(tz=UTC),))
    thread.start()
    thread.join()
    busy = time.perf_counter() - started

    assert pauses
    assert set(pauses) == {key_cache._PAUSE}
    # About one pause per slice of work; the chain walk and the keys both count.
    assert len(pauses) >= busy / (4 * key_cache._WORK_SLICE)


async def test_derivation_never_sleeps_on_an_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    pauses: list[float] = []
    monkeypatch.setattr(key_cache, "_sleep", pauses.append)

    _ = cached(accessory_data(paired_days=30)).search_window(datetime.now(tz=UTC))

    assert pauses == []
