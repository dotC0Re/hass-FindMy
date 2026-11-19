"""The latest location report of each device survives a restart (last_reports.py).

Since the fetch limit of an account is kept across restarts, the first fetch after a restart
can be up to 15 minutes away. Without the stored reports every tracker would be without a
location until then. Nothing here talks to Apple; reports are plain objects with the attributes
the entities read.
"""

from __future__ import annotations

import json
import logging
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE

from custom_components.findmy import async_remove_entry, async_setup_entry, async_unload_entry
from custom_components.findmy.last_reports import (
    STORAGE_KEY,
    STORAGE_VERSION,
    LastReportStore,
    RestoredReport,
)
from custom_components.findmy.storage import RuntimeStorage

from . import test_fetch_budget_paths as paths
from .conftest import FetchClock, KeyPairFactory, make_config_entry

if TYPE_CHECKING:
    from unittest.mock import MagicMock

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

# The mocked entity platforms and Apple accounts of the tests that go through setup.
platforms = paths.platforms
apple = paths.apple

SEEN_AT = datetime(2026, 11, 19, 8, 30, tzinfo=UTC)
EMAIL = "jane@example.com"


def make_report(**changes: Any) -> SimpleNamespace:
    """Something with the attributes the integration reads from a FindMy location report."""
    values: dict[str, Any] = {
        "timestamp": SEEN_AT,
        "latitude": 52.520008,
        "longitude": 13.404954,
        "horizontal_accuracy": 35,
        "status": 0x40,
    }
    return SimpleNamespace(**(values | changes))


def restored(**changes: Any) -> RestoredReport:
    return RestoredReport.from_report(make_report(**changes))  # pyright: ignore[reportArgumentType]


def store_path(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(".storage", STORAGE_KEY))


def write_store(hass: HomeAssistant, content: str) -> None:
    path = store_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(content, encoding="utf-8")


def store_file(reports: Any, version: int = STORAGE_VERSION) -> str:
    return json.dumps(
        {"version": version, "minor_version": 1, "key": STORAGE_KEY, "data": {"reports": reports}}
    )


async def final_write(hass: HomeAssistant) -> None:
    """What Home Assistant does on stop: write everything still waiting for its delay."""
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()


# --- The stored record ------------------------------------------------------------------


def test_record_round_trips_through_its_stored_form() -> None:
    report = restored()

    assert RestoredReport.from_dict(json.loads(json.dumps(report.as_dict()))) == report


def test_record_holds_only_what_the_entities_show() -> None:
    """No payload and no key: nothing that could decrypt or prove anything beyond the position."""
    assert set(restored().as_dict()) == {
        "timestamp",
        "latitude",
        "longitude",
        "horizontal_accuracy",
        "status",
    }


@pytest.mark.parametrize(
    "broken",
    [
        None,
        "text",
        [],
        {},
        {"timestamp": 5},
        {"timestamp": "not a time"},
        {"timestamp": "2026-11-19T08:30:00"},  # no time zone
        {"latitude": "52.5"},
        {"latitude": True},
        {"latitude": 91.0},
        {"longitude": -181},
        {"horizontal_accuracy": 3.5},
        {"horizontal_accuracy": 256},
        {"status": -1},
        {"status": None},
    ],
)
def test_broken_record_is_rejected(broken: Any) -> None:
    record: Any = broken
    if isinstance(broken, dict) and broken:
        record = {**restored().as_dict(), **broken}

    with pytest.raises((KeyError, TypeError, ValueError)):
        _ = RestoredReport.from_dict(record)


# --- The store ----------------------------------------------------------------------------


async def test_reports_survive_a_restart(hass: HomeAssistant) -> None:
    first = LastReportStore(hass)
    await first.async_load()
    first.remember("tag", make_report())  # pyright: ignore[reportArgumentType]
    await final_write(hass)

    second = LastReportStore(hass)
    await second.async_load()

    assert second.get("tag") == restored()
    assert second.get("other") is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
