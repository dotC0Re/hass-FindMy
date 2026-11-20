"""FindMy config entries set up through Home Assistant's real entity platforms.

``tests/README.md`` explains why pytest-homeassistant-custom-component is not used. What it would
provide for entity tests is rebuilt here, as small as the integration allows:

* :func:`async_prepare_platforms` loads what Home Assistant's bootstrap loads before any
  integration is set up: the entity, device, area, floor, label, category and issue
  registries, the restore state store and the translation cache. :func:`async_setup_findmy`
  then sets the integration up with ``async_setup_component``, which sets up every registered
  entry of it and forwards each one to the real ``device_tracker``, ``sensor`` and
  ``binary_sensor`` platforms. The entities end up in the registries and the state machine.
* Home Assistant's Bluetooth integration needs adapters and is not set up; it is only marked
  as loaded so that the dependency of the manifest is met. The integration uses nothing of it
  but its public functions, and :class:`FakeBluetooth` stands in for those.
* :class:`VirtualClock` lets time pass without waiting: it moves the clocks the integration
  reads and runs the timers of the event loop that fall due.
* Library accounts are mocks (``conftest.make_mock_account``); nothing talks to Apple.

This module imports nothing from ``homeassistant.components.bluetooth`` at import time, so that
``conftest.py`` can import it where that component does not import (see the
``requires_bluetooth`` marker); tests using it carry that marker.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import struct
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast, override

from bleak.backends.device import BLEDevice
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from findmy import KeyPairType, LocationReport
from habluetooth import BluetoothServiceInfoBleak
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity, restore_state, translation
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import event as event_helper
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from custom_components.findmy import account as account_module
from custom_components.findmy.const import DOMAIN

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from unittest.mock import MagicMock

    import pytest
    from findmy import FindMyAccessory, KeyPair
    from homeassistant.core import HomeAssistant, State

    from custom_components.findmy.storage import RuntimeStorage

# Apple counts report timestamps from 2001-01-01.
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
_BLUETOOTH = "homeassistant.components.bluetooth"
# Manufacturer data id and DULT service, as local_bluetooth.py matches them. Repeated here so
# that this module does not import the Bluetooth integration.
APPLE_COMPANY_ID = 0x004C
DULT_SERVICE_UUID = "0000fcb2-0000-1000-8000-00805f9b34fb"


# --- Time ------------------------------------------------------------------------------------


@dataclass
class VirtualClock:
    """The time the integration sees: the real time plus an offset that only tests move.

    Key derivation needs a time close to the real one (the accessories of the factories are
    aligned to it), so the clock starts there instead of at a fixed date.

    The monotonic clock is the exception. Linux and Windows count it from the boot, so its
    value depends on the machine the tests run on. It starts at ``uptime_at_start`` instead:
    Home Assistant started ten minutes after its host, as after a power cut.
    """

    offset: float = 0.0
    uptime_at_start: float = 600.0
    _started: float = field(default_factory=time.monotonic)

    def monotonic(self) -> float:
        return self.uptime_at_start + time.monotonic() - self._started + self.offset

    def timestamp(self) -> float:
        return time.time() + self.offset

    def utcnow(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self.offset)

    def datetime_class(self) -> type[datetime]:
        """A ``datetime`` whose ``now()`` follows this clock, to replace a module's import."""
        clock = self

        class _ShiftedDatetime(datetime):
            @classmethod
            @override
            def now(cls, tz: Any = None) -> datetime:  # mirrors datetime.now
                # A real datetime, so that comparisons and isinstance checks stay ordinary.
                return datetime.now(tz) + timedelta(seconds=clock.offset)

        return _ShiftedDatetime

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make Home Assistant's timers and the entity modules read this clock."""
        # Point-in-time listeners check the wall clock when they fire and rearm if early.
        monkeypatch.setattr(event_helper, "time_tracker_timestamp", self.timestamp)
        monkeypatch.setattr(event_helper, "time_tracker_utcnow", self.utcnow)

        device_tracker = importlib.import_module("custom_components.findmy.device_tracker")
        presence = importlib.import_module("custom_components.findmy.presence")
        shifted = self.datetime_class()
        monkeypatch.setattr(device_tracker, "datetime", shifted)
        monkeypatch.setattr(device_tracker, "monotonic", self.monotonic)
        monkeypatch.setattr(presence, "datetime", shifted)

    async def async_advance(self, hass: HomeAssistant, delta: timedelta) -> None:
        """Let ``delta`` pass: move the clock, then run every timer that falls due in it.

        Each timer runs at most once per call, as in pytest-homeassistant-custom-component's
        ``async_fire_time_changed``: an interval that falls due several times within ``delta``
        fires once. Tests advance in steps no longer than the intervals they rely on.
        """
        seconds = delta.total_seconds()
        self.offset += seconds
        loop = hass.loop
        deadline = loop.time() + seconds
        scheduled = cast("list[asyncio.TimerHandle]", loop._scheduled)  # pyright: ignore[reportAttributeAccessIssue]
        for handle in sorted(scheduled, key=lambda handle: handle.when()):
            if handle.cancelled() or handle.when() > deadline:
                continue
            handle._run()
            handle.cancel()
        await hass.async_block_till_done(wait_background_tasks=True)


