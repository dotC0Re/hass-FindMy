"""Tests for the persisted per-account fetch budget.

The decision logic gets its times passed in, so nothing here waits. Persistence runs against a
real ``Store`` in the temporary config directory of the ``hass`` fixture; a restart is two
budgets on the same ``hass``, with the shutdown write triggered by its event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from custom_components.findmy.budget import (
    MIN_FETCH_INTERVAL,
    RETENTION,
    SAVE_DELAY,
    STORAGE_KEY,
    STORAGE_VERSION,
    FetchBudget,
    PersistentFetchBudget,
    account_budget_id,
)

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
INTERVAL = MIN_FETCH_INTERVAL


# --- decision logic -------------------------------------------------------------------------


def test_never_fetched_account_may_fetch_at_once() -> None:
    budget = FetchBudget()

    assert budget.last_fetch("a") is None
    assert budget.is_allowed("a", T0)
    assert budget.next_allowed("a", T0) == T0
    assert budget.pick(["a"], T0) == "a"
    assert budget.next_due(["a"], T0) == T0


def test_just_fetched_account_waits_one_interval() -> None:
    budget = FetchBudget()
    budget.record_fetch("a", T0)

    assert budget.last_fetch("a") == T0
    assert not budget.is_allowed("a", T0)
    assert not budget.is_allowed("a", T0 + timedelta(minutes=5))
    assert budget.next_allowed("a", T0 + timedelta(minutes=5)) == T0 + INTERVAL
    assert budget.pick(["a"], T0 + timedelta(minutes=5)) is None
    assert budget.next_due(["a"], T0 + timedelta(minutes=5)) == T0 + INTERVAL


def test_boundary_is_exactly_one_interval() -> None:
    budget = FetchBudget()
    budget.record_fetch("a", T0)

    just_before = T0 + INTERVAL - timedelta(microseconds=1)
    assert not budget.is_allowed("a", just_before)
    assert budget.pick(["a"], just_before) is None

    assert budget.is_allowed("a", T0 + INTERVAL)
    assert budget.next_allowed("a", T0 + INTERVAL) == T0 + INTERVAL
    assert budget.pick(["a"], T0 + INTERVAL) == "a"


def test_next_allowed_is_never_before_now() -> None:
    budget = FetchBudget()
    budget.record_fetch("a", T0)

    later = T0 + timedelta(hours=3)
    assert budget.next_allowed("a", later) == later


def test_other_times_zones_are_converted_to_utc() -> None:
    budget = FetchBudget()
    cet = datetime(2026, 11, 19, 13, 0, tzinfo=dt_util.get_time_zone("Europe/Berlin"))
    budget.record_fetch("a", cet)

    assert budget.last_fetch("a") == T0
    assert budget.last_fetch("a").tzinfo is UTC  # pyright: ignore[reportOptionalMemberAccess]


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
def test_accounts_take_turns_over_an_hour(count: int) -> None:
    """Asked every 15/n minutes, each account fetches once per 15 minutes, in turn."""
    budget = FetchBudget()
    accounts = [f"account-{index}" for index in range(count)]
    step = INTERVAL / count
    fetches: dict[str, list[datetime]] = {account: [] for account in accounts}
    order: list[str] = []

    now = T0
    while now < T0 + timedelta(hours=1):
        account = budget.pick(accounts, now)
        # Every tick finds a usable account: updates arrive every 15/n minutes.
        assert account is not None, now
        budget.record_fetch(account, now)
        fetches[account].append(now)
        order.append(account)
        now += step

    assert order == accounts * 4
    for times in fetches.values():
        assert len(times) == 4
        assert all(later - earlier >= INTERVAL for earlier, later in pairwise(times))


def test_manual_refreshes_between_turns_do_not_exceed_the_limit() -> None:
    """Extra refreshes in between (restarts, reloads, the button) find nothing to fetch."""
    budget = FetchBudget()
    accounts = ["a", "b", "c"]
    fetches: dict[str, list[datetime]] = {account: [] for account in accounts}

    # A refresh every minute for an hour, far more often than 15/3 minutes.
    for minute in range(60):
        now = T0 + timedelta(minutes=minute)
        account = budget.pick(accounts, now)
        if account is not None:
            budget.record_fetch(account, now)
            fetches[account].append(now)

    for times in fetches.values():
        assert len(times) == 4
        assert all(later - earlier >= INTERVAL for earlier, later in pairwise(times))


def test_early_tick_finds_nothing_and_next_due_says_when() -> None:
    """A scheduler that wakes a second early must retry at next_due, not a whole step later."""
    budget = FetchBudget()
    budget.record_fetch("a", T0)
    budget.record_fetch("b", T0 + timedelta(minutes=7, seconds=30))

    early = T0 + INTERVAL - timedelta(seconds=1)
    assert budget.pick(["a", "b"], early) is None
    assert budget.next_due(["a", "b"], early) == T0 + INTERVAL
    assert budget.pick(["a", "b"], T0 + INTERVAL) == "a"


def test_pick_prefers_the_account_that_waited_longest() -> None:
    budget = FetchBudget()
    budget.record_fetch("old", T0)
    budget.record_fetch("recent", T0 + timedelta(minutes=10))
    now = T0 + timedelta(hours=1)

    assert budget.pick(["recent", "old"], now) == "old"
    # Never fetched waited longest of all.
    assert budget.pick(["recent", "old", "new"], now) == "new"


def test_ties_keep_the_order_of_the_caller() -> None:
    budget = FetchBudget()
    budget.record_fetch("b", T0)
    budget.record_fetch("a", T0)
    now = T0 + INTERVAL

    assert budget.pick(["a", "b"], now) == "a"
    assert budget.pick(["b", "a"], now) == "b"
    # Several never-fetched accounts: the first one listed.
    assert budget.pick(["x", "y", "a"], now) == "x"
    assert budget.pick(["y", "x", "a"], now) == "y"


def test_only_the_accounts_passed_in_take_part() -> None:
    """An account waiting for a new sign-in is left out by the caller and never picked."""
    budget = FetchBudget()

    assert budget.pick(["a"], T0) == "a"
    assert budget.pick([], T0) is None
    assert budget.next_due([], T0) is None


def test_duplicate_ids_count_once() -> None:
    budget = FetchBudget()
    budget.record_fetch("a", T0)

    assert budget.pick(["a", "a"], T0 + timedelta(minutes=1)) is None
    assert budget.pick(["b", "a", "b"], T0 + INTERVAL) == "b"


def test_next_due_is_the_earliest_account() -> None:
    budget = FetchBudget()
    budget.record_fetch("a", T0)
    budget.record_fetch("b", T0 + timedelta(minutes=5))
    now = T0 + timedelta(minutes=6)

    assert budget.next_due(["a", "b"], now) == T0 + INTERVAL
    assert budget.next_due(["b"], now) == T0 + timedelta(minutes=5) + INTERVAL
    assert budget.next_due(["a", "b", "never"], now) == now


def test_clock_set_back_blocks_for_at_most_one_interval(caplog: pytest.LogCaptureFixture) -> None:
    budget = FetchBudget()
    # Recorded while the clock was an hour ahead.
    budget.record_fetch("a", T0 + timedelta(hours=1))

    with caplog.at_level(logging.WARNING):
        assert not budget.is_allowed("a", T0)
    assert "the clock was set back" in caplog.text
    assert budget.next_allowed("a", T0) == T0 + INTERVAL
    # Pulled back once: the block does not move along with the clock.
    assert budget.last_fetch("a") == T0
    assert not budget.is_allowed("a", T0 + timedelta(minutes=10))
    assert budget.is_allowed("a", T0 + INTERVAL)
    assert budget.pick(["a"], T0 + INTERVAL) == "a"


def test_clock_set_back_in_pick_and_next_due() -> None:
    budget = FetchBudget()
    budget.record_fetch("a", T0 + timedelta(days=2))

    assert budget.pick(["a"], T0) is None
    assert budget.next_due(["a"], T0) == T0 + INTERVAL


def test_clock_jumping_forward_allows_one_early_fetch() -> None:
    """Documented limit: a forward jump looks like a long pause, once."""
    budget = FetchBudget()
    budget.record_fetch("a", T0)

    jumped = T0 + timedelta(hours=2)
    assert budget.pick(["a"], jumped) == "a"
    budget.record_fetch("a", jumped)
    # From then on the limit holds against the new clock.
    assert not budget.is_allowed("a", jumped + timedelta(minutes=14))
    assert budget.is_allowed("a", jumped + INTERVAL)


def test_naive_times_are_rejected() -> None:
    budget = FetchBudget()
    naive = datetime(2026, 11, 19, 12, 0)  # noqa: DTZ001

    with pytest.raises(ValueError, match="timezone-aware"):
        budget.record_fetch("a", naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        budget.pick(["a"], naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        budget.next_due(["a"], naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        budget.next_allowed("a", naive)


def test_invalid_settings_are_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        FetchBudget(interval=timedelta(0))
    with pytest.raises(ValueError, match="retention"):
        FetchBudget(interval=timedelta(hours=1), retention=timedelta(minutes=30))


def test_custom_interval() -> None:
    budget = FetchBudget(interval=timedelta(minutes=30))
    budget.record_fetch("a", T0)

    assert not budget.is_allowed("a", T0 + timedelta(minutes=15))
    assert budget.is_allowed("a", T0 + timedelta(minutes=30))


def test_forget_lets_the_account_fetch_at_once() -> None:
    budget = FetchBudget()
    budget.record_fetch("a", T0)
    budget.forget("a")
    budget.forget("unknown")

    assert budget.last_fetch("a") is None
    assert budget.is_allowed("a", T0)


def test_prune_drops_old_records_and_pulls_future_ones_back() -> None:
    budget = FetchBudget()
    budget.record_fetch("old", T0)
    budget.record_fetch("kept", T0 + timedelta(days=1))
    # Recorded while the clock was ahead; recording it already dropped "old".
    budget.record_fetch("future", T0 + RETENTION + timedelta(hours=2))

    now = T0 + RETENTION + timedelta(hours=1)
    budget.prune(now)

    assert budget.as_dict() == {"kept": T0 + timedelta(days=1), "future": now}


def test_recording_at_an_earlier_time_pulls_later_records_back() -> None:
    """Fetches are recorded at the current time, so a later record means the clock went back."""
    budget = FetchBudget()
    budget.record_fetch("a", T0 + timedelta(hours=1))

    budget.record_fetch("b", T0)

    assert budget.as_dict() == {"a": T0, "b": T0}


def test_prune_keeps_a_record_exactly_at_retention() -> None:
    budget = FetchBudget()
    budget.record_fetch("a", T0)

    budget.prune(T0 + RETENTION)
    assert budget.last_fetch("a") == T0


def test_recording_a_fetch_prunes_stale_records() -> None:
    """The store cannot grow forever even if nobody calls prune()."""
    budget = FetchBudget()
    budget.record_fetch("removed", T0)

    budget.record_fetch("a", T0 + RETENTION + timedelta(seconds=1))

    assert budget.as_dict() == {"a": T0 + RETENTION + timedelta(seconds=1)}


def test_account_budget_id_is_stable_and_holds_no_address() -> None:
    budget_id = account_budget_id("Jane.Doe@Example.com")

    assert budget_id == account_budget_id("  jane.doe@example.com ")
    assert budget_id != account_budget_id("john.doe@example.com")
    assert len(budget_id) == 64
    assert all(char in "0123456789abcdef" for char in budget_id)
    assert "example" not in budget_id


# --- persistence ----------------------------------------------------------------------------


def _store_path(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(".storage", STORAGE_KEY))


def _write_store(hass: HomeAssistant, content: str) -> None:
    path = _store_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _store_file(version: int, data: Any) -> str:
    return json.dumps({"version": version, "minor_version": 1, "key": STORAGE_KEY, "data": data})


async def _shut_down(hass: HomeAssistant) -> None:
    """What Home Assistant does on stop: write everything still waiting for its delay."""
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()


async def test_records_survive_a_restart(hass: HomeAssistant) -> None:
    now = dt_util.utcnow()
    first = PersistentFetchBudget(hass)
    await first.async_load()
    first.record_fetch("b", now - timedelta(minutes=5))
    first.record_fetch("a", now)
    await _shut_down(hass)

    second = PersistentFetchBudget(hass)
    await second.async_load()

    assert second.as_dict() == {"a": now, "b": now - timedelta(minutes=5)}
    assert not second.is_allowed("a", now + timedelta(minutes=1))
    assert second.pick(["a", "b"], now + timedelta(minutes=10)) == "b"


async def test_restarts_in_a_row_fetch_each_account_once(hass: HomeAssistant) -> None:
    """R4: three restarts within minutes reach Apple once, not three times."""
    now = dt_util.utcnow()
    fetched: list[str] = []
    for restart in range(3):
        budget = PersistentFetchBudget(hass)
        await budget.async_load()
        at = now + timedelta(minutes=restart)
        if (account := budget.pick(["a"], at)) is not None:
            budget.record_fetch(account, at)
            fetched.append(account)
        await _shut_down(hass)

    assert fetched == ["a"]


async def test_store_holds_only_ids_and_timestamps(hass: HomeAssistant) -> None:
    budget_id = account_budget_id("jane.doe@example.com")
    now = dt_util.utcnow()
    budget = PersistentFetchBudget(hass)
    await budget.async_load()
    budget.record_fetch(budget_id, now)
    await _shut_down(hass)

    raw = _store_path(hass).read_text(encoding="utf-8")
    assert "example.com" not in raw
    stored = json.loads(raw)
    assert stored["version"] == STORAGE_VERSION
    assert stored["data"] == {"accounts": {budget_id: {"last_fetch": now.isoformat()}}}


async def test_saves_are_delayed(hass: HomeAssistant) -> None:
    budget = PersistentFetchBudget(hass)
    await budget.async_load()

    with patch.object(Store, "async_delay_save") as delay_save:
        budget.record_fetch("a", dt_util.utcnow())

    delay_save.assert_called_once()
    assert delay_save.call_args.args[1] == SAVE_DELAY
    assert not _store_path(hass).exists()


async def test_queries_that_change_nothing_do_not_save(hass: HomeAssistant) -> None:
    now = dt_util.utcnow()
    budget = PersistentFetchBudget(hass)
    await budget.async_load()
    budget.record_fetch("a", now)

    with patch.object(Store, "async_delay_save") as delay_save:
        budget.pick(["a", "b"], now)
        budget.next_due(["a", "b"], now)
        budget.forget("unknown")
        budget.prune(now)

    delay_save.assert_not_called()


async def test_clock_set_back_is_saved(hass: HomeAssistant) -> None:
    now = dt_util.utcnow()
    first = PersistentFetchBudget(hass)
    await first.async_load()
    first.record_fetch("a", now + timedelta(hours=1))
    await _shut_down(hass)

    second = PersistentFetchBudget(hass)
    await second.async_load()

    # Pulled back on load, so the file no longer blocks the account until the clock catches up.
    last = second.last_fetch("a")
    assert last is not None
    assert last <= dt_util.utcnow()
    await _shut_down(hass)
    assert (
        json.loads(_store_path(hass).read_text(encoding="utf-8"))["data"]["accounts"]["a"][
            "last_fetch"
        ]
        == last.isoformat()
    )


async def test_load_prunes_stale_records(hass: HomeAssistant) -> None:
    now = dt_util.utcnow()
    _write_store(
        hass,
        _store_file(
            STORAGE_VERSION,
            {
                "accounts": {
                    "removed": {"last_fetch": (now - RETENTION - timedelta(days=1)).isoformat()},
                    "kept": {"last_fetch": (now - timedelta(days=1)).isoformat()},
                }
            },
        ),
    )

    budget = PersistentFetchBudget(hass)
    await budget.async_load()

    assert set(budget.as_dict()) == {"kept"}


async def test_forget_is_saved(hass: HomeAssistant) -> None:
    now = dt_util.utcnow()
    first = PersistentFetchBudget(hass)
    await first.async_load()
    first.record_fetch("a", now)
    first.record_fetch("b", now)
    first.forget("a")
    await _shut_down(hass)

    second = PersistentFetchBudget(hass)
    await second.async_load()
    assert set(second.as_dict()) == {"b"}


async def test_loading_twice_does_nothing(hass: HomeAssistant) -> None:
    budget = PersistentFetchBudget(hass)
    await budget.async_load()

    with patch.object(Store, "async_load") as load:
        await budget.async_load()

    load.assert_not_called()


async def test_concurrent_loads_wait_for_the_first(hass: HomeAssistant) -> None:
    now = dt_util.utcnow()
    _write_store(
        hass, _store_file(STORAGE_VERSION, {"accounts": {"a": {"last_fetch": now.isoformat()}}})
    )
    budget = PersistentFetchBudget(hass)

    await asyncio.gather(budget.async_load(), budget.async_load())

    assert budget.as_dict() == {"a": now}


async def test_record_before_load_is_kept_if_newer(hass: HomeAssistant) -> None:
    now = dt_util.utcnow()
    _write_store(
        hass,
        _store_file(
            STORAGE_VERSION,
            {
                "accounts": {
                    "a": {"last_fetch": (now - timedelta(minutes=30)).isoformat()},
                    "b": {"last_fetch": (now - timedelta(minutes=5)).isoformat()},
                }
            },
        ),
    )

    budget = PersistentFetchBudget(hass)
    budget.record_fetch("b", now - timedelta(minutes=10))
    budget.record_fetch("a", now - timedelta(minutes=1))
    await budget.async_load()

    expected = {"a": now - timedelta(minutes=1), "b": now - timedelta(minutes=5)}
    assert budget.as_dict() == expected

    # The merged records are written, not only what was recorded before the load.
    await _shut_down(hass)
    restarted = PersistentFetchBudget(hass)
    await restarted.async_load()
    assert restarted.as_dict() == expected


async def test_missing_store_starts_empty(hass: HomeAssistant, caplog) -> None:
    budget = PersistentFetchBudget(hass)
    with caplog.at_level(logging.WARNING):
        await budget.async_load()

    assert budget.as_dict() == {}
    assert not [r for r in caplog.records if r.name == "custom_components.findmy.budget"]


async def test_corrupt_store_starts_empty(hass: HomeAssistant, caplog) -> None:
    # The Store reports a corrupt file as a repair issue, which needs the registry.
    await ir.async_load(hass)
    _write_store(hass, "{not json")

    budget = PersistentFetchBudget(hass)
    await budget.async_load()

    assert budget.as_dict() == {}
    if sys.platform != "win32":
        # Moved aside by the Store. On Windows the rename fails (a colon in the new name) and
        # the load ends in the fallback instead; Home Assistant does not support Windows.
        assert not _store_path(hass).exists()
        assert list(_store_path(hass).parent.glob(f"{STORAGE_KEY}.corrupt.*"))

    budget.record_fetch("a", dt_util.utcnow())
    await _shut_down(hass)
    assert "a" in json.loads(_store_path(hass).read_text(encoding="utf-8"))["data"]["accounts"]


async def test_failure_while_loading_starts_empty(hass: HomeAssistant, caplog) -> None:
    """Without the issue registry the corrupt-file path itself fails; setup must still go on."""
    _write_store(hass, "{not json")

    budget = PersistentFetchBudget(hass)
    with caplog.at_level(logging.WARNING):
        await budget.async_load()

    assert budget.as_dict() == {}
    assert "Could not load the fetch budget" in caplog.text


@pytest.mark.parametrize("version", [STORAGE_VERSION + 1, STORAGE_VERSION - 1])
async def test_unknown_version_starts_empty(hass: HomeAssistant, caplog, version: int) -> None:
    now = dt_util.utcnow()
    _write_store(hass, _store_file(version, {"accounts": {"a": {"last_fetch": now.isoformat()}}}))

    budget = PersistentFetchBudget(hass)
    with caplog.at_level(logging.WARNING):
        await budget.async_load()

    assert budget.as_dict() == {}
    assert "Could not load the fetch budget" in caplog.text

    # The next record replaces the file with the current version.
    budget.record_fetch("b", now)
    await _shut_down(hass)
    stored = json.loads(_store_path(hass).read_text(encoding="utf-8"))
    assert stored["version"] == STORAGE_VERSION
    assert set(stored["data"]["accounts"]) == {"b"}


@pytest.mark.parametrize(
    "data",
    [
        [],
        "accounts",
        {},
        {"accounts": []},
        {"accounts": None},
    ],
)
async def test_unexpected_layout_starts_empty(hass: HomeAssistant, caplog, data: Any) -> None:
    _write_store(hass, _store_file(STORAGE_VERSION, data))

    budget = PersistentFetchBudget(hass)
    with caplog.at_level(logging.WARNING):
        await budget.async_load()

    assert budget.as_dict() == {}
    assert "unexpected layout" in caplog.text


async def test_unreadable_entries_are_skipped(hass: HomeAssistant, caplog) -> None:
    now = dt_util.utcnow()
    _write_store(
        hass,
        _store_file(
            STORAGE_VERSION,
            {
                "accounts": {
                    "good": {"last_fetch": now.isoformat()},
                    "naive": {"last_fetch": "2026-11-19T12:00:00"},
                    "garbage": {"last_fetch": "yesterday"},
                    "number": {"last_fetch": 1763553600},
                    "missing": {},
                    "flat": "2026-11-19T12:00:00+00:00",
                }
            },
        ),
    )

    budget = PersistentFetchBudget(hass)
    with caplog.at_level(logging.WARNING):
        await budget.async_load()

    assert budget.as_dict() == {"good": now}
    assert "Skipped 5 unreadable entries" in caplog.text
