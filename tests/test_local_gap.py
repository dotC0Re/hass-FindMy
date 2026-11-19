"""An accessory whose index fell far behind the clock is still recognised locally.

Every fetch and every local refresh only looks at the top week of key indices. A tag whose
battery was empty for longer than that continues below this window once it has power again.
With an Apple account a location report realigns it; the local Bluetooth matching has to find
it without one, from the keys above its last alignment (``local_gap_keys``).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from findmy import FindMyAccessory, KeyPairType

from custom_components.findmy import key_cache
from custom_components.findmy.key_cache import (
    LOCAL_GAP_STEP,
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
    import pytest
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


def test_tag_with_a_new_battery_after_ten_days_is_heard_locally() -> None:
    """The reported case: last heard, battery empty for ten days, index continues from there."""
    now = datetime.now(tz=UTC)
    cached, plain = make_pair(aligned_days_ago=10.5)
    alignment_index = cached.alignment[1]
    # It ran for a few more hours after it was last heard, then stood still.
    resumed_index = alignment_index + 20
    assert resumed_index < cached.get_min_index(now - KEY_WINDOW_PAST)
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
    assert cached.local_gap_keys(cached.get_min_index(now - KEY_WINDOW_PAST)) == []
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

    assert cached.local_gap_keys(cached.get_min_index(now - KEY_WINDOW_PAST)) == []

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
    covered_from = cached.get_min_index(now - KEY_WINDOW_PAST)
    gap = covered_from - alignment_index
    assert gap > 300  # more than three steps, so the steps are visible

    first = {index for index, _key, primary in cached.local_gap_keys(covered_from) if primary}
    second = {index for index, _key, primary in cached.local_gap_keys(covered_from) if primary}

    # The week right above the alignment comes first: that is where a revived tag is.
    assert first == set(range(alignment_index, alignment_index + 100))
    assert second == set(range(alignment_index, alignment_index + 200))

    complete = cached.local_gap_keys(covered_from)
    for _ in range(gap // 100):
        complete = cached.local_gap_keys(covered_from)
    assert {index for index, _key, primary in complete if primary} == set(
        range(alignment_index, covered_from)
    )

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
    covered_from = cached.get_min_index(now - KEY_WINDOW_PAST)
    assert covered_from - alignment_index > MAX_LOCAL_GAP_INDICES

    keys = cached.local_gap_keys(covered_from)
    primaries = {index for index, _key, primary in keys if primary}

    assert len(primaries) == LOCAL_GAP_STEP
    assert max(primaries) < alignment_index + MAX_LOCAL_GAP_INDICES
    assert len(cached._gap_primary) <= MAX_LOCAL_GAP_INDICES
    # A whole day shares its two secondary keys.
    assert len([key for key in keys if not key[2]]) <= 2 * (LOCAL_GAP_STEP // DAY + 1)


def test_gap_keys_are_dropped_when_the_alignment_moves() -> None:
    now = datetime.now(tz=UTC)
    cached, _plain = make_pair(aligned_days_ago=12)
    covered_from = cached.get_min_index(now - KEY_WINDOW_PAST)
    assert cached.local_gap_keys(covered_from)

    # A location report found it two days below the clock.
    cached.update_alignment(now - timedelta(minutes=5), cached.get_max_index(now) - 2 * DAY)

    assert cached.local_gap_keys(cached.get_min_index(now - KEY_WINDOW_PAST)) == []
    assert cached._gap_primary == {}
    assert cached._gap_secondary == {}
