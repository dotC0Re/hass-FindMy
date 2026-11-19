"""Local Bluetooth matching of rolling-key accessories, through the real entity platforms.

Advertisements are built from the real keys of a random accessory and fed into the callbacks
the device tracker registers (ha_platforms.FakeBluetooth). The accessory of the factory is
aligned to index 192 at the current time; it advertises the primary key of an index.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN

from custom_components.findmy import device_tracker
from custom_components.findmy.const import CONF_AWAY_TIMEOUT
from custom_components.findmy.key_cache import CachedFindMyAccessory
from custom_components.findmy.local_bluetooth import build_candidate_key_lookup

from .conftest import make_config_entry
from .ha_platforms import (
    FakeBluetooth,
    VirtualClock,
    async_setup_findmy,
    dult_advertisement,
    get_state,
    nearby_advertisement,
    primary_key,
    separated_advertisement,
)

if TYPE_CHECKING:
    from findmy import FindMyAccessory
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from custom_components.findmy.storage import RuntimeStorage

    from .conftest import AccessoryFactory

pytestmark = pytest.mark.requires_bluetooth

INDEX = 192
TRACKER = "device_tracker.findmy_test_tag"
PRESENCE = "binary_sensor.test_tag_bluetooth_presence"
SIGNAL = "sensor.test_tag_signal_strength"
# Battery bits 0b11 ("critical"), counter 9.
CRITICAL = 0b1100_1001
LOCAL_ATTRIBUTES = (
    "mac_address",
    "local_state",
    "local_rssi",
    "local_source",
    "local_battery",
    "alignment_index",
)


class _Counter:
    """Counts the candidate builds and the changes of the accessory's config entry.

    Only updates that changed the entry count: those are what Home Assistant writes to
    ``core.config_entries``.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, hass: HomeAssistant) -> None:
        self.builds: int = 0
        self.entry_writes: list[dict[str, Any]] = []
        build = build_candidate_key_lookup
        update = hass.config_entries.async_update_entry

        def counting_build(*args: Any) -> Any:
            self.builds += 1
            return build(*args)

        def counting_update(entry: ConfigEntry, **kwargs: Any) -> bool:
            changed = update(entry, **kwargs)
            if changed and "data" in kwargs:
                self.entry_writes.append(kwargs["data"]["data"])
            return changed

        monkeypatch.setattr(device_tracker, "build_candidate_key_lookup", counting_build)
        monkeypatch.setattr(hass.config_entries, "async_update_entry", counting_update)


@pytest.fixture
def accessory(accessory_factory: AccessoryFactory) -> FindMyAccessory:
    return accessory_factory(name="Test Tag")


