"""Every way a refresh can start keeps the persisted 15-minute limit of an account (R4).

The limit used to live only in the coordinator's interval, so setup, reloads, re-authentication,
manual refreshes and restarts fetched at once. These tests take each of those paths through
``__init__.py`` and the storage, with the entity platforms mocked and every account a mock on
the worker thread: nothing here talks to Apple. Time passes through ``fetch_clock``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from findmy import KeyPair, UnauthorizedError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.findmy import async_setup, async_setup_entry, async_unload_entry
from custom_components.findmy import coordinator as coordinator_module
from custom_components.findmy.account import AccountRuntime, FetchStatus
from custom_components.findmy.budget import account_budget_id
from custom_components.findmy.const import DOMAIN
from custom_components.findmy.storage import RuntimeStorage

from .conftest import FetchClock, make_account_state, make_config_entry, make_mock_account

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.findmy.coordinator import FindMyCoordinator

# The library accounts opened for account entries, by Apple ID, oldest first.
type Apple = dict[str, list[MagicMock]]


@pytest.fixture
def platforms(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stands in for the entity platforms."""
    calls = MagicMock()
    calls.setup = AsyncMock(return_value=None)
    calls.unload = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", calls.setup)
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", calls.unload)
    monkeypatch.setattr(ConfigEntry, "async_start_reauth", MagicMock())
    return calls


@pytest.fixture
def apple(monkeypatch: pytest.MonkeyPatch) -> Apple:
    """The library accounts opened for account entries, by Apple ID, oldest first.

    Each setup of an entry opens a new mock, as a reload or a restart opens a new account.
    """
    opened: Apple = {}

    async def async_load(
        worker: Any, entry_id: str, account_data: Any, **_options: object
    ) -> AccountRuntime:
        email = account_data["account"]["username"]
        mock = make_mock_account(email=email, reports={})
        opened.setdefault(email, []).append(mock)
        return await AccountRuntime._async_open(worker, entry_id, lambda: mock, account_data)

    monkeypatch.setattr(AccountRuntime, "async_load", async_load)
    return opened


def fetches(apple: Apple, email: str = "jane@example.com") -> int:
    """How often the Apple ID was asked for reports, over every account opened for it."""
    return sum(mock.fetch_location.await_count for mock in apple.get(email, []))


async def start(hass: HomeAssistant) -> RuntimeStorage:
    """What Home Assistant does when it starts the integration: attach and load the budget."""
    assert await async_setup(hass, {}) is True
    return RuntimeStorage.get(hass)


async def restart(hass: HomeAssistant) -> RuntimeStorage:
    """Stop and start again on the same configuration directory."""
    old = RuntimeStorage.get(hass)
    await old.coordinator.async_shutdown()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    del hass.data[DOMAIN]
    return await start(hass)


def account_entry(hass: HomeAssistant, email: str = "jane@example.com") -> ConfigEntry:
    data = {"type": "account", "account_data": make_account_state(email=email)}
    return make_config_entry(hass, data)


async def run_delayed_refresh(hass: HomeAssistant, coordinator: FindMyCoordinator) -> None:
    """Run the refresh that setup delays by ten seconds now, through the same path."""
    assert coordinator._refresh_handle is not None
    coordinator.schedule_refresh(0)
    await asyncio.sleep(0)
    # The delayed refresh is a background task, which Home Assistant does not wait for unless
    # asked to.
    await hass.async_block_till_done(wait_background_tasks=True)
    # The next manual refresh must not wait for the debouncer's cooldown.
    coordinator._debounced_refresh.async_cancel()


def listen(coordinator: FindMyCoordinator) -> list[None]:
    """A listener, so that the coordinator schedules refreshes; records each update."""
    updates: list[None] = []
    coordinator.async_add_listener(lambda: updates.append(None))
    return updates


