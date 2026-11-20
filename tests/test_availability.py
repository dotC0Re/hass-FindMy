"""The entities of a device never become unavailable because of what a fetch did.

``CoordinatorEntity`` ties availability to the success of the latest refresh. A fetch that fails
says something about the Apple account, which the account sensors show; it takes nothing away
from what is known about a device, and for a device nothing is known about the state is
``unknown``. Which refresh came last does not matter either: one the fetch budget turns away
counts as a success. ``unavailable`` stays what Home Assistant shows for an entry that is not
loaded.

Entries are set up through the real entity platforms (ha_platforms.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest
from findmy import UnauthorizedError
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.findmy.storage import RuntimeStorage

from .conftest import make_account_state, make_config_entry
from .ha_platforms import (
    FakeBluetooth,
    LibraryAccounts,
    answer_with,
    async_setup_findmy,
    dult_advertisement,
    get_state,
    make_location_report,
    primary_key,
    separated_advertisement,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .conftest import AccessoryFactory, FetchClock, KeyPairFactory

pytestmark = pytest.mark.requires_bluetooth

EMAIL = "jane@example.com"
INDEX = 2 * 96
# Battery bits 0b10 ("low"), counter 5.
LOW_BATTERY = 0b1000_0101
# The enabled sensors of a device: the voltage and the status counter are opt-in.
SENSORS = ("latitude", "longitude", "position", "battery_level", "battery")
LOCAL_ATTRIBUTES = (
    "mac_address",
    "local_state",
    "local_rssi",
    "local_source",
    "local_battery",
    "local_detected_at",
    "alignment_index",
)


def _account_entry(hass: HomeAssistant) -> None:
    make_config_entry(hass, {"type": "account", "account_data": make_account_state(email=EMAIL)})


def _entity_ids(slug: str, *, accessory: bool) -> list[str]:
    ids = [
        f"device_tracker.findmy_{slug}",
        *(f"sensor.{slug}_{suffix}" for suffix in SENSORS),
        f"binary_sensor.{slug}_battery_low",
    ]
    if accessory:
        ids += [f"binary_sensor.{slug}_bluetooth_presence", f"sensor.{slug}_signal_strength"]
    return ids


def _snapshot(
    hass: HomeAssistant, slug: str, *, accessory: bool
) -> dict[str, tuple[str, dict[str, Any]]]:
    """State and attributes of every entity of a device."""
    return {
        entity_id: (state.state, dict(state.attributes))
        for entity_id in _entity_ids(slug, accessory=accessory)
        if (state := get_state(hass, entity_id))
    }


def _states(hass: HomeAssistant, slug: str, *, accessory: bool) -> dict[str, str]:
    return {
        entity_id: state
        for entity_id, (state, _) in _snapshot(hass, slug, accessory=accessory).items()
    }


def _fail_fetches(library_accounts: LibraryAccounts) -> None:
    library_accounts.by_email[EMAIL].fetch_location.side_effect = aiohttp.ClientConnectionError()


async def _refresh(hass: HomeAssistant, storage: RuntimeStorage) -> None:
    await storage.coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_device_nothing_is_known_about_is_unknown_whatever_the_fetches_did(
    platform_hass: HomeAssistant,
    library_accounts: LibraryAccounts,
    fetch_clock: FetchClock,
    keypair_factory: KeyPairFactory,
    accessory_factory: AccessoryFactory,
) -> None:
    hass = platform_hass
    keys = keypair_factory(name="Keys")
    tag = accessory_factory(name="Tag")
    make_config_entry(hass, {"type": "device_static", "data": keys.to_json()})
    make_config_entry(hass, {"type": "device_rolling_derived", "data": tag.to_json()})
    _account_entry(hass)
    storage = await async_setup_findmy(hass)
    coordinator = storage.coordinator
    fetches = library_accounts.by_email[EMAIL].fetch_location

    def assert_nothing_known() -> None:
        keys_states = _states(hass, "keys", accessory=False)
        assert set(keys_states.values()) == {STATE_UNKNOWN}
        tag_states = _states(hass, "tag", accessory=True)
        # Presence is a state of its own: not heard, so not here.
        assert tag_states.pop("binary_sensor.tag_bluetooth_presence") == STATE_OFF
        assert set(tag_states.values()) == {STATE_UNKNOWN}

    assert_nothing_known()

    _fail_fetches(library_accounts)
    await _refresh(hass, storage)
    assert coordinator.last_update_success is False
    assert_nothing_known()

    # A refresh the fetch budget turns away counts as a success; the states do not follow it.
    await _refresh(hass, storage)
    assert fetches.await_count == 1
    assert coordinator.last_update_success is True
    assert_nothing_known()

    fetch_clock.advance()
    await _refresh(hass, storage)
    assert fetches.await_count == 2
    assert coordinator.last_update_success is False
    assert_nothing_known()

    # The account is refused: nothing to fetch with any more, which is no failure either.
    fetches.side_effect = UnauthorizedError("expired")
    fetch_clock.advance()
    await _refresh(hass, storage)
    assert coordinator.last_update_success is False
    assert_nothing_known()
    await _refresh(hass, storage)
    assert fetches.await_count == 3
    assert coordinator.last_update_success is True
    assert_nothing_known()


async def test_reports_and_local_sightings_survive_failed_fetches_with_all_attributes(  # noqa: PLR0917
    platform_hass: HomeAssistant,
    library_accounts: LibraryAccounts,
    fake_bluetooth: FakeBluetooth,
    fetch_clock: FetchClock,
    keypair_factory: KeyPairFactory,
    accessory_factory: AccessoryFactory,
) -> None:
    hass = platform_hass
    located = keypair_factory(name="Located")
    heard = accessory_factory(name="Heard")
    make_config_entry(hass, {"type": "device_static", "data": located.to_json()})
    make_config_entry(hass, {"type": "device_rolling_derived", "data": heard.to_json()})
    _account_entry(hass)
    storage = await async_setup_findmy(hass)
    located_device = storage.get_entry(hass.config_entries.async_entries("findmy")[0])
    report = make_location_report(
        located,
        timestamp=datetime.now(UTC),
        latitude=52.5,
        longitude=13.4,
        horizontal_accuracy=20,
        status=0x40,
    )
    library_accounts.by_email[EMAIL].fetch_location.side_effect = answer_with(
        {located_device: report}
    )
    await _refresh(hass, storage)
    key = primary_key(heard, INDEX)
    fake_bluetooth.advertise(
        key.mac_address,
        manufacturer_data=separated_advertisement(key, LOW_BATTERY),
        rssi=-58,
        source="proxy-1",
    )
    await hass.async_block_till_done()
    before = {
        "located": _snapshot(hass, "located", accessory=False),
        "heard": _snapshot(hass, "heard", accessory=True),
    }
    assert before["located"]["device_tracker.findmy_located"][0] == "not_home"
    assert before["located"]["sensor.located_latitude"][0] == "52.5"
    assert before["heard"]["sensor.heard_battery_level"][0] == "low"
    assert before["heard"]["binary_sensor.heard_bluetooth_presence"][0] == STATE_ON
    assert before["heard"]["device_tracker.findmy_heard"][1]["local_source"] == "proxy-1"

    _fail_fetches(library_accounts)
    # A failed fetch, one the budget turns away, and another failed one.
    for advance in (True, False, True):
        if advance:
            fetch_clock.advance()
        await _refresh(hass, storage)
        assert {
            "located": _snapshot(hass, "located", accessory=False),
            "heard": _snapshot(hass, "heard", accessory=True),
        } == before


async def test_a_reloaded_entry_shows_what_was_known_before_while_fetches_fail(
    platform_hass: HomeAssistant,
    library_accounts: LibraryAccounts,
    fake_bluetooth: FakeBluetooth,
    fetch_clock: FetchClock,
    accessory_factory: AccessoryFactory,
) -> None:
    """No new advertisement arrives: the Bluetooth history no longer holds the old one."""
    hass = platform_hass
    tag = accessory_factory(name="Tag")
    dult = accessory_factory(name="Dult")
    tag_entry = make_config_entry(hass, {"type": "device_rolling_derived", "data": tag.to_json()})
    dult_entry = make_config_entry(hass, {"type": "device_rolling_derived", "data": dult.to_json()})
    _account_entry(hass)
    storage = await async_setup_findmy(hass)
    tag_device = storage.get_entry(tag_entry)
    tag_key = primary_key(tag, INDEX)
    report = make_location_report(
        tag_key,
        timestamp=datetime.now(UTC),
        latitude=52.5,
        longitude=13.4,
        horizontal_accuracy=20,
        status=0x40,
    )
    library_accounts.by_email[EMAIL].fetch_location.side_effect = answer_with({tag_device: report})
    await _refresh(hass, storage)
    fake_bluetooth.advertise(
        tag_key.mac_address,
        manufacturer_data=separated_advertisement(tag_key, LOW_BATTERY),
        rssi=-58,
        source="proxy-1",
    )
    fake_bluetooth.advertise(
        primary_key(dult, INDEX).mac_address,
        service_data=dult_advertisement(near_owner=True),
        rssi=-70,
        source="proxy-2",
    )
    await hass.async_block_till_done()
    before = {slug: _snapshot(hass, slug, accessory=True) for slug in ("tag", "dult")}
    # The local sighting of the tag is newer than the report, so the battery is the local one.
    assert before["tag"]["sensor.tag_battery_level"][0] == "low"
    tag_attributes = before["tag"]["device_tracker.findmy_tag"][1]
    assert tag_attributes["local_battery"] == "Low"
    assert tag_attributes["mac_address"] == tag_key.mac_address
    assert before["dult"]["device_tracker.findmy_dult"][1]["local_state"] == "near_owner"

    _fail_fetches(library_accounts)
    fetch_clock.advance()
    await _refresh(hass, storage)
    # Nothing is heard again: the history has forgotten both advertisements.
    fake_bluetooth.history.clear()
    for entry in (tag_entry, dult_entry):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    def assert_as_before() -> None:
        for slug in ("tag", "dult"):
            after = _snapshot(hass, slug, accessory=True)
            assert STATE_UNAVAILABLE not in {state for state, _ in after.values()}
            assert {id_: state for id_, (state, _) in after.items()} == {
                id_: state for id_, (state, _) in before[slug].items()
            }
            tracker = f"device_tracker.findmy_{slug}"
            for name in (*LOCAL_ATTRIBUTES, "detected_at", "status"):
                assert after[tracker][1][name] == before[slug][tracker][1][name], (slug, name)
            presence = f"binary_sensor.{slug}_bluetooth_presence"
            for name in ("mac_address", "local_state", "battery", "source", "rssi"):
                assert after[presence][1][name] == before[slug][presence][1][name], (slug, name)

    assert_as_before()
    assert get_state(hass, "sensor.tag_signal_strength").state == "-58"

    # Further fetches that fail change nothing either.
    fetch_clock.advance()
    await _refresh(hass, storage)
    assert storage.coordinator.last_update_success is False
    assert_as_before()


async def test_a_reloaded_tracker_catches_up_on_the_alignment_of_the_stored_match(
    platform_hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    accessory_factory: AccessoryFactory,
) -> None:
    """The entry a reloaded device is built from trails the alignment by up to an hour."""
    hass = platform_hass
    tag = accessory_factory(name="Tag", alignment_index=INDEX - 8)
    entry = make_config_entry(hass, {"type": "device_rolling_derived", "data": tag.to_json()})
    await async_setup_findmy(hass)
    key = primary_key(tag, INDEX)
    fake_bluetooth.advertise(
        key.mac_address, manufacturer_data=separated_advertisement(key, LOW_BATTERY)
    )
    await hass.async_block_till_done()
    # The next rotation: the alignment moves on, but the entry is saved at most once an hour.
    next_key = primary_key(tag, INDEX + 1)
    fake_bluetooth.advertise(
        next_key.mac_address, manufacturer_data=separated_advertisement(next_key, LOW_BATTERY)
    )
    await hass.async_block_till_done()
    assert get_state(hass, "device_tracker.findmy_tag").attributes["alignment_index"] == INDEX + 1
    assert entry.data["data"]["alignment_index"] == INDEX
    fake_bluetooth.history.clear()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    tracker = get_state(hass, "device_tracker.findmy_tag")
    assert tracker.attributes["mac_address"] == next_key.mac_address
    # The device was built from the entry, which trails; the stored match brings it up to date.
    assert tracker.attributes["alignment_index"] == INDEX + 1


async def test_without_an_account_everything_behaves_as_before(
    platform_hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    keypair_factory: KeyPairFactory,
    accessory_factory: AccessoryFactory,
) -> None:
    hass = platform_hass
    keys = keypair_factory(name="Keys")
    tag = accessory_factory(name="Tag")
    make_config_entry(hass, {"type": "device_static", "data": keys.to_json()})
    entry = make_config_entry(hass, {"type": "device_rolling_derived", "data": tag.to_json()})
    storage = await async_setup_findmy(hass)

    assert set(_states(hass, "keys", accessory=False).values()) == {STATE_UNKNOWN}
    key = primary_key(tag, INDEX)
    fake_bluetooth.advertise(
        key.mac_address, manufacturer_data=separated_advertisement(key, LOW_BATTERY)
    )
    await hass.async_block_till_done()
    await _refresh(hass, storage)
    assert storage.coordinator.last_update_success is True

    assert _states(hass, "tag", accessory=True) == {
        "device_tracker.findmy_tag": STATE_UNKNOWN,
        "sensor.tag_latitude": STATE_UNKNOWN,
        "sensor.tag_longitude": STATE_UNKNOWN,
        "sensor.tag_position": STATE_UNKNOWN,
        "sensor.tag_battery_level": "low",
        "sensor.tag_battery": "40",
        "binary_sensor.tag_battery_low": STATE_ON,
        "binary_sensor.tag_bluetooth_presence": STATE_ON,
        "sensor.tag_signal_strength": "-60",
    }
    assert set(_states(hass, "keys", accessory=False).values()) == {STATE_UNKNOWN}

    # An entry that is not loaded is what shows unavailable.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert get_state(hass, "device_tracker.findmy_tag").state == STATE_UNAVAILABLE
    assert get_state(hass, "sensor.tag_battery").state == STATE_UNAVAILABLE
