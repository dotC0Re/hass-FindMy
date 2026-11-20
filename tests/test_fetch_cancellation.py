"""Entries that are unloaded or reloaded while a fetch runs (G2, G5).

Unloading an account entry closes its account on the worker, and worker jobs run one after the
other: without cancelling it first, the close waited behind a fetch with that account for up to
two minutes. A device entry reloaded during a fetch has a new device object by the time the
fetch ends, and its report has to reach that one.

The entity platforms are mocked and every account is a mock on the worker thread, as in
test_fetch_budget_paths.py; nothing here talks to Apple.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, final
from unittest.mock import AsyncMock, MagicMock

import pytest
from findmy import KeyPair
from homeassistant.config_entries import ConfigEntry

from custom_components.findmy import (
    async_remove_entry,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.findmy.account import AccountRuntime, FetchStatus
from custom_components.findmy.budget import account_budget_id
from custom_components.findmy.key_cache import CachedFindMyAccessory, SearchWindow
from custom_components.findmy.storage import RuntimeStorage

from .conftest import FetchClock, make_account_state, make_config_entry, make_mock_account

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# The library accounts opened for account entries, by Apple ID, oldest first.
type Apple = dict[str, list[MagicMock]]

JANE = "jane@example.com"


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
    """A new library account mock for every setup of an account entry."""
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


@final
class Gate:
    """Holds a fake fetch on the worker thread until the test lets it go.

    ``started`` is set on the test's loop once the fetch runs; ``cancelled`` once the worker
    cancelled it.
    """

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.started = asyncio.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.sources: list[object] = []

    async def fetch(self, sources: list[object]) -> dict[object, None]:
        self.sources = list(sources)
        self.loop.call_soon_threadsafe(self.started.set)
        try:
            _ = await asyncio.to_thread(self.release.wait, 10)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return dict.fromkeys(sources)


async def start(hass: HomeAssistant) -> RuntimeStorage:
    assert await async_setup(hass, {}) is True
    return RuntimeStorage.get(hass)


def account_entry(hass: HomeAssistant, email: str = JANE) -> ConfigEntry:
    data = {"type": "account", "account_data": make_account_state(email=email)}
    return make_config_entry(hass, data)


def fetches(apple: Apple, email: str = JANE) -> int:
    return sum(mock.fetch_location.await_count for mock in apple.get(email, []))


def budget_record(storage: RuntimeStorage, email: str = JANE) -> datetime | None:
    return storage.budget.last_fetch(account_budget_id(email))


def unlocated_accessory() -> CachedFindMyAccessory:
    """A cached accessory with a gap below its top window, so that a catch-up is planned."""
    now = datetime.now(tz=UTC)
    accessory = CachedFindMyAccessory.from_json(
        {
            "type": "accessory",
            "master_key": bytes(range(1, 29)).hex(),
            "skn": bytes(range(32)).hex(),
            "sks": bytes(range(32, 64)).hex(),
            "paired_at": (now - timedelta(days=40)).isoformat(),
            "name": "Unlocated tag",
            "model": None,
            "identifier": "unlocated",
            "group_identifier": None,
            "serial_number": None,
            "alignment_date": (now - timedelta(days=10)).isoformat(),
            "alignment_index": 30 * 96,
        }
    )
    assert isinstance(accessory, CachedFindMyAccessory)
    return accessory


async def test_unloading_an_account_cancels_its_fetch_instead_of_waiting(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    storage = await start(hass)
    coordinator = storage.coordinator
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True
    account = storage.get_entry(entry)
    assert isinstance(account, AccountRuntime)
    accessory = unlocated_accessory()
    alignment = accessory.alignment
    coordinator.async_add_listener(lambda: None, accessory)
    gate = Gate()

    async def fetch_and_realign(sources: list[object]) -> dict[object, None]:
        # The reports found so far realign the window; the fetch then hangs.
        for source in sources:
            if isinstance(source, SearchWindow):
                source.update_alignment(datetime.now(tz=UTC), source.highest)
        return await gate.fetch(sources)

    library = apple[JANE][0]
    library.fetch_location.side_effect = fetch_and_realign
    refresh = asyncio.create_task(coordinator.async_refresh())
    await asyncio.wait_for(gate.started.wait(), 10)
    cursor = accessory._catch_up_cursor

    # Well below the two minutes of the fetch limit.
    assert await asyncio.wait_for(async_unload_entry(hass, entry), 5) is True
    await asyncio.wait_for(refresh, 5)

    assert await asyncio.to_thread(gate.cancelled.wait, 5)
    # The thread the fake fetch waited in cannot be cancelled; let it end.
    gate.release.set()
    library.close.assert_awaited_once()
    # Not a failure of the coordinator: the other devices keep their data.
    assert coordinator.last_update_success is True
    assert account.last_fetch_result is FetchStatus.ERROR
    # The job had started, so it may have reached Apple and counts.
    assert budget_record(storage) == fetch_clock.now
    # Nothing of the cancelled fetch was taken over.
    assert accessory.alignment == alignment
    assert accessory._catch_up_cursor == cursor
    assert coordinator._running_fetch is None


async def test_reloading_an_account_during_its_fetch_keeps_its_limit(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    storage = await start(hass)
    coordinator = storage.coordinator
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True
    gate = Gate()
    apple[JANE][0].fetch_location.side_effect = gate.fetch
    refresh = asyncio.create_task(coordinator.async_refresh())
    await asyncio.wait_for(gate.started.wait(), 10)

    assert await asyncio.wait_for(async_unload_entry(hass, entry), 5) is True
    assert await asyncio.wait_for(async_setup_entry(hass, entry), 5) is True
    await refresh
    gate.release.set()

    fetch_clock.advance(timedelta(minutes=10))
    await coordinator.async_refresh()
    assert fetches(apple) == 1
    fetch_clock.advance(timedelta(minutes=5))
    await coordinator.async_refresh()
    assert fetches(apple) == 2
    renewed = storage.get_entry(entry)
    assert isinstance(renewed, AccountRuntime)
    assert renewed.last_fetch_result is FetchStatus.SUCCESS


async def test_fetch_cancelled_while_its_keys_are_derived_does_not_count(
    hass: HomeAssistant,
    platforms: MagicMock,
    apple: Apple,
    fetch_clock: FetchClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing was handed to the worker yet, let alone sent to Apple."""
    storage = await start(hass)
    coordinator = storage.coordinator
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True
    accessory = unlocated_accessory()
    coordinator.async_add_listener(lambda: None, accessory)
    deriving = threading.Event()
    release = threading.Event()
    original = CachedFindMyAccessory.search_window

    def slow_search_window(self: CachedFindMyAccessory, now: datetime) -> SearchWindow:
        deriving.set()
        _ = release.wait(10)
        return original(self, now)

    monkeypatch.setattr(CachedFindMyAccessory, "search_window", slow_search_window)
    refresh = asyncio.create_task(coordinator.async_refresh())
    assert await asyncio.to_thread(deriving.wait, 5)

    assert await asyncio.wait_for(async_unload_entry(hass, entry), 5) is True
    await asyncio.wait_for(refresh, 5)
    release.set()
    await hass.async_block_till_done()

    assert fetches(apple) == 0
    assert budget_record(storage) is None
    assert coordinator.last_update_success is True