async def test_file_is_readable_by_home_assistant_only(hass: HomeAssistant) -> None:
    store = LastReportStore(hass)
    await store.async_load()
    store.remember("tag", make_report())  # pyright: ignore[reportArgumentType]
    await final_write(hass)

    assert stat.S_IMODE(store_path(hass).stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR


async def test_report_remembered_before_the_load_does_not_wipe_the_file(
    hass: HomeAssistant,
) -> None:
    """Home Assistant's Store returns a pending save instead of the file when loading."""
    write_store(hass, store_file({"old": restored().as_dict()}))
    newer = make_report(timestamp=SEEN_AT + timedelta(hours=1), latitude=48.1)

    store = LastReportStore(hass)
    store.remember("new", newer)  # pyright: ignore[reportArgumentType]
    await store.async_load()
    await final_write(hass)

    again = LastReportStore(hass)
    await again.async_load()
    assert again.get("old") == restored()
    assert again.get("new") == restored(timestamp=SEEN_AT + timedelta(hours=1), latitude=48.1)


async def test_newer_report_in_memory_wins_over_the_file(hass: HomeAssistant) -> None:
    write_store(hass, store_file({"tag": restored().as_dict()}))
    newer = make_report(timestamp=SEEN_AT + timedelta(minutes=20), longitude=11.5)

    store = LastReportStore(hass)
    store.remember("tag", newer)  # pyright: ignore[reportArgumentType]
    await store.async_load()

    assert store.get("tag") == restored(timestamp=SEEN_AT + timedelta(minutes=20), longitude=11.5)


async def test_forgotten_report_is_gone_after_a_restart(hass: HomeAssistant) -> None:
    write_store(hass, store_file({"tag": restored().as_dict(), "kept": restored().as_dict()}))
    store = LastReportStore(hass)
    await store.async_load()

    store.forget("tag")
    store.forget("never known")
    await final_write(hass)

    again = LastReportStore(hass)
    await again.async_load()
    assert again.get("tag") is None
    assert again.get("kept") == restored()


async def test_corrupt_file_starts_without_reports(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """However Home Assistant deals with the file (see test_budget.py): logged, and no error."""
    write_store(hass, "{not json")

    store = LastReportStore(hass)
    with caplog.at_level(logging.WARNING):
        await store.async_load()

    assert store.get("tag") is None
    assert any(
        record.levelno >= logging.WARNING and STORAGE_KEY in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("reports", [None, [], "text", 5])
async def test_unexpected_layout_starts_without_reports(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, reports: Any
) -> None:
    write_store(hass, store_file(reports))

    store = LastReportStore(hass)
    with caplog.at_level(logging.WARNING):
        await store.async_load()

    assert store.get("tag") is None
    assert "unexpected layout" in caplog.text


async def test_unreadable_entries_are_skipped_and_the_rest_is_kept(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    broken = {**restored().as_dict(), "latitude": "north"}
    write_store(hass, store_file({"good": restored().as_dict(), "bad": broken}))

    store = LastReportStore(hass)
    with caplog.at_level(logging.WARNING):
        await store.async_load()

    assert store.get("good") == restored()
    assert store.get("bad") is None
    assert "Skipped 1 unreadable" in caplog.text


async def test_unchanged_report_is_not_written_again(hass: HomeAssistant) -> None:
    store = LastReportStore(hass)
    await store.async_load()
    store.remember("tag", make_report())  # pyright: ignore[reportArgumentType]
    await final_write(hass)
    written = store_path(hass).stat().st_mtime_ns
    store_path(hass).unlink()

    store.remember("tag", make_report())  # pyright: ignore[reportArgumentType]
    await final_write(hass)

    assert written
    assert not store_path(hass).exists()


# --- Through setup, fetch and restart -----------------------------------------------------


def device_entry(hass: HomeAssistant, keypair_factory: KeyPairFactory) -> ConfigEntry:
    return make_config_entry(hass, {"type": "device_static", "data": keypair_factory().to_json()})


async def set_up_device(hass: HomeAssistant, entry: ConfigEntry) -> Any:
    """Set a device entry up and register its device the way its entities would."""
    assert await async_setup_entry(hass, entry) is True
    storage = RuntimeStorage.get(hass)
    device = storage.get_entry(entry)
    _ = storage.coordinator.async_add_listener(lambda: None, device)
    return device


async def fetch_once(
    hass: HomeAssistant, apple_accounts: paths.Apple, device: Any, report: Any
) -> None:
    """Let the newest account answer the refresh that follows setup with one report."""
    apple_accounts[EMAIL][-1].fetch_location.return_value = {device: report}
    await paths.run_delayed_refresh(hass, RuntimeStorage.get(hass).coordinator)


async def test_location_is_restored_after_a_restart_without_a_fetch(
    hass: HomeAssistant,
    platforms: MagicMock,
    apple: paths.Apple,
    fetch_clock: FetchClock,
    keypair_factory: KeyPairFactory,
) -> None:
    _ = await paths.start(hass)
    account = paths.account_entry(hass)
    tag = device_entry(hass, keypair_factory)
    assert await async_setup_entry(hass, account) is True
    device = await set_up_device(hass, tag)
    await fetch_once(hass, apple, device, make_report())
    assert paths.fetches(apple) == 1

    fetch_clock.advance(timedelta(minutes=2))
    storage = await paths.restart(hass)
    assert storage.coordinator.data is None
    assert await async_setup_entry(hass, account) is True
    device = await set_up_device(hass, tag)

    # Known as soon as the device is set up, before any refresh.
    assert storage.coordinator.data == {device: restored()}

    await paths.run_delayed_refresh(hass, storage.coordinator)

    # The fetch limit turned the refresh away, and the restored location is still there.
    assert paths.fetches(apple) == 1
    assert storage.coordinator.last_update_success is True
    assert storage.coordinator.data == {device: restored()}


async def test_fetched_report_replaces_the_restored_one(
    hass: HomeAssistant,
    platforms: MagicMock,
    apple: paths.Apple,
    fetch_clock: FetchClock,
    keypair_factory: KeyPairFactory,
) -> None:
    _ = await paths.start(hass)
    account = paths.account_entry(hass)
    tag = device_entry(hass, keypair_factory)
    assert await async_setup_entry(hass, account) is True
    device = await set_up_device(hass, tag)
    await fetch_once(hass, apple, device, make_report())

    fetch_clock.advance(timedelta(minutes=20))
    storage = await paths.restart(hass)
    assert await async_setup_entry(hass, account) is True
    device = await set_up_device(hass, tag)
    newer = make_report(timestamp=SEEN_AT + timedelta(minutes=18), latitude=48.137154)
    await fetch_once(hass, apple, device, newer)

    assert paths.fetches(apple) == 2
    assert storage.coordinator.data == {device: newer}
    # And that one is what the next restart starts with.
    assert storage.reports.get(tag.entry_id) == restored(
        timestamp=SEEN_AT + timedelta(minutes=18), latitude=48.137154
    )


async def test_reloaded_device_keeps_its_location(
    hass: HomeAssistant,
    platforms: MagicMock,
    apple: paths.Apple,
    fetch_clock: FetchClock,
    keypair_factory: KeyPairFactory,
) -> None:
    """A reload creates a new device object; whether or not the coordinator still finds the
    fetched report under it (it does for a static key, which compares by its key), the device
    has its location."""
    storage = await paths.start(hass)
    tag = device_entry(hass, keypair_factory)
    assert await async_setup_entry(hass, paths.account_entry(hass)) is True
    device = await set_up_device(hass, tag)
    await fetch_once(hass, apple, device, make_report())

    assert await async_unload_entry(hass, tag) is True
    reloaded = await set_up_device(hass, tag)

    assert reloaded is not device
    known = (storage.coordinator.data or {}).get(reloaded)
    assert known is not None
    assert RestoredReport.from_report(known) == restored()


async def test_removed_device_is_forgotten(
    hass: HomeAssistant,
    platforms: MagicMock,
    apple: paths.Apple,
    fetch_clock: FetchClock,
    keypair_factory: KeyPairFactory,
) -> None:
    storage = await paths.start(hass)
    tag = device_entry(hass, keypair_factory)
    assert await async_setup_entry(hass, paths.account_entry(hass)) is True
    device = await set_up_device(hass, tag)
    await fetch_once(hass, apple, device, make_report())
    assert storage.reports.get(tag.entry_id) == restored()

    assert await async_unload_entry(hass, tag) is True
    await async_remove_entry(hass, tag)
    await final_write(hass)

    assert storage.reports.get(tag.entry_id) is None
    assert (
        tag.entry_id
        not in json.loads(store_path(hass).read_text(encoding="utf-8"))["data"]["reports"]
    )


@pytest.mark.requires_bluetooth
async def test_tracker_shows_the_restored_location(
    hass: HomeAssistant, keypair_factory: KeyPairFactory
) -> None:
    from custom_components.findmy import device_tracker  # noqa: PLC0415  # needs Bluetooth

    write_store(hass, store_file({"entry-1": restored().as_dict()}))
    storage = RuntimeStorage(hass)
    await storage.reports.async_load()
    device = keypair_factory()

    storage.coordinator.restore_report(device, "entry-1")
    tracker = device_tracker.FindMyDeviceTracker(storage.coordinator, device, "entry-1")

    assert tracker.latitude == pytest.approx(52.520008)
    assert tracker.longitude == pytest.approx(13.404954)
    assert tracker.location_accuracy == 35.0
    assert tracker.detected_at == SEEN_AT
    assert tracker.status == 0x40
