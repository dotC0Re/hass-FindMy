"""Tests for the status byte helpers shared by the FindMy entities."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from custom_components.findmy._entity import (
    BATTERY_LABELS,
    BATTERY_PERCENTS,
    BATTERY_VOLTAGES_MV,
    battery_bits,
    battery_label,
    battery_percent,
    battery_voltage_mv,
    status_counter,
)


@pytest.mark.parametrize(
    ("status", "bits", "label", "percent", "voltage"),
    [
        (0b0000_0000, 0b00, "ok", 90, 3000),
        (0b0111_1111, 0b01, "medium", 65, 2800),
        (0b1000_0001, 0b10, "low", 40, 2600),
        (0b1100_0000, 0b11, "critical", 15, 2400),
    ],
)
def test_battery_helpers_read_top_two_bits(
    status: int,
    bits: int,
    label: str,
    percent: int,
    voltage: int,
) -> None:
    assert battery_bits(status) == bits
    assert battery_label(status) == label
    assert battery_percent(status) == percent
    assert battery_voltage_mv(status) == voltage


def test_battery_helpers_ignore_bits_beyond_the_status_byte() -> None:
    # Only bits 6-7 count, even if a caller passes a wider integer.
    assert battery_bits(0x1_40) == 0b01


@pytest.mark.parametrize(
    "helper",
    [battery_bits, battery_label, battery_percent, battery_voltage_mv, status_counter],
)
def test_helpers_pass_through_missing_status(helper: Callable[[int | None], object]) -> None:
    assert helper(None) is None


def test_every_battery_level_has_label_percent_and_voltage() -> None:
    assert set(BATTERY_LABELS) == set(BATTERY_PERCENTS) == set(BATTERY_VOLTAGES_MV) == set(range(4))


@pytest.mark.parametrize(
    ("status", "counter"),
    [
        (0b0000_0000, 0),
        (0b0000_0001, 1),
        (0b0011_1111, 63),
        (0b1100_0000, 0),
        (0b1110_1010, 0b10_1010),
    ],
)
def test_status_counter_reads_low_six_bits(status: int, counter: int) -> None:
    assert status_counter(status) == counter
