"""What the integration keeps in memory while Home Assistant runs.

One :class:`RuntimeStorage` exists per Home Assistant instance. It maps config entries to the
object each of them stands for (an Apple account or a device), and owns what all entries share:
the coordinator, the FindMy worker thread, the fetch budget of the accounts (budget.py), the
latest location report of each device (last_reports.py) and the checkpoints of the key chains
of rolling-key accessories (key_checkpoints.py).

Accounts are stored as :class:`~.account.AccountRuntime`. The library account behind one lives
on the worker thread and is created and closed there, see account.py.
"""

# pyright: reportImportCycles=false

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from findmy import FixedRollingKeyPairAccessory, KeyPair

from .account import AccountRuntime, async_local_anisette_libs
from .budget import PersistentFetchBudget
from .const import DOMAIN
from .coordinator import FindMyCoordinator, FindMyDevice
from .key_cache import CachedFindMyAccessory
from .key_checkpoints import KeyCheckpointStore
from .last_reports import LastReportStore
from .worker import FindMyWorker

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event, HomeAssistant

    from .config_flow import EntryData
    from .local_bluetooth import LocalObservation

type StorageItem = AccountRuntime | FindMyDevice

_LOGGER = logging.getLogger(__name__)

# Stopping cancels the running job and closes every account, each close limited to a few
# seconds by the worker. A job that cannot be interrupted must not hold up the shutdown of Home
# Assistant for longer than this; the worker thread is a daemon and ends with the process.
_WORKER_STOP_TIMEOUT = 30.0


