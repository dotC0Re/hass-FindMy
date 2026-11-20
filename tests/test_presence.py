"""Bluetooth presence and signal strength of a rolling-key accessory (presence.py).

The presence sensor learns the accessory's current address from the device tracker's matches,
and how recently that address was heard from the Bluetooth history, which Home Assistant
updates for every advertisement while it runs the callbacks only when the payload changes.
FakeBluetooth models both: ``notify=False`` is the same payload heard again.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import aiohttp
import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import State
from homeassistant.helpers import restore_state
from homeassistant.util import dt as dt_util

from custom_components.findmy.const import CONF_AWAY_TIMEOUT

from .conftest import make_account_state, make_config_entry
from .ha_platforms import (
    FakeBluetooth,
    LibraryAccounts,
    VirtualClock,
    async_setup_findmy,
    get_state,
    primary_key,
    separated_advertisement,
)

if TYPE_CHECKING:
    from findmy import FindMyAccessory, KeyPair
    from homeassistant.core import HomeAssistant

    from .conftest import AccessoryFactory

pytestmark = pytest.mark.requires_bluetooth

INDEX = 192
PRESENCE = "binary_sensor.test_tag_bluetooth_presence"
SIGNAL = "sensor.test_tag_signal_strength"
STATUS = 0b0100_0000
STEP = timedelta(seconds=30)


@pytest.fixture
def accessory(accessory_factory: AccessoryFactory) -> FindMyAccessory:
    return accessory_factory(name="Test Tag")


@pytest.fixture
def key(accessory: FindMyAccessory) -> KeyPair:
    return primary_key(accessory, INDEX)


async def _setup(
    hass: HomeAssistant, accessory: FindMyAccessory, *, away_timeout: int | None = None
) -> str:
    entry = make_config_entry(hass, {"type": "device_rolling_derived", "data": accessory.to_json()})
    if away_timeout is not None:
        hass.config_entries.async_update_entry(entry, options={CONF_AWAY_TIMEOUT: away_timeout})
    await async_setup_findmy(hass)
    return entry.entry_id


async def _pass(hass: HomeAssistant, clock: VirtualClock, duration: timedelta) -> None:
    """Let time pass in steps of the presence refresh interval."""
    for _ in range(int(duration / STEP)):
        await clock.async_advance(hass, STEP)


def _hear(bluetooth: FakeBluetooth, key: KeyPair, *, rssi: int = -60, notify: bool) -> None:
    bluetooth.advertise(
        key.mac_address,
        manufacturer_data=separated_advertisement(key, STATUS),
        rssi=rssi,
        notify=notify,
    )


async def test_present_while_heard_and_away_after_the_timeout(
    platform_hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    virtual_clock: VirtualClock,
    accessory: FindMyAccessory,
    key: KeyPair,
) -> None:
    hass = platform_hass
    await _setup(hass, accessory)
    _hear(fake_bluetooth, key, notify=True)
    await hass.async_block_till_done()
    assert get_state(hass, PRESENCE).state == STATE_ON
    assert get_state(hass, PRESENCE).attributes["away_timeout"] == 10

    # Heard again every few minutes, with the same payload: no callback, only the history.
    for _ in range(4):
        await _pass(hass, virtual_clock, timedelta(minutes=4))
        _hear(fake_bluetooth, key, notify=False)
    await _pass(hass, virtual_clock, STEP)
    presence = get_state(hass, PRESENCE)
    assert presence.state == STATE_ON
    # The longest silence between two advertisements, in seconds.
    assert presence.attributes["max_gap"] == pytest.approx(240, abs=31)

    # Not heard for the default ten minutes: away, and without a signal strength.
    await _pass(hass, virtual_clock, timedelta(minutes=9))
    assert get_state(hass, PRESENCE).state == STATE_ON
    await _pass(hass, virtual_clock, timedelta(minutes=1))
    assert get_state(hass, PRESENCE).state == STATE_OFF
    assert get_state(hass, SIGNAL).state == STATE_UNKNOWN

    # Back as soon as the refresh finds it in the history again.
    _hear(fake_bluetooth, key, rssi=-71, notify=False)
    await _pass(hass, virtual_clock, STEP)
    assert get_state(hass, PRESENCE).state == STATE_ON
    assert get_state(hass, SIGNAL).state == "-71"


async def test_away_timeout_comes_from_the_entry_options(
    platform_hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    virtual_clock: VirtualClock,
    accessory: FindMyAccessory,
    key: KeyPair,
) -> None:
    hass = platform_hass
    await _setup(hass, accessory, away_timeout=2)
    _hear(fake_bluetooth, key, notify=True)
    await hass.async_block_till_done()
    assert get_state(hass, PRESENCE).attributes["away_timeout"] == 2

    await _pass(hass, virtual_clock, timedelta(minutes=1, seconds=30))
    assert get_state(hass, PRESENCE).state == STATE_ON
    await _pass(hass, virtual_clock, timedelta(minutes=1))
    assert get_state(hass, PRESENCE).state == STATE_OFF


async def test_signal_strength_follows_large_changes_at_once_and_small_ones_later(
    platform_hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    virtual_clock: VirtualClock,
    accessory: FindMyAccessory,
    key: KeyPair,
) -> None:
    hass = platform_hass
    await _setup(hass, accessory)
    _hear(fake_bluetooth, key, rssi=-60, notify=True)
    await hass.async_block_till_done()
    assert get_state(hass, SIGNAL).state == "-60"

    _hear(fake_bluetooth, key, rssi=-62, notify=False)
    await _pass(hass, virtual_clock, STEP)
    assert get_state(hass, SIGNAL).state == "-60"

    _hear(fake_bluetooth, key, rssi=-66, notify=False)
    await _pass(hass, virtual_clock, STEP)
    assert get_state(hass, SIGNAL).state == "-66"

    _hear(fake_bluetooth, key, rssi=-67, notify=False)
    await _pass(hass, virtual_clock, STEP)
    assert get_state(hass, SIGNAL).state == "-66"
    # Five minutes after the last write, a small change is written as well.
    for _ in range(10):
        _hear(fake_bluetooth, key, rssi=-67, notify=False)
        await _pass(hass, virtual_clock, STEP)
    assert get_state(hass, SIGNAL).state == "-67"


async def test_restart_while_present_assumes_present_until_the_timeout(
    platform_hass: HomeAssistant,
    virtual_clock: VirtualClock,
    accessory: FindMyAccessory,
) -> None:
    """Home Assistant restarted while the accessory was here; nothing has been heard yet."""
    hass = platform_hass
    _restore(hass, State(PRESENCE, STATE_ON, {"last_seen": "2026-11-19T07:00:00+00:00"}))
    await _setup(hass, accessory)

    assert get_state(hass, PRESENCE).state == STATE_ON
    await _pass(hass, virtual_clock, timedelta(minutes=9, seconds=30))
    assert get_state(hass, PRESENCE).state == STATE_ON
    await _pass(hass, virtual_clock, timedelta(minutes=1))
    assert get_state(hass, PRESENCE).state == STATE_OFF


async def test_restart_while_away_keeps_away_and_the_last_sighting(
    platform_hass: HomeAssistant,
    virtual_clock: VirtualClock,
    accessory: FindMyAccessory,
) -> None:
    hass = platform_hass
    _restore(hass, State(PRESENCE, STATE_OFF, {"last_seen": "2026-11-19T07:00:00+00:00"}))
    await _setup(hass, accessory)
    await _pass(hass, virtual_clock, STEP)

    presence = get_state(hass, PRESENCE)
    assert presence.state == STATE_OFF
    assert presence.attributes["last_seen"] == dt_util.parse_datetime("2026-11-19T07:00:00+00:00")


async def test_reload_takes_over_the_address_already_matched(
    platform_hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    accessory: FindMyAccessory,
    key: KeyPair,
) -> None:
    """A reloaded entry starts from the last local match instead of waiting for a rotation."""
    hass = platform_hass
    entry_id = await _setup(hass, accessory)
    _hear(fake_bluetooth, key, rssi=-55, notify=True)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_reload(entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    presence = get_state(hass, PRESENCE)
    assert presence.state == STATE_ON
    assert presence.attributes["mac_address"] == key.mac_address
    assert get_state(hass, SIGNAL).state == "-55"


async def test_presence_stays_available_when_fetches_fail(
    platform_hass: HomeAssistant,
    fake_bluetooth: FakeBluetooth,
    library_accounts: LibraryAccounts,
    accessory: FindMyAccessory,
    key: KeyPair,
) -> None:
    """Presence only follows local Bluetooth; the state of Apple's servers is not its concern."""
    hass = platform_hass
    make_config_entry(
        hass, {"type": "account", "account_data": make_account_state(email="a@example.com")}
    )
    await _setup(hass, accessory)
    _hear(fake_bluetooth, key, notify=True)
    await hass.async_block_till_done()
    library_accounts.by_email[
        "a@example.com"
    ].fetch_location.side_effect = aiohttp.ClientConnectionError()

    from custom_components.findmy.storage import RuntimeStorage  # noqa: PLC0415

    coordinator = RuntimeStorage.get(hass).coordinator
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert get_state(hass, PRESENCE).state == STATE_ON
    assert get_state(hass, SIGNAL).state == "-60"


def _restore(hass: HomeAssistant, state: State) -> None:
    """What Home Assistant restores for an entity after a restart."""
    data = restore_state.async_get(hass)
    data.last_states[state.entity_id] = restore_state.StoredState(state, None, dt_util.utcnow())
