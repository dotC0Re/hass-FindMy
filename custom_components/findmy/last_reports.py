"""The latest location report of each device, kept across restarts.

Purpose
-------
The coordinator holds the latest report of every device in memory only. That was no problem as
long as a fetch followed ten seconds after every start. Since the fetch limit of an account
survives restarts (budget.py), the first fetch after a restart can be up to 15 minutes away,
and until then every tracker and location sensor would be without a location. This module
remembers the latest report of each device in ``.storage/findmy.last_reports`` and hands it
back when the device is set up again.

What is stored
--------------
Only what the entities show: time, position, accuracy and status byte, per config entry id.
Neither the encrypted report nor a key is stored; FindMy.py can only serialise a decrypted
report together with the private key that decrypted it. The file is readable by the user Home
Assistant runs as only. It holds a position per device, as the recorder database does.

A restored report is a :class:`RestoredReport`, not a library object. It has the attributes of
``LocationReport`` that the entities read, so both can sit in the coordinator's data.

Limits
------
* A restored position is as old as its timestamp says; the entities show that time.
* A lost or broken file only means that the entities start without a location, as before.
* The record of a device is dropped when its config entry is removed, not when it is unloaded.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, TypedDict, cast, final

from homeassistant.helpers.storage import Store

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

    from findmy import LocationReport

    type KnownReport = LocationReport | RestoredReport

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "findmy.last_reports"
STORAGE_VERSION = 1
# A fetch brings the reports of all devices at once; one write shortly after is enough.
SAVE_DELAY = 5.0

_MAX_LATITUDE = 90.0
_MAX_LONGITUDE = 180.0
_MAX_BYTE = 0xFF


class _StoredReport(TypedDict):
    timestamp: str
    latitude: float
    longitude: float
    horizontal_accuracy: int
    status: int


class _StoredData(TypedDict):
    reports: dict[str, _StoredReport]


@final
@dataclass(frozen=True, slots=True)
class RestoredReport:
    """What the entities read from a location report, as plain data."""

    timestamp: datetime
    latitude: float
    longitude: float
    horizontal_accuracy: int
    status: int

    @classmethod
    def from_report(cls, report: KnownReport) -> RestoredReport:
        """Copy the shown values out of a report."""
        return cls(
            timestamp=report.timestamp,
            latitude=report.latitude,
            longitude=report.longitude,
            horizontal_accuracy=report.horizontal_accuracy,
            status=report.status,
        )

    def as_dict(self) -> _StoredReport:
        return {
            "timestamp": self.timestamp.isoformat(),
            "latitude": self.latitude,
            "longitude": self.longitude,
            "horizontal_accuracy": self.horizontal_accuracy,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, stored: object) -> RestoredReport:
        """Read a stored record; raises KeyError, TypeError or ValueError for a broken one."""
        if not isinstance(stored, dict):
            raise TypeError(type(stored).__name__)
        record = cast("dict[str, object]", stored)

        raw_timestamp = record["timestamp"]
        if not isinstance(raw_timestamp, str):
            msg = "timestamp"
            raise TypeError(msg)
        timestamp = datetime.fromisoformat(raw_timestamp)
        if timestamp.tzinfo is None:
            msg = "timestamp without a time zone"
            raise ValueError(msg)

        latitude = _number(record["latitude"], "latitude", _MAX_LATITUDE)
        longitude = _number(record["longitude"], "longitude", _MAX_LONGITUDE)
        return cls(
            timestamp=timestamp,
            latitude=latitude,
            longitude=longitude,
            horizontal_accuracy=_byte(record["horizontal_accuracy"], "horizontal_accuracy"),
            status=_byte(record["status"], "status"),
        )


def _number(value: object, name: str, limit: float) -> float:
    # bool is an int in Python; a file edited by hand must not turn `true` into a position.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(name)
    if not -limit <= value <= limit:
        raise ValueError(name)
    return float(value)


def _byte(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(name)
    if not 0 <= value <= _MAX_BYTE:
        raise ValueError(name)
    return value


@final
class LastReportStore:
    """The latest report per config entry id, in memory and in Home Assistant's storage.

    Everything here runs on Home Assistant's event loop. Call :meth:`async_load` once before
    the first device is set up.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        # private: the file holds positions, so only Home Assistant's user may read it.
        self._store: Store[_StoredData] = Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True)
        self._reports: dict[str, RestoredReport] = {}
        # A second caller waits for the first load instead of finding half the records.
        self._load_lock = asyncio.Lock()
        self._loaded = False
        self._changed_before_load = False

    async def async_load(self) -> None:
        """Load the stored reports once. A broken store starts empty, with a warning."""
        async with self._load_lock:
            if self._loaded:
                return
            try:
                await self._async_load_reports()
            finally:
                self._loaded = True
                if self._changed_before_load:
                    self._changed()

    async def _async_load_reports(self) -> None:
        try:
            stored = await self._store.async_load()
        except Exception:
            # Unknown version, unreadable file, anything else. Restoring is a convenience;
            # losing it must not stop the setup of the integration.
            _LOGGER.warning(
                "Could not load the last location reports from %s, starting without them",
                self._store.path,
                exc_info=True,
            )
            return
        if stored is None:
            # No file yet; a corrupt file has been moved aside and reported by the Store.
            return

        reports: object = cast("Mapping[str, object]", stored).get("reports")
        if not isinstance(reports, dict):
            _LOGGER.warning(
                "The last location reports in %s have an unexpected layout, starting without them",
                self._store.path,
            )
            return

        skipped = 0
        for entry_id, record in cast("dict[object, object]", reports).items():
            try:
                if not isinstance(entry_id, str):
                    raise TypeError  # noqa: TRY301
                restored = RestoredReport.from_dict(record)
            except KeyError, TypeError, ValueError:
                skipped += 1
                continue
            # Something remembered before the load is newer than what was on disk.
            known = self._reports.get(entry_id)
            if known is None or restored.timestamp > known.timestamp:
                self._reports[entry_id] = restored
        if skipped:
            _LOGGER.warning(
                "Skipped %i unreadable entries of the last location reports in %s",
                skipped,
                self._store.path,
            )

    def get(self, entry_id: str) -> RestoredReport | None:
        """The latest known report of the device of a config entry."""
        return self._reports.get(entry_id)

    def remember(self, entry_id: str, report: KnownReport) -> None:
        """Keep a report as the latest one of a device and save it soon."""
        restored = RestoredReport.from_report(report)
        if self._reports.get(entry_id) == restored:
            return
        self._reports[entry_id] = restored
        self._changed()

    def forget(self, entry_id: str) -> None:
        """Drop the report of a device whose config entry was removed."""
        if self._reports.pop(entry_id, None) is not None:
            self._changed()

    def _changed(self) -> None:
        if not self._loaded:
            # A pending save would be what Store.async_load returns instead of the file, and
            # the reports on disk would be lost. Saved once the load has merged them.
            self._changed_before_load = True
            return
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    def _data_to_save(self) -> _StoredData:
        return {
            "reports": {entry_id: report.as_dict() for entry_id, report in self._reports.items()}
        }
