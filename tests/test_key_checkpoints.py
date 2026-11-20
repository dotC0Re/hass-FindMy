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
from findmy import FindMyAccessory
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE, EVENT_HOMEASSISTANT_STOP

from custom_components.findmy import async_remove_entry
from custom_components.findmy import key_checkpoints as checkpoints_module
from custom_components.findmy.const import DOMAIN
from custom_components.findmy.key_cache import (
    CHECKPOINT_TAG_LENGTH,
    PRIMARY_CHAIN,
    SECONDARY_CHAIN,
    CachedFindMyAccessory,
    ChainCheckpoint,
    ChainCheckpoints,
)
from custom_components.findmy.key_checkpoints import (
    STORAGE_KEY,
    STORAGE_VERSION,
    KeyCheckpointStore,
)
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


def checkpoint() -> ChainCheckpoint:
    return ChainCheckpoint(os.urandom(32), os.urandom(CHECKPOINT_TAG_LENGTH))


def some_checkpoints() -> ChainCheckpoints:
    """Checkpoints of the right form; whether they verify is not the store's business."""
    return {
        PRIMARY_CHAIN: {672: checkpoint(), 1344: checkpoint()},
        SECONDARY_CHAIN: {4: checkpoint()},
    }


def write_store(hass: HomeAssistant, data: object, version: int = STORAGE_VERSION) -> None:
    path = store_path(hass)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = {"version": version, "minor_version": 1, "key": STORAGE_KEY, "data": data}
    _ = path.write_text(json.dumps(content), encoding="utf-8")


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
    tag = "cd" * CHECKPOINT_TAG_LENGTH
    good = {PRIMARY_CHAIN: {"672": {"secret": "ab" * 32, "tag": tag}}}
    write_store(
        hass,
        {
            "library": checkpoints_module.LIBRARY_VERSION,
            "devices": {
                "good": good,
                "short": {PRIMARY_CHAIN: {"672": {"secret": "ab", "tag": tag}}},
                "index": {PRIMARY_CHAIN: {"zero": {"secret": "ab" * 32, "tag": tag}}},
                "no tag": {PRIMARY_CHAIN: {"672": {"secret": "ab" * 32}}},
                "layout": ["not", "a", "mapping"],
            },
        },
    )

    store = KeyCheckpointStore(hass)
    await store.async_load()

    expected = ChainCheckpoint(bytes.fromhex("ab" * 32), bytes.fromhex(tag))
    assert store.get("good") == {PRIMARY_CHAIN: {672: expected}}
    assert store.get("short") == {}
    assert "Skipped 4 unreadable" in caplog.text


async def test_checkpoints_of_version_1_without_tags_are_dropped(hass: HomeAssistant) -> None:
    write_store(
        hass,
        {
            "library": checkpoints_module.LIBRARY_VERSION,
            "devices": {"entry": {PRIMARY_CHAIN: {"672": "ab" * 32}}},
        },
        version=1,
    )

    store = KeyCheckpointStore(hass)
    await store.async_load()

    assert store.get("entry") == {}


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


async def stored_checkpoints_of(hass: HomeAssistant, data: FindMyAccessoryMapping) -> Any:
    """The checkpoints an accessory of this data leaves in the file once it has derived keys."""
    accessory = CachedFindMyAccessory.from_json(data)
    assert isinstance(accessory, CachedFindMyAccessory)
    _ = await hass.async_add_executor_job(accessory.search_window, datetime.now(tz=UTC))
    checkpoints = await hass.async_add_executor_job(accessory.chain_checkpoints)
    return {
        chain: {
            str(ind): {"secret": cp.secret.hex(), "tag": cp.tag.hex()} for ind, cp in stored.items()
        }
        for chain, stored in checkpoints.items()
    }


@pytest.mark.parametrize(
    "damage", ["flipped byte in a secret", "record of another accessory", "wrong index"]
)
async def test_damaged_record_in_the_file_costs_no_key_and_is_overwritten(
    hass: HomeAssistant, damage: str, caplog: pytest.LogCaptureFixture
) -> None:
    data = accessory_data()
    entry = device_entry(hass, data)
    record = await stored_checkpoints_of(hass, accessory_data() if "another" in damage else data)
    primary = record[PRIMARY_CHAIN]
    ind = max(primary, key=int)
    if damage == "flipped byte in a secret":
        secret = primary[ind]["secret"]
        primary[ind]["secret"] = ("0" if secret[0] != "0" else "1") + secret[1:]
    elif damage == "wrong index":
        primary[str(int(ind) + 4)] = primary.pop(ind)
    write_store(
        hass,
        {"library": checkpoints_module.LIBRARY_VERSION, "devices": {entry.entry_id: record}},
    )

    storage = RuntimeStorage(hass)
    await storage.key_checkpoints.async_load()
    accessory = await storage.add_entry(entry)
    assert isinstance(accessory, CachedFindMyAccessory)

    assert "do not belong to it" in caplog.text
    assert storage.key_checkpoints.get(entry.entry_id) == {}
    now = datetime.now(tz=UTC)
    window = await hass.async_add_executor_job(accessory.search_window, now)
    plain = FindMyAccessory.from_json(data)
    for index in range(window.lowest, window.highest + 1):
        assert {k.private_key_bytes for k in window.keys_at(index)} == {
            k.private_key_bytes for k in plain.keys_at(index)
        }
    # The next save writes the accessory's own checkpoints, which verify.
    await storage.async_keep_checkpoints(entry.entry_id, accessory)
    await final_write(hass)
    again = KeyCheckpointStore(hass)
    await again.async_load()
    fresh = CachedFindMyAccessory.from_json(data)
    assert isinstance(fresh, CachedFindMyAccessory)
    assert fresh.restore_chain_checkpoints(again.get(entry.entry_id)) is True


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
