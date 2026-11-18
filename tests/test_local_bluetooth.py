"""Tests for matching local Find My advertisements against generated rolling keys.

Advertisements are built from real keys of randomly generated accessories, so these tests
exercise the same key derivation and byte layout as a real AirTag.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from findmy import FindMyAccessory, KeyPair, KeyPairType

from custom_components.findmy.local_bluetooth import (
    KEY_WINDOW_FUTURE,
    KEY_WINDOW_PAST,
    CandidateKey,
    _lookup_prefix,
    build_candidate_key_lookup,
    extract_offline_finding_payload,
    match_dult_advertisement,
    match_local_advertisement,
)

from .conftest import AccessoryFactory, KeyPairFactory

ALIGNMENT_INDEX = 500
INTERVAL = timedelta(minutes=15)
# Status byte with battery bits 0b01 ("medium") and a non-zero counter.
STATUS = 0b0100_0101


def _key_at(accessory: FindMyAccessory, index: int, key_type: KeyPairType) -> KeyPair:
    return next(key for key in accessory.keys_at(index) if key.key_type == key_type)


def _separated_adv(key: KeyPair, status: int = STATUS) -> bytes:
    """Apple manufacturer data (without company ID) of a separated accessory."""
    return key.of_data(status=status, hint=0x42)


def _nearby_adv(key: KeyPair, status: int = STATUS) -> bytes:
    """Apple manufacturer data of a nearby accessory: status and the top two key bits."""
    return bytes([0x12, 0x02, status, key.adv_key_bytes[0] >> 6])


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def accessory(accessory_factory: AccessoryFactory, now: datetime) -> FindMyAccessory:
    return accessory_factory(
        paired_at=now - timedelta(days=10),
        alignment_date=now,
        alignment_index=ALIGNMENT_INDEX,
    )


@pytest.fixture
def candidates(
    accessory: FindMyAccessory,
    now: datetime,
) -> dict[bytes, tuple[CandidateKey, ...]]:
    return build_candidate_key_lookup(accessory, now)


# --- _lookup_prefix -------------------------------------------------------------------------


def test_lookup_prefix_masks_top_two_bits_and_keeps_six_bytes() -> None:
    assert _lookup_prefix(bytes.fromhex("ff0102030405060708")) == bytes.fromhex("3f0102030405")


def test_lookup_prefix_maps_key_and_mac_address_to_same_value(
    keypair_factory: KeyPairFactory,
) -> None:
    key = keypair_factory()
    mac = bytes.fromhex(key.mac_address.replace(":", ""))

    # The MAC address forces the top two bits to 1, the key may have any value there.
    assert mac[0] & 0xC0 == 0xC0
    assert _lookup_prefix(mac) == _lookup_prefix(key.adv_key_bytes)


# --- extract_offline_finding_payload --------------------------------------------------------


def test_extract_returns_separated_section(keypair_factory: KeyPairFactory) -> None:
    data = _separated_adv(keypair_factory())

    assert extract_offline_finding_payload(data) == data


def test_extract_returns_nearby_section(keypair_factory: KeyPairFactory) -> None:
    data = _nearby_adv(keypair_factory())

    assert extract_offline_finding_payload(data) == data


def test_extract_skips_other_sections_and_trailing_bytes(keypair_factory: KeyPairFactory) -> None:
    section = _separated_adv(keypair_factory())
    other = bytes([0x10, 0x05, 0x01, 0x18, 0x00, 0x00, 0x00])

    assert extract_offline_finding_payload(other + section + b"\x00\x00") == section


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"\x12", id="type-only"),
        pytest.param(bytes([0x12, 0x05, 0, 0, 0, 0, 0]), id="unknown-length"),
        pytest.param(bytes([0x12, 0x19]) + bytes(10), id="truncated-separated"),
        pytest.param(bytes([0x12, 0x02, 0x00]), id="truncated-nearby"),
        pytest.param(bytes([0x10, 0x02, 0x00, 0x00]), id="other-type"),
    ],
)
def test_extract_rejects_incomplete_or_foreign_data(data: bytes) -> None:
    assert extract_offline_finding_payload(data) is None


# --- build_candidate_key_lookup -------------------------------------------------------------


def test_lookup_covers_key_window(
    accessory: FindMyAccessory,
    candidates: dict[bytes, tuple[CandidateKey, ...]],
) -> None:
    indices = {candidate.index for group in candidates.values() for candidate in group}

    first = ALIGNMENT_INDEX - KEY_WINDOW_PAST // INTERVAL
    last = ALIGNMENT_INDEX + KEY_WINDOW_FUTURE // INTERVAL
    assert min(indices) == first
    assert max(indices) == last

    primaries = {
        candidate.adv_key
        for group in candidates.values()
        for candidate in group
        if candidate.can_align
    }
    expected = {
        _key_at(accessory, index, KeyPairType.PRIMARY).adv_key_bytes
        for index in range(first, last + 1)
    }
    assert primaries == expected


def test_lookup_is_keyed_by_masked_prefix(
    candidates: dict[bytes, tuple[CandidateKey, ...]],
) -> None:
    for prefix, group in candidates.items():
        assert len(prefix) == 6
        assert prefix[0] & 0xC0 == 0
        for candidate in group:
            assert _lookup_prefix(candidate.adv_key) == prefix


def test_lookup_marks_only_primary_keys_as_alignable(
    accessory: FindMyAccessory,
    candidates: dict[bytes, tuple[CandidateKey, ...]],
) -> None:
    primary = _key_at(accessory, ALIGNMENT_INDEX, KeyPairType.PRIMARY)
    secondary = _key_at(accessory, ALIGNMENT_INDEX, KeyPairType.SECONDARY)

    by_key = {candidate.adv_key: candidate for group in candidates.values() for candidate in group}
    assert by_key[primary.adv_key_bytes].can_align
    assert by_key[primary.adv_key_bytes].index == ALIGNMENT_INDEX
    assert not by_key[secondary.adv_key_bytes].can_align


# --- match_local_advertisement --------------------------------------------------------------


def test_match_separated_primary_key(
    accessory: FindMyAccessory,
    candidates: dict[bytes, tuple[CandidateKey, ...]],
    now: datetime,
) -> None:
    key = _key_at(accessory, ALIGNMENT_INDEX + 3, KeyPairType.PRIMARY)

    observation = match_local_advertisement(
        key.mac_address.lower(), _separated_adv(key), now, -61, candidates
    )

    assert observation is not None
    assert observation.state == "separated"
    assert observation.key_index == ALIGNMENT_INDEX + 3
    assert observation.can_align
    assert observation.mac_address == key.mac_address.upper()
    assert observation.detected_at == now
    assert observation.rssi == -61
    assert observation.status == STATUS
    assert observation.battery_level == "Medium"


def test_match_separated_secondary_key_cannot_align(
    accessory: FindMyAccessory,
    candidates: dict[bytes, tuple[CandidateKey, ...]],
    now: datetime,
) -> None:
    key = _key_at(accessory, ALIGNMENT_INDEX, KeyPairType.SECONDARY)

    observation = match_local_advertisement(
        key.mac_address, _separated_adv(key), now, None, candidates
    )

    assert observation is not None
    assert observation.state == "separated"
    assert not observation.can_align


def test_match_nearby_key(
    accessory: FindMyAccessory,
    candidates: dict[bytes, tuple[CandidateKey, ...]],
    now: datetime,
) -> None:
    key = _key_at(accessory, ALIGNMENT_INDEX - 10, KeyPairType.PRIMARY)

    observation = match_local_advertisement(key.mac_address, _nearby_adv(key), now, -40, candidates)

    assert observation is not None
    assert observation.state == "nearby"
    assert observation.key_index == ALIGNMENT_INDEX - 10
    assert observation.can_align
    assert observation.status == STATUS


@pytest.mark.parametrize("build_adv", [_separated_adv, _nearby_adv], ids=["separated", "nearby"])
def test_no_match_for_foreign_accessory(
    accessory_factory: AccessoryFactory,
    candidates: dict[bytes, tuple[CandidateKey, ...]],
    now: datetime,
    build_adv: Callable[[KeyPair], bytes],
) -> None:
    other = accessory_factory(alignment_date=now, alignment_index=ALIGNMENT_INDEX)
    key = _key_at(other, ALIGNMENT_INDEX, KeyPairType.PRIMARY)

    assert match_local_advertisement(key.mac_address, build_adv(key), now, None, candidates) is None


def test_no_match_for_key_outside_window(
    accessory: FindMyAccessory,
    candidates: dict[bytes, tuple[CandidateKey, ...]],
    now: datetime,
) -> None:
    index = ALIGNMENT_INDEX + KEY_WINDOW_FUTURE // INTERVAL + 1
    key = _key_at(accessory, index, KeyPairType.PRIMARY)

    assert (
        match_local_advertisement(key.mac_address, _separated_adv(key), now, None, candidates)
        is None
    )


def test_separated_requires_full_key_but_nearby_only_prefix(
    keypair_factory: KeyPairFactory,
    now: datetime,
) -> None:
    key = keypair_factory()
    # Same six-byte prefix, different remainder: a nearby advertisement cannot tell them
    # apart, a separated one carries the whole key and must not match.
    lookalike = CandidateKey(index=7, adv_key=key.adv_key_bytes[:6] + bytes(22), can_align=True)
    lookup = {_lookup_prefix(key.adv_key_bytes): (lookalike,)}

    assert (
        match_local_advertisement(key.mac_address, _separated_adv(key), now, None, lookup) is None
    )
    nearby = match_local_advertisement(key.mac_address, _nearby_adv(key), now, None, lookup)
    assert nearby is not None
    assert nearby.key_index == 7


@pytest.mark.parametrize(
    ("address", "data"),
    [
        pytest.param("C0:01:02:03:04:05", b"", id="empty"),
        pytest.param("C0:01:02:03:04:05", b"\x07\x05\x00\x00\x00\x00\x00", id="no-of-section"),
        pytest.param("C0:01:02:03:04:05", bytes([0x12, 0x19]) + bytes(24), id="truncated"),
        pytest.param("not-a-mac", bytes([0x12, 0x02, 0x00, 0x00]), id="invalid-address"),
        pytest.param("C0:01", bytes([0x12, 0x02, 0x00, 0x00]), id="short-address"),
    ],
)
def test_no_match_for_broken_payload(
    address: str,
    data: bytes,
    candidates: dict[bytes, tuple[CandidateKey, ...]],
    now: datetime,
) -> None:
    assert match_local_advertisement(address, data, now, None, candidates) is None


@pytest.mark.xfail(
    raises=OverflowError,
    strict=True,
    reason="nearby key byte > 3 overflows in FindMy 0.10.2 and escapes the matcher",
)
def test_nearby_with_reserved_bits_set_does_not_raise(
    candidates: dict[bytes, tuple[CandidateKey, ...]],
    now: datetime,
) -> None:
    data = bytes([0x12, 0x02, 0x00, 0x07])

    assert match_local_advertisement("C0:01:02:03:04:05", data, now, None, candidates) is None


# --- match_dult_advertisement ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "state"),
    [(0x01, "near_owner"), (0x00, "separated_dult"), (0x03, "near_owner")],
)
def test_match_dult(
    accessory: FindMyAccessory,
    candidates: dict[bytes, tuple[CandidateKey, ...]],
    now: datetime,
    flags: int,
    state: str,
) -> None:
    key = _key_at(accessory, ALIGNMENT_INDEX + 1, KeyPairType.PRIMARY)

    observation = match_dult_advertisement(
        key.mac_address.lower(), bytes([0x01, flags, 0xAA]), now, -70, candidates
    )

    assert observation is not None
    assert observation.state == state
    assert observation.status == flags
    assert observation.mac_address == key.mac_address.upper()
    assert observation.key_index == ALIGNMENT_INDEX + 1
    assert observation.battery_level is None
    assert observation.rssi == -70


@pytest.mark.parametrize(
    ("address", "service_data"),
    [
        pytest.param(None, b"\x01", id="too-short"),
        pytest.param(None, b"\x02\x01", id="other-network"),
        pytest.param("zz:zz:zz:zz:zz:zz", b"\x01\x01", id="invalid-address"),
        pytest.param("C0:01:02:03:04", b"\x01\x01", id="short-address"),
        pytest.param("C0:01:02:03:04:05", b"\x01\x01", id="unknown-key"),
    ],
)
def test_no_dult_match(
    accessory: FindMyAccessory,
    candidates: dict[bytes, tuple[CandidateKey, ...]],
    now: datetime,
    address: str | None,
    service_data: bytes,
) -> None:
    if address is None:
        address = _key_at(accessory, ALIGNMENT_INDEX, KeyPairType.PRIMARY).mac_address

    assert match_dult_advertisement(address, service_data, now, None, candidates) is None