async def test_unloading_another_account_lets_the_running_fetch_finish(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    """The fetch of one account is not thrown away because another one is unloaded."""
    storage = await start(hass)
    coordinator = storage.coordinator
    fetching = account_entry(hass, "a@example.com")
    unloaded = account_entry(hass, "b@example.com")
    assert await async_setup_entry(hass, fetching) is True
    assert await async_setup_entry(hass, unloaded) is True
    gate = Gate()
    apple["a@example.com"][0].fetch_location.side_effect = gate.fetch
    refresh = asyncio.create_task(coordinator.async_refresh())
    await asyncio.wait_for(gate.started.wait(), 10)

    unload = asyncio.create_task(async_unload_entry(hass, unloaded))
    await asyncio.sleep(0.1)
    # The close of the unloaded account waits for its turn on the worker.
    assert not unload.done()
    gate.release.set()
    assert await asyncio.wait_for(unload, 5) is True
    await refresh

    assert not gate.cancelled.is_set()
    assert coordinator.last_update_success is True
    account = storage.get_entry(fetching)
    assert isinstance(account, AccountRuntime)
    assert account.last_fetch_result is FetchStatus.SUCCESS
    apple["b@example.com"][0].close.assert_awaited_once()


@dataclass(frozen=True)
class FakeReport:
    """What the coordinator and the stored reports read from a location report."""

    timestamp: datetime
    latitude: float = 52.520008
    longitude: float = 13.404954
    horizontal_accuracy: int = 10
    status: int = 0


@pytest.mark.parametrize("equality", ["by keys", "by identity"])
async def test_report_of_a_device_reloaded_during_the_fetch_reaches_its_new_object(  # noqa: PLR0917
    hass: HomeAssistant,
    platforms: MagicMock,
    apple: Apple,
    fetch_clock: FetchClock,
    monkeypatch: pytest.MonkeyPatch,
    equality: str,
) -> None:
    """G5: the report and the alignment went to the old object, and were never stored.

    In FindMy.py 0.10.2 two objects of one accessory compare equal, by their keys. The
    coordinator must not rely on that: with identity as equality, the report still has to
    reach the object that is loaded for the entry when the fetch ends.
    """
    if equality == "by identity":
        monkeypatch.setattr(CachedFindMyAccessory, "__eq__", object.__eq__)
        monkeypatch.setattr(CachedFindMyAccessory, "__hash__", object.__hash__)
    storage = await start(hass)
    coordinator = storage.coordinator
    assert await async_setup_entry(hass, account_entry(hass)) is True
    tag = make_config_entry(
        hass, {"type": "device_rolling_derived", "data": unlocated_accessory().to_json()}
    )
    assert await async_setup_entry(hass, tag) is True
    old = storage.get_entry(tag)
    assert isinstance(old, CachedFindMyAccessory)
    remove_listener = coordinator.async_add_listener(lambda: None, old)
    gate = Gate()
    seen_at = datetime.now(tz=UTC) - timedelta(minutes=5)
    report = FakeReport(seen_at)
    found: list[tuple[datetime, int]] = []

    async def fetch(sources: list[object]) -> dict[object, object]:
        _ = await gate.fetch(sources)
        reports: dict[object, object] = {}
        for source in sources:
            reports[source] = None
            if type(source) is SearchWindow:
                source.update_alignment(seen_at, source.highest)
                found.append((seen_at, source.highest))
                reports[source] = report
        return reports

    apple[JANE][0].fetch_location.side_effect = fetch
    refresh = asyncio.create_task(coordinator.async_refresh())
    await asyncio.wait_for(gate.started.wait(), 10)

    # The reload: the entities of the old object go, those of the new one come.
    assert await async_unload_entry(hass, tag) is True
    remove_listener()
    assert await async_setup_entry(hass, tag) is True
    new = storage.get_entry(tag)
    assert isinstance(new, CachedFindMyAccessory)
    assert new is not old
    _ = coordinator.async_add_listener(lambda: None, new)
    gate.release.set()
    await refresh

    assert coordinator.last_update_success is True
    assert (coordinator.data or {}).get(new) == report
    stored = storage.reports.get(tag.entry_id)
    assert stored is not None
    assert stored.timestamp == seen_at
    assert new.alignment == found[0]


# --- One fetch at a time ----------------------------------------------------------------------
#
# The fetch is counted against the budget when its job starts on the worker (G1), so between the
# choice of the account and that moment nothing else may choose it again. Home Assistant runs one
# refresh at a time (the lock of the coordinator's debouncer); these tests take its paths, and
# also overlap refreshes outside that lock, which the coordinator guards against itself.


@final
class Derivation:
    """Holds the key derivation of the next search windows in their executor thread."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        original = CachedFindMyAccessory.search_window

        def slow(accessory: CachedFindMyAccessory, now: datetime) -> SearchWindow:
            self.started.set()
            _ = self.release.wait(10)
            return original(accessory, now)

        monkeypatch.setattr(CachedFindMyAccessory, "search_window", slow)

    async def wait_started(self) -> None:
        assert await asyncio.to_thread(self.started.wait, 5)


async def set_up_with_accessory(hass: HomeAssistant, *emails: str) -> RuntimeStorage:
    storage = await start(hass)
    for email in emails or (JANE,):
        assert await async_setup_entry(hass, account_entry(hass, email)) is True
    _ = storage.coordinator.async_add_listener(lambda: None, unlocated_accessory())
    return storage


def count_budget_records(storage: RuntimeStorage, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The budget ids the budget counts a fetch for, in order."""
    recorded: list[str] = []
    original = storage.budget.record_fetch

    def record(budget_id: str, now: datetime) -> None:
        recorded.append(budget_id)
        original(budget_id, now)

    monkeypatch.setattr(storage.budget, "record_fetch", record)
    return recorded


async def first_started(gates: dict[str, Gate]) -> Gate:
    """The gate of whichever account fetches first."""
    waits = {asyncio.ensure_future(gate.started.wait()): gate for gate in gates.values()}
    done, pending = await asyncio.wait(waits, timeout=10, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        _ = task.cancel()
    assert done
    return waits[next(iter(done))]


async def test_refreshes_started_while_the_first_derives_keys_fetch_once(
    hass: HomeAssistant,
    platforms: MagicMock,
    apple: Apple,
    fetch_clock: FetchClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) Through Home Assistant's lock, and around it: one fetch, counted once, no failure."""
    storage = await set_up_with_accessory(hass)
    coordinator = storage.coordinator
    recorded = count_budget_records(storage, monkeypatch)
    derivation = Derivation(monkeypatch)

    first = asyncio.create_task(coordinator.async_refresh())
    await derivation.wait_started()
    # Waits for Home Assistant's lock, then finds the account counted.
    second = asyncio.create_task(coordinator.async_refresh())
    # Around the lock: returns at once, with the data as it is.
    assert await coordinator._async_update_data() == {}
    derivation.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 10)

    assert fetches(apple) == 1
    assert recorded == [account_budget_id(JANE)]
    assert coordinator.last_update_success is True
    assert coordinator.last_exception is None
    assert coordinator._running_fetch is None


async def test_delayed_refresh_during_a_running_scheduled_refresh_does_not_fetch_again(
    hass: HomeAssistant,
    platforms: MagicMock,
    apple: Apple,
    fetch_clock: FetchClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) A device set up while the scheduled refresh fetches plans a refresh of its own."""
    storage = await set_up_with_accessory(hass)
    coordinator = storage.coordinator
    recorded = count_budget_records(storage, monkeypatch)
    gate = Gate()
    apple[JANE][0].fetch_location.side_effect = gate.fetch

    # What Home Assistant's timer runs for a scheduled refresh.
    scheduled = asyncio.create_task(coordinator._handle_refresh_interval())
    await asyncio.wait_for(gate.started.wait(), 10)
    device = make_config_entry(hass, {"type": "device_static", "data": KeyPair.new().to_json()})
    assert await async_setup_entry(hass, device) is True
    coordinator.schedule_refresh(0)
    await asyncio.sleep(0.05)
    assert fetches(apple) == 1
    gate.release.set()
    await asyncio.wait_for(scheduled, 10)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert fetches(apple) == 1
    assert recorded == [account_budget_id(JANE)]
    assert coordinator.last_update_success is True
    assert coordinator._running_fetch is None


async def test_second_account_fetches_after_the_first_never_alongside_it(
    hass: HomeAssistant,
    platforms: MagicMock,
    apple: Apple,
    fetch_clock: FetchClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) With two accounts, a refresh that arrives during a fetch does not prepare another.

    Through Home Assistant's lock it runs once the first fetch has ended, and may then fetch
    with the other account: the limit is per account. Around the lock it fetches nothing.
    """
    emails = ("a@example.com", "b@example.com")
    storage = await set_up_with_accessory(hass, *emails)
    coordinator = storage.coordinator
    recorded = count_budget_records(storage, monkeypatch)
    gates = {email: Gate() for email in emails}
    running: list[str] = []
    most = 0

    def fetch_for(email: str) -> Any:
        async def fetch(sources: list[object]) -> dict[object, None]:
            nonlocal most
            running.append(email)
            most = max(most, len(running))
            try:
                return await gates[email].fetch(sources)
            finally:
                running.remove(email)

        return fetch

    for email in emails:
        apple[email][0].fetch_location.side_effect = fetch_for(email)

    first = asyncio.create_task(coordinator.async_refresh())
    _ = await first_started(gates)
    assert await coordinator._async_update_data() == {}
    second = asyncio.create_task(coordinator.async_refresh())
    await asyncio.sleep(0.05)
    assert sum(fetches(apple, email) for email in emails) == 1
    for gate in gates.values():
        gate.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 10)

    assert [fetches(apple, email) for email in emails] == [1, 1]
    assert sorted(recorded) == sorted(account_budget_id(email) for email in emails)
    assert most == 1
    assert coordinator.last_update_success is True
    assert coordinator._running_fetch is None


