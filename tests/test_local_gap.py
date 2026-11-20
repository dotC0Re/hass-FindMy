"""An accessory whose index fell far behind the clock is still recognised locally.

Every fetch and every local refresh only looks at the top week of key indices. A tag whose
battery was empty for longer than that continues below this window once it has power again.
With an Apple account a location report realigns it; the local Bluetooth matching has to find
it without one, from the keys above its last alignment (``local_gap_keys``). A tag may also
start again a little below the index it was last heard at; a day of keys below the alignment
covers that. A tag that only sends its secondary key cannot align, so its matches raise the
lower end of that range instead (``note_unaligned_match``).
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from findmy import FindMyAccessory, KeyPairType

from custom_components.findmy import key_cache
from custom_components.findmy.key_cache import (
    LOCAL_GAP_STEP,
    LOCAL_RESTART_MARGIN,
    MAX_LOCAL_GAP_INDICES,
    CachedFindMyAccessory,
)
from custom_components.findmy.local_bluetooth import (
    KEY_WINDOW_PAST,
    build_candidate_key_lookup,
    match_dult_advertisement,
    match_local_advertisement,
)

if TYPE_CHECKING:
    from findmy import FindMyAccessoryMapping

DAY = 96
STATUS = 0b0100_0101


def make_pair(
    *, paired_days: int = 60, aligned_days_ago: float
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
        "alignment_date": (now - timedelta(days=aligned_days_ago)).isoformat(),
        "alignment_index": int((paired_days - aligned_days_ago) * DAY),
    }
    cached = CachedFindMyAccessory.from_json(data)
    assert isinstance(cached, CachedFindMyAccessory)
    return cached, FindMyAccessory.from_json(data)


def primary_key(plain: FindMyAccessory, index: int) -> bytes:
    return next(
        key.adv_key_bytes for key in plain.keys_at(index) if key.key_type == KeyPairType.PRIMARY
    )


def nearby_adv(adv_key: bytes) -> bytes:
    """Apple manufacturer data of a nearby accessory: status and the top two key bits."""
    return bytes([0x12, 0x02, STATUS, adv_key[0] >> 6])


def mac_of(adv_key: bytes) -> str:
    """The random static address an accessory advertises a key under."""
    return ":".join(f"{byte:02X}" for byte in bytes([adv_key[0] | 0xC0]) + adv_key[1:6])


def covered(cached: CachedFindMyAccessory, now: datetime) -> int:
    """The lowest index build_candidate_key_lookup covers with its regular window."""
    return cached.get_min_index(now - KEY_WINDOW_PAST)


def aligning(keys: list[key_cache.LocalGapKey]) -> set[int]:
    return {key.index for key in keys if key.can_align}


@pytest.fixture
def derived(monkeypatch: pytest.MonkeyPatch) -> list[tuple[KeyPairType, int]]:
    """Every primary and secondary key derivation of a cached accessory, by type and index."""
    calls: list[tuple[KeyPairType, int]] = []
    primary = CachedFindMyAccessory._primary_key_at
    secondary = CachedFindMyAccessory._secondary_keys_at

    def counting_primary(self: CachedFindMyAccessory, ind: int) -> object:
        calls.append((KeyPairType.PRIMARY, ind))
        return primary(self, ind)

    def counting_secondary(self: CachedFindMyAccessory, ind: int) -> object:
        calls.append((KeyPairType.SECONDARY, ind))
        return secondary(self, ind)

    monkeypatch.setattr(CachedFindMyAccessory, "_primary_key_at", counting_primary)
    monkeypatch.setattr(CachedFindMyAccessory, "_secondary_keys_at", counting_secondary)
    return calls


def test_tag_with_a_new_battery_after_ten_days_is_heard_locally() -> None:
    """The reported case: last heard, battery empty for ten days, index continues from there."""
    now = datetime.now(tz=UTC)
    cached, plain = make_pair(aligned_days_ago=10.5)
    alignment_index = cached.alignment[1]
    # It ran for a few more hours after it was last heard, then stood still.
    resumed_index = alignment_index + 20
    assert resumed_index < covered(cached, now)
    adv_key = primary_key(plain, resumed_index)

    candidates = build_candidate_key_lookup(cached, now)
    nearby = match_local_advertisement(mac_of(adv_key), nearby_adv(adv_key), now, -60, candidates)
    dult = match_dult_advertisement(mac_of(adv_key), bytes([0x01, 0x01]), now, -60, candidates)

    assert nearby is not None
    assert nearby.key_index == resumed_index
    assert nearby.can_align is True
    # Second-generation AirTags near their owner only send the DULT payload.
    assert dult is not None
    assert dult.key_index == resumed_index


def test_match_in_the_gap_realigns_and_closes_the_gap() -> None:
    now = datetime.now(tz=UTC)
    cached, plain = make_pair(aligned_days_ago=10.5)
    resumed_index = cached.alignment[1] + 20
    adv_key = primary_key(plain, resumed_index)
    candidates = build_candidate_key_lookup(cached, now)
    observation = match_local_advertisement(
        mac_of(adv_key), nearby_adv(adv_key), now, -60, candidates
    )
    assert observation is not None

    cached.update_alignment(observation.detected_at, observation.key_index)

    assert cached.alignment == (now, resumed_index)
    # The top window is where the accessory is now; nothing is left below it.
    assert cached.local_gap_keys(covered(cached, now)) == []
    # And the key it advertises 15 minutes later is among the regular candidates.
    later = build_candidate_key_lookup(cached, now + timedelta(minutes=15))
    next_key = primary_key(plain, resumed_index + 1)
    assert (
        match_local_advertisement(mac_of(next_key), nearby_adv(next_key), now, -60, later)
        is not None
    )


def test_secondary_keys_of_the_gap_match_but_cannot_align() -> None:
    now = datetime.now(tz=UTC)
    cached, plain = make_pair(aligned_days_ago=10.5)
    index = cached.alignment[1] + 20
    secondary = next(key for key in plain.keys_at(index) if key.key_type == KeyPairType.SECONDARY)

    candidates = build_candidate_key_lookup(cached, now)
    observation = match_local_advertisement(
        mac_of(secondary.adv_key_bytes),
        secondary.of_data(status=STATUS, hint=0x42),
        now,
        -60,
        candidates,
    )

    assert observation is not None
    assert observation.state == "separated"
    assert observation.can_align is False


def test_accessory_that_kept_pace_has_no_gap_and_costs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(tz=UTC)
    cached, _plain = make_pair(aligned_days_ago=0.1)
    derived: list[int] = []
    original = CachedFindMyAccessory._primary_key_at

    def counting(self: CachedFindMyAccessory, ind: int) -> object:
        derived.append(ind)
        return original(self, ind)

    assert cached.local_gap_keys(covered(cached, now)) == []

    regular = build_candidate_key_lookup(cached, now)
    monkeypatch.setattr(CachedFindMyAccessory, "_primary_key_at", counting)
    again = build_candidate_key_lookup(cached, now)

    assert derived == []
    assert again == regular


def test_gap_is_derived_once_and_in_steps_from_the_alignment_upwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(tz=UTC)
    monkeypatch.setattr(key_cache, "LOCAL_GAP_STEP", 100)
    cached, _plain = make_pair(aligned_days_ago=12)
    alignment_index = cached.alignment[1]
    covered_from = covered(cached, now)
    gap = covered_from - alignment_index
    assert gap > 300  # more than three steps, so the steps are visible

    first = aligning(cached.local_gap_keys(covered_from))
    second = aligning(cached.local_gap_keys(covered_from))

    # The week right above the alignment comes first: that is where a revived tag is. The day
    # below it comes on top of the step, in the first call.
    assert first == set(range(alignment_index, alignment_index + 100))
    assert second == set(range(alignment_index, alignment_index + 200))
    assert set(cached._gap_primary) == set(range(alignment_index - DAY, alignment_index + 200))

    complete = cached.local_gap_keys(covered_from)
    for _ in range(gap // 100):
        complete = cached.local_gap_keys(covered_from)
    assert aligning(complete) == set(range(alignment_index, covered_from))

    derived: list[int] = []
    original = CachedFindMyAccessory._primary_key_at

    def counting(self: CachedFindMyAccessory, ind: int) -> object:
        derived.append(ind)
        return original(self, ind)

    monkeypatch.setattr(CachedFindMyAccessory, "_primary_key_at", counting)
    assert cached.local_gap_keys(covered_from) == complete
    assert derived == []


def test_gap_is_bounded_for_an_accessory_that_was_never_heard_again() -> None:
    """Years without a sighting must not mean years of keys in memory."""
    now = datetime.now(tz=UTC)
    cached, _plain = make_pair(paired_days=900, aligned_days_ago=700)
    alignment_index = cached.alignment[1]
    covered_from = covered(cached, now)
    assert covered_from - alignment_index > MAX_LOCAL_GAP_INDICES

    keys = cached.local_gap_keys(covered_from)
    primaries = aligning(keys)

    assert len(primaries) == LOCAL_GAP_STEP
    assert max(primaries) < alignment_index + MAX_LOCAL_GAP_INDICES
    assert len(cached._gap_primary) <= MAX_LOCAL_GAP_INDICES + LOCAL_RESTART_MARGIN
    # A whole day shares its two secondary keys.
    assert len(cached._gap_secondary) <= (LOCAL_GAP_STEP + LOCAL_RESTART_MARGIN) // DAY + 2


def test_gap_keys_are_dropped_when_the_alignment_moves() -> None:
    now = datetime.now(tz=UTC)
    cached, _plain = make_pair(aligned_days_ago=12)
    assert cached.local_gap_keys(covered(cached, now))

    # A location report found it two days below the clock.
    cached.update_alignment(now - timedelta(minutes=5), cached.get_max_index(now) - 2 * DAY)

    assert cached.local_gap_keys(covered(cached, now)) == []
    assert cached._gap_primary == {}
    assert cached._gap_secondary == {}


# --- A day below the last sighting ----------------------------------------------------------


@pytest.mark.parametrize("below", [1, 2, DAY])
def test_tag_that_starts_again_below_an_old_alignment_is_heard_but_cannot_align(
    below: int,
) -> None:
    """A tag that kept its index in flash only now and then, after a change of battery."""
    now = datetime.now(tz=UTC)
    cached, plain = make_pair(aligned_days_ago=10.5)
    alignment_index = cached.alignment[1]
    adv_key = primary_key(plain, alignment_index - below)
    candidates = build_candidate_key_lookup(cached, now)

    nearby = match_local_advertisement(mac_of(adv_key), nearby_adv(adv_key), now, -60, candidates)
    dult = match_dult_advertisement(mac_of(adv_key), bytes([0x01, 0x01]), now, -60, candidates)

    assert nearby is not None
    assert nearby.key_index == alignment_index - below
    # It must neither move the alignment backwards nor look like a new index to the tracker.
    assert nearby.can_align is False
    assert dult is not None
    assert dult.can_align is False


def test_nothing_below_the_day_below_the_alignment_is_a_candidate() -> None:
    now = datetime.now(tz=UTC)
    cached, plain = make_pair(aligned_days_ago=10.5)
    adv_key = primary_key(plain, cached.alignment[1] - DAY - 1)
    candidates = build_candidate_key_lookup(cached, now)

    assert (
        match_local_advertisement(mac_of(adv_key), nearby_adv(adv_key), now, -60, candidates)
        is None
    )


@pytest.mark.parametrize("aligned_hours_ago", [0.25, 2.4, 11.5])
def test_accessory_heard_within_twelve_hours_derives_nothing_extra(
    derived: list[tuple[KeyPairType, int]], aligned_hours_ago: float
) -> None:
    """The 36-hour window reaches a day below the alignment: nothing is left to derive.

    An accessory that is heard regularly gets a new alignment with every match. Anything paid
    once per alignment would be paid all the time, so each new alignment is checked as well.
    """
    now = datetime.now(tz=UTC)
    cached, _plain = make_pair(aligned_days_ago=aligned_hours_ago / 24)
    date, index = cached.alignment

    for step in range(5):
        if step:
            # Heard again, an index further.
            date, index = date + timedelta(minutes=1), index + 1
            cached.update_alignment(date, index)
        assert covered(cached, now) <= index - LOCAL_RESTART_MARGIN
        assert cached.local_gap_keys(covered(cached, now)) == []

    assert derived == []
    assert cached._gap_primary == {}


def test_day_below_a_sighting_older_than_twelve_hours_is_derived_once(
    derived: list[tuple[KeyPairType, int]],
) -> None:
    """Not heard for 20 hours: no gap above, and the window covers part of the day below."""
    now = datetime.now(tz=UTC)
    cached, _plain = make_pair(aligned_days_ago=20 / 24)
    alignment_index = cached.alignment[1]
    covered_from = covered(cached, now)
    assert alignment_index - DAY < covered_from <= alignment_index

    keys = cached.local_gap_keys(covered_from)
    missing = set(range(alignment_index - DAY, covered_from))

    assert {key.index for key in keys} == missing
    assert not any(key.can_align for key in keys)
    primaries = sorted(ind for kind, ind in derived if kind == KeyPairType.PRIMARY)
    assert primaries == sorted(missing)
    # Plus the secondary keys of at most the two days these indices fall in.
    assert len(derived) - len(primaries) <= 2

    derived.clear()
    assert cached.local_gap_keys(covered_from) == keys
    assert derived == []


# --- A bound raised by matches that cannot align --------------------------------------------


def test_unaligned_match_raises_the_bound_and_drops_lower_keys(
    derived: list[tuple[KeyPairType, int]],
) -> None:
    """A tag far behind its alignment that only sends its secondary key, about once a day."""
    now = datetime.now(tz=UTC)
    cached, plain = make_pair(aligned_days_ago=12)
    alignment = cached.alignment
    stored = cached.to_json()
    covered_from = covered(cached, now)
    tag_index = alignment[1] + 3 * DAY + 10
    assert tag_index < covered_from
    before = build_candidate_key_lookup(cached, now)
    assert min(cached._gap_primary) == alignment[1] - DAY

    # The second of the two secondary keys of its day; the first is shared with the day before.
    secondary = plain._secondary_keys_at(tag_index)[1]
    observation = match_local_advertisement(
        mac_of(secondary.adv_key_bytes),
        secondary.of_data(status=STATUS, hint=0x42),
        now,
        -60,
        before,
    )
    assert observation is not None
    assert observation.can_align is False
    bound = tag_index // DAY * DAY
    assert observation.key_index // DAY * DAY == bound

    derived.clear()
    cached.note_unaligned_match(observation.key_index)
    after = build_candidate_key_lookup(cached, now)

    # Only a day below the bound is left of everything below it, and nothing new was derived:
    # the day below the bound lay in the gap already.
    assert min(cached._gap_primary) == bound - LOCAL_RESTART_MARGIN
    assert min(cached._gap_secondary) == (bound - LOCAL_RESTART_MARGIN) // DAY
    assert sum(map(len, after.values())) < sum(map(len, before.values()))
    assert derived == []
    # The alignment and what is stored are untouched.
    assert cached.alignment == alignment
    assert cached.to_json() == stored

    # The tag's own primary key is still a candidate and realigns it, and so is one in the day
    # below the bound, which may be the tag's day (see note_unaligned_match).
    for index in (tag_index, bound - 1):
        adv_key = primary_key(plain, index)
        match = match_local_advertisement(mac_of(adv_key), nearby_adv(adv_key), now, -60, after)
        assert match is not None
        assert match.key_index == index
        assert match.can_align is True


def test_raised_bound_keeps_its_reach_and_survives_further_calls(
    monkeypatch: pytest.MonkeyPatch, derived: list[tuple[KeyPairType, int]]
) -> None:
    now = datetime.now(tz=UTC)
    # A short reach and a long step, so that one call derives everything the test looks at.
    monkeypatch.setattr(key_cache, "MAX_LOCAL_GAP_INDICES", 3 * DAY)
    monkeypatch.setattr(key_cache, "LOCAL_GAP_STEP", 10 * DAY)
    cached, _plain = make_pair(paired_days=900, aligned_days_ago=700)
    anchor = cached.alignment[1]
    covered_from = covered(cached, now)
    _ = cached.local_gap_keys(covered_from)
    assert set(cached._gap_primary) == set(range(anchor - DAY, anchor + 3 * DAY))

    cached.note_unaligned_match(anchor + 2 * DAY + 5)
    bound = (anchor + 2 * DAY + 5) // DAY * DAY
    keys = cached.local_gap_keys(covered_from)

    # 90 days, here three, from the new bound: beyond the old reach.
    expected = set(range(bound - DAY, bound + 3 * DAY))
    assert set(cached._gap_primary) == expected
    assert aligning(keys) == expected

    # Further calls keep it and derive nothing; a lower day does not take it back.
    cached.note_unaligned_match(anchor + DAY)
    derived.clear()
    for _ in range(3):
        assert cached.local_gap_keys(covered_from) == keys
    assert derived == []


def test_raised_bound_is_forgotten_when_the_alignment_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(tz=UTC)
    monkeypatch.setattr(key_cache, "MAX_LOCAL_GAP_INDICES", 3 * DAY)
    monkeypatch.setattr(key_cache, "LOCAL_GAP_STEP", 10 * DAY)
    cached, _plain = make_pair(paired_days=900, aligned_days_ago=700)
    date, anchor = cached.alignment
    covered_from = covered(cached, now)
    cached.note_unaligned_match(anchor + 2 * DAY)
    _ = cached.local_gap_keys(covered_from)
    assert min(cached._gap_primary) == anchor + DAY

    # Heard again at the alignment itself, later: the bound above it is no longer true.
    cached.update_alignment(date + timedelta(minutes=1), anchor)
    _ = cached.local_gap_keys(covered_from)
    assert set(cached._gap_primary) == set(range(anchor - DAY, anchor + 3 * DAY))

    cached.note_unaligned_match(anchor + 2 * DAY)
    cached.update_alignment(date + timedelta(minutes=2), anchor + 1)
    _ = cached.local_gap_keys(covered_from)
    assert set(cached._gap_primary) == set(range(anchor + 1 - DAY, anchor + 1 + 3 * DAY))


def test_bound_never_falls_and_never_touches_the_alignment() -> None:
    cached, _plain = make_pair(aligned_days_ago=12)
    alignment = cached.alignment
    stored = cached.to_json()
    anchor = alignment[1]

    # Below the alignment, and anywhere in its own day: nothing is recorded.
    for index in (anchor - 1, anchor - DAY, anchor, anchor // DAY * DAY + DAY - 1):
        cached.note_unaligned_match(index)
    assert cached._gap_floor_request is None

    cached.note_unaligned_match(anchor + 3 * DAY)
    cached.note_unaligned_match(anchor + DAY)
    assert cached._gap_floor_request == (alignment, (anchor + 3 * DAY) // DAY * DAY)
    assert cached.alignment == alignment
    assert cached.to_json() == stored


def test_unaligned_match_does_not_wait_for_a_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The event loop must not wait for a local refresh that derives keys in another thread."""
    now = datetime.now(tz=UTC)
    cached, _plain = make_pair(aligned_days_ago=12)
    anchor = cached.alignment[1]
    covered_from = covered(cached, now)
    entered = threading.Event()
    release = threading.Event()
    original = CachedFindMyAccessory._primary_key_at

    def blocking(self: CachedFindMyAccessory, ind: int) -> object:
        if not entered.is_set():
            entered.set()
            assert release.wait(timeout=30)
        return original(self, ind)

    monkeypatch.setattr(CachedFindMyAccessory, "_primary_key_at", blocking)
    refresh = threading.Thread(target=cached.local_gap_keys, args=(covered_from,))
    refresh.start()
    try:
        assert entered.wait(timeout=30)
        assert cached._gap_lock.locked()

        noted = threading.Thread(target=cached.note_unaligned_match, args=(anchor + 2 * DAY,))
        noted.start()
        noted.join(timeout=5)
        assert not noted.is_alive()
        assert cached._gap_lock.locked()
    finally:
        release.set()
        refresh.join(timeout=60)
    assert not refresh.is_alive()

    # The refresh that was running kept its range; the next one applies the bound.
    assert min(cached._gap_primary) == anchor - DAY
    _ = cached.local_gap_keys(covered_from)
    assert min(cached._gap_primary) == (anchor + 2 * DAY) // DAY * DAY - DAY
