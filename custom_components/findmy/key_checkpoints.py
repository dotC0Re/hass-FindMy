"""Checkpoints of the secret chains of rolling-key accessories, kept across restarts.

Purpose
-------
FindMy.py derives the keys of a rolling-key accessory from a chain of secrets that starts at its
pairing, and walks that chain from the start once per run: 96 steps per day since pairing (see
key_cache.py, "The secret chain"). This module keeps a few secrets of each chain in
``.storage/findmy.key_checkpoints``, so that after a restart the walk starts close to where the
keys are needed instead of at the pairing.

What is stored
--------------
Per config entry id of a rolling (derived) device, and for each of its two chains, the chain
secrets at a few key indices, 32 bytes each, with the version of FindMy.py that derived them.
A chain secret is no key: FindMy.py computes it from the SKN or SKS in the config entry, and
turning it into a key pair needs the master key, which only the config entry holds. Someone who
can read the config entry learns nothing from this file, and the file alone gives no key. Like
the config entries, it is readable by the user Home Assistant runs as only. The record of a
device is dropped when its config entry is removed.

Limits
------
* Checkpoints made with another version of FindMy.py are dropped, in case it derives the chain
  differently; the next walk then starts at the pairing once more.
* A lost or broken file only means that, too.
* key_cache.py decides whether the checkpoints can be used at all (_chain_access_works).
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
from typing import TYPE_CHECKING, TypedDict, cast, final

from homeassistant.helpers.storage import Store

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

    from .key_cache import ChainCheckpoints

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "findmy.key_checkpoints"
STORAGE_VERSION = 1
# Checkpoints change about once a week per accessory; there is no hurry to write them.
SAVE_DELAY = 30.0

_SECRET_LENGTH = 32


def _library_version() -> str | None:
    try:
        return importlib.metadata.version("findmy")
    except importlib.metadata.PackageNotFoundError:
        return None


# The version the checkpoints of this run are made with.
LIBRARY_VERSION = _library_version()


class _StoredData(TypedDict):
    library: str | None
    devices: dict[str, dict[str, dict[str, str]]]


def _parse_chain(stored: object) -> dict[int, bytes]:
    """Read the checkpoints of one chain; raises TypeError or ValueError for broken ones."""
    if not isinstance(stored, dict):
        raise TypeError(type(stored).__name__)
    secrets: dict[int, bytes] = {}
    for raw_index, raw_secret in cast("dict[object, object]", stored).items():
        if not isinstance(raw_index, str) or not isinstance(raw_secret, str):
            msg = "index or secret"
            raise TypeError(msg)
        index = int(raw_index)
        secret = bytes.fromhex(raw_secret)
        if index <= 0 or len(secret) != _SECRET_LENGTH:
            msg = "index or secret"
            raise ValueError(msg)
        secrets[index] = secret
    return secrets


def _parse_device(stored: object) -> ChainCheckpoints:
    if not isinstance(stored, dict):
        raise TypeError(type(stored).__name__)
    checkpoints: ChainCheckpoints = {}
    for chain, secrets in cast("dict[object, object]", stored).items():
        if not isinstance(chain, str):
            raise TypeError(type(chain).__name__)
        checkpoints[chain] = _parse_chain(secrets)
    return checkpoints


@final
class KeyCheckpointStore:
    """Chain checkpoints per config entry id, in memory and in Home Assistant's storage.

    Everything here runs on Home Assistant's event loop. Call :meth:`async_load` once before
    the first device is set up.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        # private: what is stored is derived from the secrets of the config entries.
        self._store: Store[_StoredData] = Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True)
        self._devices: dict[str, ChainCheckpoints] = {}
        # A second caller waits for the first load instead of finding half the records.
        self._load_lock = asyncio.Lock()
        self._loaded = False
        self._changed_before_load = False

    async def async_load(self) -> None:
        """Load the stored checkpoints once. A broken store starts empty, with a warning."""
        async with self._load_lock:
            if self._loaded:
                return
            try:
                await self._async_load_checkpoints()
            finally:
                self._loaded = True
                if self._changed_before_load:
                    self._changed()

    async def _async_load_checkpoints(self) -> None:
        try:
            stored = await self._store.async_load()
        except Exception:
            # Unknown version, unreadable file, anything else. The checkpoints only save time.
            _LOGGER.warning(
                "Could not load the key checkpoints from %s, starting without them",
                self._store.path,
                exc_info=True,
            )
            return
        if stored is None:
            # No file yet; a corrupt file has been moved aside and reported by the Store.
            return

        data = cast("Mapping[str, object]", stored)
        if data.get("library") != LIBRARY_VERSION or LIBRARY_VERSION is None:
            _LOGGER.debug(
                "Dropping the key checkpoints of FindMy.py %s (now %s)",
                data.get("library"),
                LIBRARY_VERSION,
            )
            self._changed()
            return
        devices = data.get("devices")
        if not isinstance(devices, dict):
            _LOGGER.warning(
                "The key checkpoints in %s have an unexpected layout, starting without them",
                self._store.path,
            )
            return

        skipped = 0
        for entry_id, record in cast("dict[object, object]", devices).items():
            try:
                if not isinstance(entry_id, str):
                    raise TypeError  # noqa: TRY301
                checkpoints = _parse_device(record)
            except TypeError, ValueError:
                skipped += 1
                continue
            # Something remembered before the load is newer than what was on disk.
            _ = self._devices.setdefault(entry_id, checkpoints)
        if skipped:
            _LOGGER.warning(
                "Skipped %i unreadable entries of the key checkpoints in %s",
                skipped,
                self._store.path,
            )

    def get(self, entry_id: str) -> ChainCheckpoints:
        """The checkpoints of the device of a config entry; empty if none are known."""
        return self._devices.get(entry_id, {})

    def remember(self, entry_id: str, checkpoints: ChainCheckpoints) -> None:
        """Keep the checkpoints of a device in place of earlier ones, and save them soon."""
        if not checkpoints or LIBRARY_VERSION is None or self._devices.get(entry_id) == checkpoints:
            return
        self._devices[entry_id] = checkpoints
        self._changed()

    def forget(self, entry_id: str) -> None:
        """Drop the checkpoints of a device whose config entry was removed."""
        if self._devices.pop(entry_id, None) is not None:
            self._changed()

    def _changed(self) -> None:
        if not self._loaded:
            # A pending save would be what Store.async_load returns instead of the file, and
            # the checkpoints on disk would be lost. Saved once the load has merged them.
            self._changed_before_load = True
            return
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    def _data_to_save(self) -> _StoredData:
        return {
            "library": LIBRARY_VERSION,
            "devices": {
                entry_id: {
                    chain: {str(index): secret.hex() for index, secret in secrets.items()}
                    for chain, secrets in checkpoints.items()
                }
                for entry_id, checkpoints in self._devices.items()
            },
        }
