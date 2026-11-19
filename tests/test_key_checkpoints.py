"""The checkpoints of the key chains, kept across restarts in Home Assistant's storage (M4).

Covers the stored file (layout, file mode, library version, broken records) and how the storage
uses it: checkpoints are restored when a rolling-key accessory is set up, kept when it is
unloaded or Home Assistant stops, and forgotten when its entry is removed. Keys are random;
nothing here talks to Apple.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE, EVENT_HOMEASSISTANT_STOP

from custom_components.findmy import async_remove_entry
from custom_components.findmy import key_checkpoints as checkpoints_module
from custom_components.findmy.const import DOMAIN
from custom_components.findmy.key_cache import (
    PRIMARY_CHAIN,
    SECONDARY_CHAIN,
    CachedFindMyAccessory,
)
from custom_components.findmy.key_checkpoints import STORAGE_KEY, KeyCheckpointStore
from custom_components.findmy.storage import RuntimeStorage

from .conftest import make_config_entry
from .test_key_chain import MAX_STEPS_FROM_CHECKPOINTS, accessory_data, chain_steps

if TYPE_CHECKING:
    from findmy import FindMyAccessoryMapping
    from homeassistant.core import HomeAssistant

# A fixture of test_key_chain.py, used here too.
__all__ = ["chain_steps"]


# --- The stored file ------------------------------------------------------------------------


def store_path(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(".storage", STORAGE_KEY))


async def final_write(hass: HomeAssistant) -> None:
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()


def some_checkpoints() -> dict[str, dict[int, bytes]]:
    return {
        PRIMARY_CHAIN: {672: os.urandom(32), 1344: os.urandom(32)},
        SECONDARY_CHAIN: {4: os.urandom(32)},
    }


async def test_checkpoints_survive_a_restart(hass: HomeAssistant) -> None:
    store = KeyCheckpointStore(hass)
    await store.async_load()
    checkpoints = some_checkpoints()
    store.remember("entry", checkpoints)
    await final_write(hass)

    again = KeyCheckpointStore(hass)
    await again.async_load()

    assert again.get("entry") == checkpoints
    assert again.get("other") == {}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
async def test_file_is_readable_by_home_assistant_only(hass: HomeAssistant) -> None:
    store = KeyCheckpointStore(hass)
    await store.async_load()
    store.remember("entry", some_checkpoints())
    await final_write(hass)

    assert stat.S_IMODE(store_path(hass).stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR


async def test_checkpoints_of_another_library_version_are_dropped(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = KeyCheckpointStore(hass)
    await store.async_load()
    store.remember("entry", some_checkpoints())
    await final_write(hass)

    monkeypatch.setattr(checkpoints_module, "LIBRARY_VERSION", "99.0")
    updated = KeyCheckpointStore(hass)
    await updated.async_load()
    await final_write(hass)

    assert updated.get("entry") == {}
    stored = json.loads(store_path(hass).read_text(encoding="utf-8"))
    assert stored["data"] == {"library": "99.0", "devices": {}}


async def test_unreadable_entries_are_skipped_and_the_rest_is_kept(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    good = {PRIMARY_CHAIN: {"672": "ab" * 32}}
    path = store_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = {
        "version": 1,
        "key": STORAGE_KEY,
        "data": {
            "library": checkpoints_module.LIBRARY_VERSION,
            "devices": {
                "good": good,
                "short": {PRIMARY_CHAIN: {"672": "ab"}},
                "index": {PRIMARY_CHAIN: {"zero": "ab" * 32}},
                "layout": ["not", "a", "mapping"],
            },
        },
    }
    _ = path.write_text(json.dumps(content), encoding="utf-8")

    store = KeyCheckpointStore(hass)
    await store.async_load()

    assert store.get("good") == {PRIMARY_CHAIN: {672: bytes.fromhex("ab" * 32)}}
    assert store.get("short") == {}
    assert "Skipped 3 unreadable" in caplog.text


async def test_forgotten_checkpoints_are_gone_after_a_restart(hass: HomeAssistant) -> None:
    store = KeyCheckpointStore(hass)
    await store.async_load()
    store.remember("entry", some_checkpoints())
    await final_write(hass)
    store.forget("entry")
    await final_write(hass)

    again = KeyCheckpointStore(hass)
    await again.async_load()
    assert again.get("entry") == {}


# --- Through the storage --------------------------------------------------------------------


def device_entry(hass: HomeAssistant, data: FindMyAccessoryMapping) -> Any:
    return make_config_entry(hass, {"type": "device_rolling_derived", "data": data})


async def test_storage_restores_checkpoints_kept_when_home_assistant_stopped(
    hass: HomeAssistant, chain_steps: list[None]
) -> None:
    data = accessory_data()
    entry = device_entry(hass, data)
    storage = RuntimeStorage(hass)
    await storage.key_checkpoints.async_load()
    accessory = await storage.add_entry(entry)
    assert isinstance(accessory, CachedFindMyAccessory)
    # What the Bluetooth matching does without any account.
    _ = await hass.async_add_executor_job(accessory.search_window, datetime.now(tz=UTC))

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    await final_write(hass)

    restarted = RuntimeStorage(hass)
    await restarted.key_checkpoints.async_load()
    chain_steps.clear()
    again = await restarted.add_entry(entry)
    assert isinstance(again, CachedFindMyAccessory)
    _ = await hass.async_add_executor_job(again.search_window, datetime.now(tz=UTC))
    assert len(chain_steps) < MAX_STEPS_FROM_CHECKPOINTS


async def test_unloading_keeps_and_removing_forgets_the_checkpoints(hass: HomeAssistant) -> None:
    data = accessory_data()
    entry = device_entry(hass, data)
    storage = RuntimeStorage.attach(hass)
    await storage.key_checkpoints.async_load()
    accessory = await storage.add_entry(entry)
    assert isinstance(accessory, CachedFindMyAccessory)
    _ = await hass.async_add_executor_job(accessory.search_window, datetime.now(tz=UTC))

    _ = await storage.del_entry(entry)
    assert storage.key_checkpoints.get(entry.entry_id) != {}

    await async_remove_entry(hass, entry)
    assert storage.key_checkpoints.get(entry.entry_id) == {}
    assert DOMAIN in hass.data