@pytest.fixture
async def counter(platform_hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> _Counter:
    return _Counter(monkeypatch, platform_hass)


@pytest.fixture
async def storage(
    platform_hass: HomeAssistant, accessory: FindMyAccessory, counter: _Counter
) -> RuntimeStorage:
    make_config_entry(
        platform_hass,
        {"type": "device_rolling_derived", "data": accessory.to_json()},
        entry_id="tag-entry",
    )
    return await async_setup_findmy(platform_hass)


def _device(hass: HomeAssistant, storage: RuntimeStorage) -> CachedFindMyAccessory:
    """The accessory as the storage keeps it; its alignment is what matching moves."""
    device = storage.get_entry(hass.config_entries.async_entries("findmy")[0])
    assert isinstance(device, CachedFindMyAccessory)
    return device


def _local(hass: HomeAssistant) -> dict[str, Any]:
    attributes = get_state(hass, TRACKER).attributes
    return {key: attributes[key] for key in LOCAL_ATTRIBUTES}


async def test_setup_prepares_candidates_and_listens(
    platform_hass: HomeAssistant,
    storage: RuntimeStorage,
    fake_bluetooth: FakeBluetooth,
    counter: _Counter,
) -> None:
    # Apple manufacturer data, connectable or not, and DULT service data.
    matchers = [dict(registration.matcher) for registration in fake_bluetooth.registrations]
    assert matchers == [
        {"connectable": False, "manufacturer_id": 0x004C},
        {"connectable": True, "manufacturer_id": 0x004C},
        {"connectable": False, "service_data_uuid": "0000fcb2-0000-1000-8000-00805f9b34fb"},
    ]
    assert counter.builds == 1
    # A primary key per 15 minutes from 36 hours back to 12 ahead, and the secondary keys.
    assert get_state(platform_hass, TRACKER).attributes["local_key_candidates"] > 4 * 48
    assert get_state(platform_hass, PRESENCE).state == STATE_OFF
    assert get_state(platform_hass, SIGNAL).state == STATE_UNKNOWN


@pytest.mark.parametrize(
    ("build", "state"),
    [(separated_advertisement, "separated"), (nearby_advertisement, "nearby")],
    ids=["separated", "nearby"],
)
@pytest.mark.usefixtures("storage")
async def test_an_offline_finding_advertisement_updates_every_local_entity(
    platform_hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    accessory: FindMyAccessory,
    build: Any,
    state: str,
) -> None:
    hass = platform_hass
    key = primary_key(accessory, INDEX)

    fake_bluetooth.advertise(
        key.mac_address, manufacturer_data=build(key, CRITICAL), rssi=-58, source="proxy-1"
    )
    await hass.async_block_till_done()

    assert _local(hass) == {
        "mac_address": key.mac_address,
        "local_state": state,
        "local_rssi": -58,
        "local_source": "proxy-1",
        "local_battery": "Very Low",
        "alignment_index": INDEX,
    }
    assert get_state(hass, TRACKER).attributes["local_detected_at"] is not None
    presence = get_state(hass, PRESENCE)
    assert presence.state == STATE_ON
    assert presence.attributes["mac_address"] == key.mac_address
    assert presence.attributes["local_state"] == state
    assert presence.attributes["battery"] == "Very Low"
    assert presence.attributes["source"] == "proxy-1"
    assert get_state(hass, SIGNAL).state == "-58"
    assert get_state(hass, "sensor.test_tag_battery_level").state == "critical"
    assert get_state(hass, "sensor.test_tag_battery").state == "15"
    assert get_state(hass, "binary_sensor.test_tag_battery_low").state == STATE_ON
    # No location report: the location entities have nothing to show.
    assert get_state(hass, "sensor.test_tag_latitude").state == STATE_UNKNOWN
    assert get_state(hass, TRACKER).state == STATE_UNKNOWN


async def test_a_dult_advertisement_is_matched_by_its_address(
    platform_hass: HomeAssistant,
    storage: RuntimeStorage,
    fake_bluetooth: FakeBluetooth,
    accessory: FindMyAccessory,
) -> None:
    """Second-generation AirTags near their owner; no status byte, so no battery state."""
    hass = platform_hass
    key = primary_key(accessory, INDEX + 1)

    fake_bluetooth.advertise(
        key.mac_address.lower(), service_data=dult_advertisement(near_owner=True), rssi=-70
    )
    await hass.async_block_till_done()

    assert _local(hass) == {
        "mac_address": key.mac_address,
        "local_state": "near_owner",
        "local_rssi": -70,
        "local_source": "AA:BB:CC:DD:EE:FF",
        "local_battery": None,
        "alignment_index": INDEX + 1,
    }
    assert get_state(hass, PRESENCE).state == STATE_ON
    assert get_state(hass, PRESENCE).attributes["battery"] is None
    assert get_state(hass, SIGNAL).state == "-70"
    assert get_state(hass, "sensor.test_tag_battery").state == STATE_UNKNOWN
    assert storage.local_status == {}


async def test_the_battery_of_a_dult_advertisement_keeps_the_last_known_one(
    platform_hass: HomeAssistant,
    storage: RuntimeStorage,
    fake_bluetooth: FakeBluetooth,
    accessory: FindMyAccessory,
) -> None:
    hass = platform_hass
    key = primary_key(accessory, INDEX)
    fake_bluetooth.advertise(
        key.mac_address, manufacturer_data=separated_advertisement(key, CRITICAL)
    )
    later = primary_key(accessory, INDEX + 1)

    fake_bluetooth.advertise(later.mac_address, service_data=dult_advertisement(near_owner=True))
    await hass.async_block_till_done()

    assert _local(hass)["local_state"] == "near_owner"
    assert _local(hass)["local_battery"] is None
    assert get_state(hass, PRESENCE).attributes["battery"] == "Very Low"
    assert get_state(hass, "sensor.test_tag_battery").state == "15"


async def test_a_foreign_accessory_changes_nothing(
    platform_hass: HomeAssistant,
    storage: RuntimeStorage,
    fake_bluetooth: FakeBluetooth,
    accessory_factory: AccessoryFactory,
) -> None:
    hass = platform_hass
    before = {entity_id: get_state(hass, entity_id) for entity_id in (TRACKER, PRESENCE, SIGNAL)}
    other = primary_key(accessory_factory(name="Someone else's"), INDEX)

    fake_bluetooth.advertise(
        other.mac_address, manufacturer_data=separated_advertisement(other, CRITICAL)
    )
    fake_bluetooth.advertise(
        other.mac_address, manufacturer_data=nearby_advertisement(other, CRITICAL)
    )
    fake_bluetooth.advertise(other.mac_address, service_data=dult_advertisement(near_owner=False))
    await hass.async_block_till_done()

    assert {entity_id: get_state(hass, entity_id) for entity_id in before} == before
    assert storage.local_observations == {}
    assert storage.local_status == {}


async def test_realignment_moves_only_forward_and_refreshes_only_then(  # noqa: PLR0917
    platform_hass: HomeAssistant,
    storage: RuntimeStorage,
    fake_bluetooth: FakeBluetooth,
    virtual_clock: VirtualClock,
    accessory: FindMyAccessory,
    counter: _Counter,
) -> None:
    """Which observations realign the accessory, refresh its candidates and save its entry.

    A refresh replays the Bluetooth history, which matches the same advertisements again. An
    observation behind the alignment (location reports may have aligned further ahead) must
    therefore not refresh: the replay would find it again and refresh in a loop.
    """
    hass = platform_hass
    device = _device(hass, storage)
    # The refresh after setup: a coordinator update writes the entry too. Without accounts no
    # other refresh follows.
    await virtual_clock.async_advance(hass, timedelta(seconds=10))
    assert counter.entry_writes == []

    def advertise(index: int) -> None:
        key = primary_key(accessory, index)
        fake_bluetooth.advertise(
            key.mac_address, manufacturer_data=separated_advertisement(key, CRITICAL)
        )

    # The same index: the alignment keeps its index, with the new time. It is saved, as the
    # first one of this run.
    advertise(INDEX)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert device._alignment_index == INDEX
    assert counter.builds == 1
    assert [data["alignment_index"] for data in counter.entry_writes] == [INDEX]

    # A lower index: matched and shown, but neither realigned, refreshed nor saved.
    advertise(INDEX - 2)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (
        get_state(hass, TRACKER).attributes["mac_address"]
        == primary_key(accessory, INDEX - 2).mac_address
    )
    assert device._alignment_index == INDEX
    assert counter.builds == 1
    assert len(counter.entry_writes) == 1

    # A higher index: realigned and refreshed at once. Saved only an hour after the last save.
    advertise(INDEX + 2)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert device._alignment_index == INDEX + 2
    assert get_state(hass, TRACKER).attributes["alignment_index"] == INDEX + 2
    assert counter.builds == 2
    assert len(counter.entry_writes) == 1

    # Another one within the cooldown waits for its end; realignments in between collapse.
    advertise(INDEX + 3)
    advertise(INDEX + 4)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert device._alignment_index == INDEX + 4
    assert counter.builds == 2
    await virtual_clock.async_advance(hass, timedelta(seconds=60))
    assert counter.builds == 3
    # The refreshes replayed the history; nothing in it moved the alignment again.
    await virtual_clock.async_advance(hass, timedelta(seconds=60))
    assert counter.builds == 3

    # An hour after the first save, the next observation saves the alignment it reached: the
    # one the 15-minute refresh replays from the history, or the next advertisement.
    await virtual_clock.async_advance(hass, timedelta(minutes=59))
    advertise(INDEX + 4)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [data["alignment_index"] for data in counter.entry_writes] == [INDEX, INDEX + 4]
    entry = hass.config_entries.async_get_entry("tag-entry")
    assert entry is not None
    assert entry.data["data"]["alignment_index"] == INDEX + 4


async def test_a_secondary_key_is_matched_but_never_aligns(
    platform_hass: HomeAssistant,
    storage: RuntimeStorage,
    fake_bluetooth: FakeBluetooth,
    accessory: FindMyAccessory,
    counter: _Counter,
) -> None:
    """A separated accessory keeps its secondary key for a day; its index is no alignment."""
    from findmy import KeyPairType  # noqa: PLC0415

    hass = platform_hass
    device = _device(hass, storage)
    key = next(key for key in accessory.keys_at(INDEX + 8) if key.key_type == KeyPairType.SECONDARY)

    fake_bluetooth.advertise(
        key.mac_address, manufacturer_data=separated_advertisement(key, CRITICAL)
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _local(hass)["mac_address"] == key.mac_address
    assert device._alignment_index == INDEX
    assert counter.builds == 1
    assert counter.entry_writes == []


async def test_unload_stops_bluetooth_and_timers(  # noqa: PLR0917
    platform_hass: HomeAssistant,
    storage: RuntimeStorage,
    fake_bluetooth: FakeBluetooth,
    virtual_clock: VirtualClock,
    accessory: FindMyAccessory,
    counter: _Counter,
) -> None:
    hass = platform_hass
    key = primary_key(accessory, INDEX + 1)
    fake_bluetooth.advertise(
        key.mac_address, manufacturer_data=separated_advertisement(key, CRITICAL)
    )
    # A realignment within the cooldown leaves a refresh waiting for its end.
    later = primary_key(accessory, INDEX + 2)
    fake_bluetooth.advertise(
        later.mac_address, manufacturer_data=separated_advertisement(later, CRITICAL)
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    builds = counter.builds

    assert await hass.config_entries.async_unload("tag-entry")
    await hass.async_block_till_done()

    assert fake_bluetooth.registrations == []
    states = {state.entity_id: state for state in hass.states.async_all()}
    # The cooldown, the presence refresh and the 15-minute candidate refresh would all have
    # fired by now; the entities would write their states.
    for _ in range(31):
        await virtual_clock.async_advance(hass, timedelta(seconds=30))
    fake_bluetooth.advertise(later.mac_address, manufacturer_data=separated_advertisement(later, 0))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert counter.builds == builds
    assert {state.entity_id: state for state in hass.states.async_all()} == states


async def test_away_timeout_option_is_used(
    platform_hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    virtual_clock: VirtualClock,
    accessory: FindMyAccessory,
) -> None:
    """Covered in detail by test_presence.py; here only that the option reaches the entity."""
    hass = platform_hass
    entry = make_config_entry(hass, {"type": "device_rolling_derived", "data": accessory.to_json()})
    hass.config_entries.async_update_entry(entry, options={CONF_AWAY_TIMEOUT: 3})
    await async_setup_findmy(hass)
    key = primary_key(accessory, INDEX)
    fake_bluetooth.advertise(
        key.mac_address, manufacturer_data=separated_advertisement(key, CRITICAL)
    )
    await hass.async_block_till_done()
    assert get_state(hass, PRESENCE).attributes["away_timeout"] == 3

    for _ in range(7):
        await virtual_clock.async_advance(hass, timedelta(seconds=30))

    assert get_state(hass, PRESENCE).state == STATE_OFF