@pytest.mark.parametrize(
    "ending", ["success", "failure", "unload", "refresh cancelled", "shutdown"]
)
async def test_next_fetch_is_possible_however_the_last_one_ended(
    hass: HomeAssistant,
    platforms: MagicMock,
    apple: Apple,
    fetch_clock: FetchClock,
    ending: str,
) -> None:
    """The guard of the running fetch is released on every path the fetch can end."""
    storage = await start(hass)
    coordinator = storage.coordinator
    entry = account_entry(hass)
    assert await async_setup_entry(hass, entry) is True
    gate = Gate()
    library = apple[JANE][0]
    library.fetch_location.side_effect = TimeoutError if ending == "failure" else gate.fetch
    refresh = asyncio.create_task(coordinator.async_refresh())
    if ending != "failure":
        await asyncio.wait_for(gate.started.wait(), 10)
        assert coordinator._running_fetch is not None

    if ending == "success":
        gate.release.set()
    elif ending == "unload":
        assert await async_unload_entry(hass, entry) is True
    elif ending == "refresh cancelled":
        _ = refresh.cancel()
    elif ending == "shutdown":
        await storage.worker.async_stop()
    if ending == "refresh cancelled":
        with pytest.raises(asyncio.CancelledError):
            await refresh
    else:
        await asyncio.wait_for(refresh, 10)
    # The thread the fake fetch waited in cannot be cancelled; let it end.
    gate.release.set()
    # The fetch task has ended once the loop ran its callbacks.
    await asyncio.sleep(0)

    assert coordinator._running_fetch is None
    if ending in {"unload", "shutdown"}:
        return
    fetch_clock.advance()
    library.fetch_location.side_effect = None
    await coordinator.async_refresh()
    assert fetches(apple) == 2
    assert coordinator.last_update_success is True


