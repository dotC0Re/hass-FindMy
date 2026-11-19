# pyright: reportImportCycles=false

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from findmy import AsyncAppleAccount, FixedRollingKeyPairAccessory, KeyPair

from .const import DOMAIN
from .coordinator import FindMyCoordinator, FindMyDevice
from .key_cache import CachedFindMyAccessory
from .worker import FindMyWorker

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event, HomeAssistant

    from .config_flow import EntryData
    from .local_bluetooth import LocalObservation

type StorageItem = AsyncAppleAccount | FindMyDevice

_LOGGER = logging.getLogger(__name__)


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
        self._coordinator: FindMyCoordinator = FindMyCoordinator(self._hass, self)
        self._worker: FindMyWorker | None = None

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
        """The thread that calls into the FindMy library are meant to run on (see worker.py).

        Started on first use, so that nothing that never signs in or fetches pays for a
        thread, and stopped together with Home Assistant.
        """
        if self._worker is None:
            worker = FindMyWorker()
            worker.start()

            async def _async_stop_worker(_event: Event) -> None:
                await worker.async_stop()

            _ = self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop_worker)
            self._worker = worker

        return self._worker

    @property
    def accounts(self) -> list[AsyncAppleAccount]:
        return [item for item in self._entries.values() if isinstance(item, AsyncAppleAccount)]

    def get_entry(self, entry: ConfigEntry[EntryData]) -> StorageItem:
        return self._entries[entry.entry_id]

    def get_entry_id(self, item: StorageItem) -> str | None:
        """Get the id of the config entry an item was stored for."""
        return next(
            (entry_id for entry_id, stored in self._entries.items() if stored is item),
            None,
        )

    async def add_entry(self, entry: ConfigEntry) -> StorageItem:
        data = cast("EntryData", entry.data)  # pyright: ignore[reportInvalidCast]

        if data["type"] == "account":
            loop = asyncio.get_running_loop()
            account = await loop.run_in_executor(
                None,
                AsyncAppleAccount.from_json,
                data["account_data"],
            )

            _LOGGER.debug(
                "Storing entry %s as account: %s",
                entry.entry_id,
                f"({account.account_name})",  # pyright: ignore[reportUnknownMemberType]
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
        item = self._entries.pop(entry.entry_id)

        if isinstance(item, AsyncAppleAccount):
            await item.close()

        return item