# --- Bluetooth -------------------------------------------------------------------------------


def service_info(
    address: str,
    *,
    time: float,  # Bluetooth's monotonic time, not a timeout
    manufacturer_data: Mapping[int, bytes] | None = None,
    service_data: Mapping[str, bytes] | None = None,
    rssi: int = -60,
    source: str = "AA:BB:CC:DD:EE:FF",
    connectable: bool = False,
) -> BluetoothServiceInfoBleak:
    """An advertisement as Home Assistant's Bluetooth integration hands it to callbacks."""
    return BluetoothServiceInfoBleak(
        name=address,
        address=address,
        rssi=rssi,
        manufacturer_data=dict(manufacturer_data or {}),
        service_data=dict(service_data or {}),
        service_uuids=list(service_data or {}),
        source=source,
        device=BLEDevice(address, None, None),
        advertisement=None,
        connectable=connectable,
        time=time,
        tx_power=None,
    )


def separated_advertisement(key: KeyPair, status: int) -> dict[int, bytes]:
    """Apple manufacturer data of an accessory away from its owner: the whole key."""
    return {APPLE_COMPANY_ID: key.of_data(status=status, hint=0x42)}


def nearby_advertisement(key: KeyPair, status: int) -> dict[int, bytes]:
    """Apple manufacturer data of an accessory near its owner: status and two key bits."""
    return {APPLE_COMPANY_ID: bytes([0x12, 0x02, status, key.adv_key_bytes[0] >> 6])}


def dult_advertisement(*, near_owner: bool) -> dict[str, bytes]:
    """DULT service data; the accessory is identified by its address alone."""
    return {DULT_SERVICE_UUID: bytes([0x01, 0x01 if near_owner else 0x00, 0xAA])}


@dataclass
class _Registration:
    callback: Callable[[BluetoothServiceInfoBleak, Any], None]
    matcher: Mapping[str, Any]


@dataclass
class FakeBluetooth:
    """Home Assistant's Bluetooth API as the integration uses it, fed by tests.

    Like the real integration it keeps the latest advertisement of every address (the
    history), replays the history to a callback when it is registered, and passes an
    advertisement to a callback registered as not connectable whatever the advertisement is,
    to one registered as connectable only if the advertisement is connectable.
    """

    clock: VirtualClock
    registrations: list[_Registration] = field(default_factory=list)
    history: dict[str, BluetoothServiceInfoBleak] = field(default_factory=dict)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bluetooth = importlib.import_module(_BLUETOOTH)
        monkeypatch.setattr(bluetooth, "async_register_callback", self._register)
        monkeypatch.setattr(bluetooth, "async_discovered_service_info", self._discovered)
        monkeypatch.setattr(bluetooth, "async_last_service_info", self._last)
        monkeypatch.setattr(bluetooth, "MONOTONIC_TIME", self.clock.monotonic)

    @staticmethod
    def _matches(registration: _Registration, info: BluetoothServiceInfoBleak) -> bool:
        matcher = registration.matcher
        if matcher.get("connectable", True) and not info.connectable:
            return False
        manufacturer_id = matcher.get("manufacturer_id")
        if manufacturer_id is not None and manufacturer_id not in info.manufacturer_data:
            return False
        uuid = matcher.get("service_data_uuid")
        return uuid is None or uuid in info.service_data

    def _register(
        self,
        _hass: HomeAssistant,
        callback: Callable[[BluetoothServiceInfoBleak, Any], None],
        matcher: Mapping[str, Any] | None,
        _mode: object,
        **_kwargs: object,
    ) -> Callable[[], None]:
        registration = _Registration(callback, matcher or {})
        self.registrations.append(registration)
        change = importlib.import_module(_BLUETOOTH).BluetoothChange.ADVERTISEMENT
        for info in sorted(self.history.values(), key=lambda info: info.time):
            if self._matches(registration, info):
                callback(info, change)

        def unregister() -> None:
            self.registrations.remove(registration)

        return unregister

    def _discovered(
        self, _hass: HomeAssistant, connectable: bool = True
    ) -> list[BluetoothServiceInfoBleak]:
        return [info for info in self.history.values() if info.connectable or not connectable]

    def _last(
        self, _hass: HomeAssistant, address: str, connectable: bool = True
    ) -> BluetoothServiceInfoBleak | None:
        info = self.history.get(address.upper())
        if info is None or (connectable and not info.connectable):
            return None
        return info

    def advertise(
        self,
        address: str,
        *,
        manufacturer_data: Mapping[int, bytes] | None = None,
        service_data: Mapping[str, bytes] | None = None,
        rssi: int = -60,
        source: str = "AA:BB:CC:DD:EE:FF",
        notify: bool = True,
    ) -> BluetoothServiceInfoBleak:
        """Receive an advertisement now.

        ``notify=False`` is the same payload heard again: Home Assistant only updates the
        history then and does not run the callbacks.
        """
        info = service_info(
            address.upper(),
            time=self.clock.monotonic(),
            manufacturer_data=manufacturer_data,
            service_data=service_data,
            rssi=rssi,
            source=source,
        )
        self.history[info.address] = info
        if notify:
            change = importlib.import_module(_BLUETOOTH).BluetoothChange.ADVERTISEMENT
            for registration in list(self.registrations):
                if self._matches(registration, info):
                    registration.callback(info, change)
        return info