def device_entry(hass: HomeAssistant) -> ConfigEntry:
    data = {"type": "device_rolling_derived", "data": unlocated_accessory().to_json()}
    return make_config_entry(hass, data)


async def loaded_accessory(
    hass: HomeAssistant, storage: RuntimeStorage, entry: ConfigEntry
) -> CachedFindMyAccessory:
    """Set a device entry up and make the coordinator fetch for it, as its entities do."""
    assert await async_setup_entry(hass, entry) is True
    accessory = storage.get_entry(entry)
    assert isinstance(accessory, CachedFindMyAccessory)
    storage.coordinator.async_add_listener(lambda: None, accessory)
    return accessory


async def test_fetch_keeps_the_checkpoints_of_the_chains_it_walked(
    hass: HomeAssistant, platforms: MagicMock, apple: Apple, fetch_clock: FetchClock
) -> None:
    """The usual case: the next start can begin where this fetch got to."""
    storage = await start(hass)
    assert await async_setup_entry(hass, account_entry(hass)) is True
    entry = device_entry(hass)
    _ = await loaded_accessory(hass, storage, entry)
    assert storage.key_checkpoints.get(entry.entry_id) == {}

    await storage.coordinator.async_refresh()

    assert fetches(apple) == 1
    assert storage.key_checkpoints.get(entry.entry_id) != {}