class RuntimeStorage:
    def __init__(self, hass: HomeAssistant) -> None:
        self._entries: dict[str, StorageItem] = {}
        # Latest local Bluetooth match per accessory unique id, with the scanner source.
        self.local_observations: dict[str, tuple[LocalObservation, str | None]] = {}
        # Latest Offline Finding status byte heard locally per accessory, with its time.
        # DULT advertisements carry no status byte and never end up here.
        self.local_status: dict[str, tuple[int, datetime]] = {}
        # Signal strength of accessories that are currently heard locally.
        self.local_rssi: dict[str, int | None] = {}

        self._hass: HomeAssistant = hass
        # When each Apple account last fetched, kept across restarts. Loaded in async_setup,
        # and again (a no-op then) by the coordinator before it decides anything.
        self.budget: PersistentFetchBudget = PersistentFetchBudget(hass)
        # The latest report of each device, kept across restarts so that the entities have a
        # location before the first fetch. Loaded in async_setup.
        self.reports: LastReportStore = LastReportStore(hass)
        # Where the key chain of each rolling-key accessory may start after a restart instead
        # of at its pairing. Loaded in async_setup; kept up to date by the coordinator after
        # every fetch, and here when an accessory is unloaded or Home Assistant stops.
        self.key_checkpoints: KeyCheckpointStore = KeyCheckpointStore(hass)
        self._coordinator: FindMyCoordinator = FindMyCoordinator(self._hass, self)
        self._worker: FindMyWorker | None = None
        # Also what the Bluetooth matching derived, in a setup without any account. Written by
        # the delayed save that Home Assistant flushes at its final write.
        _ = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_keep_all_checkpoints)

    @classmethod
    def attach(cls, hass: HomeAssistant) -> RuntimeStorage:
        if hass.data.get(DOMAIN, None) is not None:
            msg = "Already attached!"
            raise RuntimeError(msg)

        storage = cls(hass)
        hass.data[DOMAIN] = storage

        return storage

    @classmethod
    def get(cls, hass: HomeAssistant) -> RuntimeStorage:
        if hass.data.get(DOMAIN, None) is None:
            msg = "No storage attached!"
            raise RuntimeError(msg)

        return cast("RuntimeStorage", hass.data[DOMAIN])

    @property
    def coordinator(self) -> FindMyCoordinator:
        return self._coordinator

    @property
    def worker(self) -> FindMyWorker:
        """The thread every call into the FindMy library of a stored account runs on.

        Started on first use, so that nothing that never signs in or fetches pays for a
        thread, and stopped together with Home Assistant. It is deliberately not stopped when
        the last entry is unloaded: a worker cannot be restarted, and callers rely on getting
        a running one for as long as Home Assistant runs.
        """
        if self._worker is None:
            worker = FindMyWorker()
            worker.start()

            async def _async_stop_worker(_event: Event) -> None:
                try:
                    async with asyncio.timeout(_WORKER_STOP_TIMEOUT):
                        await worker.async_stop()
                except TimeoutError:
                    # The stop goes on in the background, see FindMyWorker.async_stop.
                    _LOGGER.warning("The FindMy worker thread did not stop in time")

            _ = self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop_worker)
            self._worker = worker

        return self._worker

    @property
    def accounts(self) -> list[AccountRuntime]:
        return [item for item in self._entries.values() if isinstance(item, AccountRuntime)]

    def get_entry(self, entry: ConfigEntry[EntryData]) -> StorageItem:
        return self._entries[entry.entry_id]

    async def async_keep_checkpoints(self, entry_id: str, accessory: CachedFindMyAccessory) -> None:
        """Remember where the key chains of an accessory may start after a restart."""
        checkpoints = await self._hass.async_add_executor_job(accessory.chain_checkpoints)
        self.key_checkpoints.remember(entry_id, checkpoints)

    async def _async_keep_all_checkpoints(self, _event: Event) -> None:
        for entry_id, item in list(self._entries.items()):
            if isinstance(item, CachedFindMyAccessory):
                await self.async_keep_checkpoints(entry_id, item)

    def get_item(self, entry_id: str) -> StorageItem | None:
        """The object stored for a config entry, or None while the entry is not loaded."""
        return self._entries.get(entry_id)

    def get_entry_id(self, item: StorageItem) -> str | None:
        """The id of the config entry an item was stored for, if it still is."""
        return next(
            (entry_id for entry_id, stored in self._entries.items() if stored is item),
            None,
        )

    async def add_entry(self, entry: ConfigEntry) -> StorageItem:
        """Create and store the object a config entry stands for.

        For an account entry this raises ConfigEntryError or ConfigEntryAuthFailed if the
        account cannot be used (see AccountRuntime.async_load); nothing is stored then.
        """
        data = cast("EntryData", entry.data)  # pyright: ignore[reportInvalidCast]

        if data["type"] == "account":
            # `get`: an entry without account data is damaged like any other, and is reported
            # the same way instead of with a KeyError from here.
            account_data = data.get("account_data")
            # The integrated Anisette provider loads its libraries from one verified bundle
            # (anisette_libs.py), which has to be in place before the account is restored.
            # This also raises ConfigEntryNotReady, while the bundle cannot be downloaded,
            # and ConfigEntryError for a bundle that was changed on disk.
            # None for a remote Anisette server, which nothing of this applies to.
            libs_path = await async_local_anisette_libs(self._hass, account_data)
            account = await AccountRuntime.async_load(
                self.worker, entry.entry_id, account_data, anisette_libs_path=libs_path
            )

            _LOGGER.debug(
                "Storing entry %s as account: (%s)",
                entry.entry_id,
                account.account_name,
            )

            self._entries[entry.entry_id] = account
            return account

        if data["type"] == "device_static":
            key = KeyPair.from_json(data["data"])

            _LOGGER.debug("Storing entry %s as static tag: %s", entry.entry_id, key.name)

            self._entries[entry.entry_id] = key
            return key

        if data["type"] == "device_rolling_derived":
            # Derived keys are kept between fetches and local refreshes, see key_cache.py.
            accessory = CachedFindMyAccessory.from_json(data["data"])
            # Before anything derives a key, so that the key chain starts close to where its
            # keys are needed instead of at the pairing.
            checkpoints = self.key_checkpoints.get(entry.entry_id)
            if checkpoints and isinstance(accessory, CachedFindMyAccessory):
                if accessory.restore_chain_checkpoints(checkpoints):
                    _LOGGER.debug("Restored the key chain checkpoints of %s", accessory.name)
                else:
                    # Not taken, as for checkpoints that do not belong to the accessory: the
                    # record goes, and the next save writes the accessory's own ones.
                    self.key_checkpoints.forget(entry.entry_id)

            _LOGGER.debug(
                "Storing entry %s as rolling (derived) tag: %s",
                entry.entry_id,
                accessory.name,
            )

            self._entries[entry.entry_id] = accessory
            return accessory

        if data["type"] == "device_rolling_pre_generated":
            accessory = FixedRollingKeyPairAccessory.from_json(data["data"])

            _LOGGER.debug(
                "Storing entry %s as rolling (pre-generated) tag: %s",
                entry.entry_id,
                accessory.name,
            )

            self._entries[entry.entry_id] = accessory
            return accessory

        msg = f"Could not match entry {data['type']} with StorageItem!"
        raise ValueError(msg)

    async def del_entry(self, entry: ConfigEntry[EntryData]) -> StorageItem:
        """Forget the object of a config entry; an account is closed on the worker."""
        item = self._entries.pop(entry.entry_id)

        if isinstance(item, AccountRuntime):
            # A fetch with the account holds the worker for up to two minutes, and the close
            # would queue behind it. Removed from the entries first, so that no new refresh
            # picks the account in the meantime.
            await self._coordinator.async_cancel_fetch(item)
            await item.async_close()
        elif isinstance(item, CachedFindMyAccessory):
            # A reload sets the accessory up again from its config entry.
            await self.async_keep_checkpoints(entry.entry_id, item)

        return item