async def test_delayed_refresh_after_setup_does_not_fetch_again(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    """Setting up another entry shortly after a fetch plans a refresh that must not fetch."""
    storage = await start(hass)
    assert await async_setup_entry(hass, account_entry(hass)) is True
    await run_delayed_refresh(hass, storage.coordinator)
    assert fetches(apple) == 1

    fetch_clock.advance(timedelta(minutes=2))
    device = make_config_entry(hass, {"type": "device_static", "data": KeyPair.new().to_json()})
    assert await async_setup_entry(hass, device) is True
    await run_delayed_refresh(hass, storage.coordinator)

    assert fetches(apple) == 1
    assert storage.coordinator.last_update_success is True


async def test_reloading_an_account_entry_does_not_fetch_again(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    storage = await start(hass)
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True
    await run_delayed_refresh(hass, storage.coordinator)
    assert fetches(apple) == 1

    fetch_clock.advance(timedelta(minutes=1))
    assert await async_unload_entry(hass, entry) is True
    assert await async_setup_entry(hass, entry) is True
    await run_delayed_refresh(hass, storage.coordinator)

    assert fetches(apple) == 1
    assert len(apple["jane@example.com"]) == 2

    fetch_clock.advance(timedelta(minutes=14))
    await storage.coordinator.async_refresh()
    assert fetches(apple) == 2


async def test_re_authentication_does_not_lift_the_limit(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    """The failed fetch reached Apple too; the new sign-in must not fetch again at once."""
    storage = await start(hass)
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True
    apple["jane@example.com"][0].fetch_location.side_effect = UnauthorizedError("expired")
    failed_at = fetch_clock.now
    await run_delayed_refresh(hass, storage.coordinator)
    expired = storage.get_entry(entry)
    assert isinstance(expired, AccountRuntime)
    assert expired.reauth_required is True

    # The re-authentication flow reloads the entry, which opens a new account for the Apple ID.
    fetch_clock.advance(timedelta(minutes=3))
    assert await async_unload_entry(hass, entry) is True
    assert await async_setup_entry(hass, entry) is True
    await run_delayed_refresh(hass, storage.coordinator)

    renewed = storage.get_entry(entry)
    assert isinstance(renewed, AccountRuntime)
    assert renewed is not expired
    assert fetches(apple) == 1
    assert storage.coordinator.get_account_next_fetch(renewed) == failed_at + timedelta(minutes=15)

    fetch_clock.advance(timedelta(minutes=12))
    await storage.coordinator.async_refresh()
    assert fetches(apple) == 2
    assert renewed.last_fetch_result is FetchStatus.SUCCESS


async def test_manual_refresh_within_the_limit_keeps_the_data_and_fails_nothing(
    hass: HomeAssistant,
    platforms: MagicMock,
    apple: Apple,
    fetch_clock: FetchClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = await start(hass)
    coordinator = storage.coordinator
    assert await async_setup_entry(hass, account_entry(hass)) is True
    report = MagicMock(name="report")
    device = object()
    apple["jane@example.com"][0].fetch_location.return_value = {}
    await run_delayed_refresh(hass, coordinator)
    coordinator.async_set_updated_data({device: report})  # pyright: ignore[reportArgumentType]
    updates = listen(coordinator)

    fetch_clock.advance(timedelta(minutes=10))
    with caplog.at_level(logging.DEBUG, logger="custom_components.findmy.coordinator"):
        await coordinator.async_request_refresh()

    assert fetches(apple) == 1
    assert coordinator.last_update_success is True
    assert coordinator.last_exception is None
    assert coordinator.data == {device: report}
    # Unchanged data, so the entities are not even told.
    assert updates == []
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    next_allowed = fetch_clock.now + timedelta(minutes=5)
    assert f"No Apple account may fetch before {next_allowed}" in caplog.text
    # The schedule goes on, at the time the account may fetch again.
    assert coordinator.update_interval == timedelta(minutes=5)
    assert coordinator._unsub_refresh is not None


async def test_restart_within_the_limit_waits_for_the_rest_of_it(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    storage = await start(hass)
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True
    fetched_at = fetch_clock.now
    await run_delayed_refresh(hass, storage.coordinator)
    assert fetches(apple) == 1

    # Restarted after four minutes: setup and its delayed refresh find the earlier fetch.
    fetch_clock.advance(timedelta(minutes=4))
    storage = await restart(hass)
    coordinator = storage.coordinator
    assert await async_setup_entry(hass, entry) is True
    listen(coordinator)
    await run_delayed_refresh(hass, coordinator)

    assert fetches(apple) == 1
    account = storage.get_entry(entry)
    assert isinstance(account, AccountRuntime)
    # The account sensors show the fetch from before the restart.
    assert coordinator.get_account_last_fetch(account) == fetched_at
    assert coordinator.get_account_next_fetch(account) == fetched_at + timedelta(minutes=15)
    # The first fetch after the restart is planned for when the limit ends.
    assert coordinator.update_interval == timedelta(minutes=11)

    fetch_clock.advance(timedelta(minutes=11))
    await coordinator.async_refresh()
    assert fetches(apple) == 2


async def test_several_restarts_in_a_row_fetch_once(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    entry = account_entry(hass)
    storage = await start(hass)
    for _ in range(4):
        assert await async_setup_entry(hass, entry) is True
        await run_delayed_refresh(hass, storage.coordinator)
        fetch_clock.advance(timedelta(minutes=1))
        storage = await restart(hass)

    assert fetches(apple) == 1


async def test_early_scheduled_refresh_is_retried_when_the_limit_ends(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    """Home Assistant may run a scheduled refresh up to a second early."""
    storage = await start(hass)
    coordinator = storage.coordinator
    assert await async_setup_entry(hass, account_entry(hass)) is True
    listen(coordinator)
    await run_delayed_refresh(hass, coordinator)

    fetch_clock.advance(timedelta(minutes=15) - timedelta(milliseconds=400))
    await coordinator.async_refresh()

    assert fetches(apple) == 1
    # Not a whole interval later: the shortest wait the coordinator plans.
    assert coordinator.update_interval == timedelta(seconds=1)

    fetch_clock.advance(timedelta(seconds=1))
    await coordinator.async_refresh()
    assert fetches(apple) == 2


async def test_accounts_take_turns_across_entries(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    """Two accounts: one fetch every 7:30 minutes, each account once per 15 minutes."""
    storage = await start(hass)
    coordinator = storage.coordinator
    assert await async_setup_entry(hass, account_entry(hass, "a@example.com")) is True
    assert await async_setup_entry(hass, account_entry(hass, "b@example.com")) is True
    listen(coordinator)
    await run_delayed_refresh(hass, coordinator)
    assert coordinator.update_interval == timedelta(minutes=7, seconds=30)

    for _ in range(7):
        assert coordinator.update_interval is not None
        fetch_clock.advance(coordinator.update_interval)
        await coordinator.async_refresh()

    # An hour: four fetches per account, never two in a row.
    assert fetches(apple, "a@example.com") == 4
    assert fetches(apple, "b@example.com") == 4


async def test_schedule_resumes_after_every_account_was_re_authenticated(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    storage = await start(hass)
    coordinator = storage.coordinator
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True
    listen(coordinator)
    apple["jane@example.com"][0].fetch_location.side_effect = UnauthorizedError("expired")
    await run_delayed_refresh(hass, coordinator)

    assert coordinator.update_interval is None
    assert coordinator._unsub_refresh is None

    fetch_clock.advance(timedelta(minutes=20))
    assert await async_unload_entry(hass, entry) is True
    assert await async_setup_entry(hass, entry) is True
    await run_delayed_refresh(hass, coordinator)

    assert fetches(apple) == 2
    assert coordinator.last_update_success is True
    assert coordinator.update_interval == timedelta(minutes=15)
    assert coordinator._unsub_refresh is not None


async def test_unloading_the_last_account_cancels_the_planned_refresh(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    """R3: nothing is left to fetch with, so nothing stays scheduled."""
    storage = await start(hass)
    coordinator = storage.coordinator
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True
    listen(coordinator)
    await run_delayed_refresh(hass, coordinator)
    assert coordinator._unsub_refresh is not None

    assert await async_unload_entry(hass, entry) is True

    assert coordinator.update_interval is None
    assert coordinator._unsub_refresh is None


async def test_removing_and_adding_an_account_keeps_its_limit(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    """The budget knows the Apple ID, not the config entry."""
    storage = await start(hass)
    first = account_entry(hass, "Jane@Example.com")
    assert await async_setup_entry(hass, first) is True
    await run_delayed_refresh(hass, storage.coordinator)
    assert await async_unload_entry(hass, first) is True

    fetch_clock.advance(timedelta(minutes=5))
    second = account_entry(hass, "Jane@Example.com")
    assert second.entry_id != first.entry_id
    assert await async_setup_entry(hass, second) is True
    await run_delayed_refresh(hass, storage.coordinator)

    assert fetches(apple, "Jane@Example.com") == 1
    assert set(storage.budget.as_dict()) == {account_budget_id("jane@example.com")}


async def test_delayed_refresh_does_not_hold_up_home_assistant(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    """G3: Home Assistant waits for its tracked tasks while it starts, but not for a fetch."""
    storage = await start(hass)
    assert await async_setup_entry(hass, account_entry(hass)) is True
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    async def slow_fetch(sources: list[object]) -> dict[object, None]:
        # Runs on the worker thread, whose loop cannot wait for an event of this one.
        loop.call_soon_threadsafe(started.set)
        _ = await asyncio.to_thread(release.wait, 5)
        return dict.fromkeys(sources)

    apple["jane@example.com"][0].fetch_location.side_effect = slow_fetch
    storage.coordinator.schedule_refresh(0)
    await asyncio.wait_for(started.wait(), 5)

    # Returns while the fetch still runs.
    await asyncio.wait_for(hass.async_block_till_done(), 5)
    assert fetches(apple) == 1

    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fetches(apple) == 1
    assert storage.coordinator.last_update_success is True


async def test_error_of_the_delayed_refresh_is_logged(  # noqa: PLR0917
    hass: HomeAssistant,
    platforms: MagicMock,
    apple: Apple,
    fetch_clock: FetchClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nobody awaits the background task, so what escapes the refresh must reach the log."""
    storage = await start(hass)
    coordinator = storage.coordinator
    assert await async_setup_entry(hass, account_entry(hass)) is True
    bug = RuntimeError("bug")
    monkeypatch.setattr(coordinator, "async_refresh", AsyncMock(side_effect=bug))

    await run_delayed_refresh(hass, coordinator)

    record = next(r for r in caplog.records if "delayed refresh" in r.getMessage())
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[1] is bug


def budget_record(storage: RuntimeStorage, email: str = "jane@example.com") -> object:
    """When the budget last counted a fetch of the Apple ID, or None."""
    return storage.budget.last_fetch(account_budget_id(email))


async def test_no_refresh_starts_once_home_assistant_stops(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    """G1: the delayed refresh, the scheduled one and requested ones all stay away."""
    storage = await start(hass)
    coordinator = storage.coordinator
    assert await async_setup_entry(hass, account_entry(hass)) is True
    listen(coordinator)
    assert coordinator._refresh_handle is not None
    assert coordinator._unsub_refresh is not None

    hass.set_state(CoreState.stopping)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert coordinator._refresh_handle is None
    assert coordinator._unsub_refresh is None
    await coordinator.async_request_refresh()
    await coordinator.async_refresh()
    coordinator.schedule_refresh(0)
    assert coordinator._refresh_handle is None
    await asyncio.sleep(0.05)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert fetches(apple) == 0
    assert budget_record(storage) is None


async def test_refresh_requested_while_stopping_does_not_fetch(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    """Before the stop event reaches the coordinator, Home Assistant is already stopping."""
    storage = await start(hass)
    coordinator = storage.coordinator
    assert await async_setup_entry(hass, account_entry(hass)) is True
    hass.set_state(CoreState.stopping)

    await coordinator.async_refresh()

    assert fetches(apple) == 0
    assert budget_record(storage) is None
    assert coordinator.last_update_success is True


async def test_fetch_turned_away_by_the_stopped_worker_does_not_count(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    storage = await start(hass)
    coordinator = storage.coordinator
    assert await async_setup_entry(hass, account_entry(hass)) is True
    await storage.worker.async_stop()

    await coordinator.async_refresh()

    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert "shutdown" in str(coordinator.last_exception)
    assert budget_record(storage) is None


async def test_fetch_cancelled_while_it_waits_for_the_worker_does_not_count(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    """The job never started, so nothing was sent to Apple."""
    storage = await start(hass)
    coordinator = storage.coordinator
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True
    account = storage.get_entry(entry)
    assert isinstance(account, AccountRuntime)
    gate = threading.Event()

    async def occupy() -> None:
        _ = await asyncio.to_thread(gate.wait, 5)

    # Another job holds the worker, as a sign-in or the close of another account would.
    busy = asyncio.create_task(storage.worker.run(occupy))
    refresh = asyncio.create_task(coordinator.async_refresh())
    # Long enough for the refresh to hand its job to the worker.
    await asyncio.sleep(0.05)
    assert account.last_fetch_result is FetchStatus.ONGOING
    _ = refresh.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refresh
    gate.set()
    await busy

    assert fetches(apple) == 0
    assert budget_record(storage) is None
    assert account.last_fetch_result is FetchStatus.ERROR


@pytest.mark.parametrize("failure", ["crash", "timeout"])
async def test_fetch_that_reached_the_worker_counts_however_it_ends(  # noqa: PLR0917
    hass: HomeAssistant,
    platforms: MagicMock,
    apple: Apple,
    fetch_clock: FetchClock,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Its requests may have reached Apple before it failed."""
    storage = await start(hass)
    coordinator = storage.coordinator
    assert await async_setup_entry(hass, account_entry(hass)) is True
    library = apple["jane@example.com"][0]
    if failure == "crash":
        library.fetch_location.side_effect = RuntimeError("bug")
    else:
        monkeypatch.setattr(coordinator_module, "_FETCH_TIMEOUT", 0.05)

        async def hang(_sources: object) -> None:
            await asyncio.sleep(60)

        library.fetch_location.side_effect = hang
    counted_at = fetch_clock.now

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert budget_record(storage) == counted_at
    fetch_clock.advance(timedelta(minutes=14))
    await coordinator.async_refresh()
    assert fetches(apple) == 1