@pytest.mark.parametrize("slow", ["search_window", "chain_checkpoints"])
async def test_device_removed_while_a_fetch_walks_its_chain_leaves_no_checkpoints(  # noqa: PLR0917
    hass: HomeAssistant,
    platforms: MagicMock,
    apple: Apple,
    fetch_clock: FetchClock,
    monkeypatch: pytest.MonkeyPatch,
    slow: str,
) -> None:
    """Review findings A-5 and N-2: the fetch still holds the accessory when the entry is gone.

    Removing the device forgets its record. The fetch, held up in the executor while it cut the
    window or collected the checkpoints, must not write the record again afterwards.
    """
    storage = await start(hass)
    coordinator = storage.coordinator
    assert await async_setup_entry(hass, account_entry(hass)) is True
    entry = device_entry(hass)
    accessory = await loaded_accessory(hass, storage, entry)
    held = threading.Event()
    release = threading.Event()
    original = getattr(CachedFindMyAccessory, slow)
    calls = 0

    def held_up(self: CachedFindMyAccessory, *args: Any) -> Any:
        nonlocal calls
        calls += 1
        result = original(self, *args)
        if calls == 1:
            # The chain is walked; the fetch has not written anything yet.
            held.set()
            _ = release.wait(10)
        return result

    monkeypatch.setattr(CachedFindMyAccessory, slow, held_up)
    refresh = asyncio.create_task(coordinator.async_refresh())
    assert await asyncio.to_thread(held.wait, 5)

    assert await asyncio.wait_for(async_unload_entry(hass, entry), 5) is True
    await async_remove_entry(hass, entry)
    assert storage.key_checkpoints.get(entry.entry_id) == {}
    release.set()
    await asyncio.wait_for(refresh, 10)
    await hass.async_block_till_done()

    assert accessory.chain_checkpoints() != {}
    assert storage.key_checkpoints.get(entry.entry_id) == {}