# --- Location reports ------------------------------------------------------------------------


def make_location_report(
    key: KeyPair,
    *,
    timestamp: datetime,
    latitude: float,
    longitude: float,
    horizontal_accuracy: int,
    status: int,
) -> LocationReport:
    """A report as Apple sends it, encrypted to ``key`` and decrypted the way FindMy.py does.

    Built with the same scheme the library decrypts: an ephemeral P-224 key, ECDH with the
    accessory's key, SHA-256 as KDF and AES-GCM.
    """
    ephemeral = ec.generate_private_key(ec.SECP224R1())
    ephemeral_public = ephemeral.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    private_value = int.from_bytes(key.private_key_bytes, "big")
    accessory_public = ec.derive_private_key(private_value, ec.SECP224R1()).public_key()
    shared = ephemeral.exchange(ec.ECDH(), accessory_public)
    symmetric = hashlib.sha256(shared + b"\x00\x00\x00\x01" + ephemeral_public).digest()
    plain = struct.pack(
        ">iiBB",
        round(latitude * 10_000_000),
        round(longitude * 10_000_000),
        horizontal_accuracy,
        status,
    )
    encryptor = Cipher(algorithms.AES(symmetric[:16]), modes.GCM(symmetric[16:])).encryptor()
    ciphertext = encryptor.update(plain) + encryptor.finalize()
    seconds = int((timestamp - _APPLE_EPOCH).total_seconds())
    # The 89-byte format of current iOS versions: timestamp, a byte, the confidence, then the
    # ephemeral key, the encrypted data and the tag.
    payload = seconds.to_bytes(4, "big") + b"\x00\x03" + ephemeral_public + ciphertext
    payload += encryptor.tag
    report = LocationReport(payload, key.hashed_adv_key_bytes)
    report.decrypt(key)
    return report


def answer_with(reports: Mapping[Any, LocationReport]) -> Callable[..., Any]:
    """A ``fetch_location`` that answers each searched source with the report of its device.

    The coordinator searches an accessory through a window cut from it (``.accessory``) and
    static keys as they are; ``reports`` is keyed by the device either way.
    """

    async def fetch(sources: list[object]) -> dict[object, LocationReport | None]:
        return {source: reports.get(getattr(source, "accessory", source)) for source in sources}

    return fetch


# --- Setup -----------------------------------------------------------------------------------


@dataclass
class LibraryAccounts:
    """The mocked library accounts the account entries restore to, by Apple ID.

    ``factory`` is ``conftest.make_mock_account``; passed in, as conftest imports this module.
    """

    factory: Callable[..., MagicMock]
    by_email: dict[str, MagicMock] = field(default_factory=dict)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Restore account entries to mocks instead of FindMy.py accounts.

        A restored library account would not talk to Apple until it fetches, but a fetch is
        exactly what the coordinator schedules after setup.
        """

        def from_json(data: Mapping[str, Any], **_options: object) -> MagicMock:
            # Options such as the Anisette library path do not matter to a mock.
            email = cast("str", data["account"]["username"])
            account = self.factory(email=email, reports={})
            self.by_email[email] = account
            return account

        monkeypatch.setattr(
            account_module, "AsyncAppleAccount", SimpleNamespace(from_json=from_json)
        )


async def async_prepare_platforms(hass: HomeAssistant) -> None:
    """Load what Home Assistant's bootstrap loads before it sets up integrations.

    Bluetooth and file_upload are only marked as set up: the manifest depends on them, and
    the integration replaces nothing of them that FakeBluetooth does not cover.
    """
    entity.async_setup(hass)
    translation.async_setup(hass)
    dr.async_setup(hass)
    _ = await asyncio.gather(
        ar.async_load(hass),
        cr.async_load(hass),
        dr.async_load(hass),
        er.async_load(hass),
        fr.async_load(hass),
        ir.async_load(hass),
        lr.async_load(hass),
        restore_state.async_load(hass),
    )
    hass.config.components.add("bluetooth")
    hass.config.components.add("file_upload")


def primary_key(accessory: FindMyAccessory, index: int) -> KeyPair:
    """The key an accessory advertises and is located with at ``index``."""
    return next(key for key in accessory.keys_at(index) if key.key_type == KeyPairType.PRIMARY)


def get_state(hass: HomeAssistant, entity_id: str) -> State:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


async def async_setup_findmy(hass: HomeAssistant) -> RuntimeStorage:
    """Set up the integration with every entry registered so far, entities included."""
    from custom_components.findmy.storage import RuntimeStorage  # noqa: PLC0415

    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done(wait_background_tasks=True)
    return RuntimeStorage.get(hass)
