"""The coordinator that fetches the location reports of all devices.

All config entries share this one coordinator. Each refresh asks one Apple account for the
latest report of every device. Which account, and whether any at all, is up to the fetch budget
(budget.py): it remembers across restarts when each account last fetched, so that none of them
fetches more than once per 15 minutes, whatever starts the refresh. The accounts take turns, and
the schedule spreads them out, so that with n accounts an update arrives every 15/n minutes.

The fetch itself runs on the FindMy worker thread (worker.py, account.py), with a time limit.
This module is the event loop's side of it: it prepares what the worker searches, waits, and
takes over the results. The worker never gets an object the event loop works with as well. An
accessory is searched through windows cut for that one fetch, and the alignment the reports
lead to is applied here afterwards; key_cache.py explains the model and why it was chosen.
"""

# pyright: reportImportCycles=false

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, final, override

import aiohttp
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from findmy import (
    FindMyAccessory,
    FixedRollingKeyPairAccessory,
    InvalidCredentialsError,
    InvalidStateError,
    KeyPair,
    LocationReport,
    UnauthorizedError,
    UnhandledProtocolError,
)

from ._entity import account_name
from .budget import MIN_FETCH_INTERVAL, account_budget_id
from .const import signal_account_fetch
from .key_cache import CachedFindMyAccessory, CatchUpWindow, SearchWindow
from .worker import WorkerNotRunningError, notify_job_start

if TYPE_CHECKING:
    from asyncio import TimerHandle

    from homeassistant.core import HomeAssistant

    from .account import AccountRuntime, FetchSource
    from .budget import FetchBudget
    from .last_reports import KnownReport
    from .storage import RuntimeStorage

_LOGGER = logging.getLogger(__name__)

FindMyDevice = KeyPair | FindMyAccessory | FixedRollingKeyPairAccessory
# A report is a library object once it was fetched in this run, and plain data while it is the
# one restored after a restart (last_reports.py). The entities read the same attributes of both.
type FindMyLocationData = dict[FindMyDevice, KnownReport | None]

# Raised by FindMy.py when a session expired and no password is stored to sign in again. That
# is the normal end of a session: accounts are restored without their password (account.py).
_MISSING_CREDENTIALS = "No username or password specified"

# Time a fetch may run on the worker, in seconds. A fetch is a handful of requests of at most
# five seconds each plus the decryption of the reports, far less than this. The limit is for a
# request that never returns: without it, such a fetch would block every later job on the
# worker, including those of the other accounts.
_FETCH_TIMEOUT = 120.0

# Shortest wait the coordinator schedules itself. Home Assistant may run a scheduled refresh up
# to a second early (it rounds the loop time down), which the budget then turns away; the retry
# follows at the time the budget names, but never in a tight loop.
_MIN_SCHEDULE_DELAY = timedelta(seconds=1)


def _utcnow() -> datetime:
    """The time the fetch budget decides on. Tests replace it to let time pass."""
    return dt_util.utcnow()


# What a fetch is known to raise. Anything else is a bug and keeps its traceback in the log.
_FETCH_ERRORS = (
    UnauthorizedError,
    InvalidStateError,
    InvalidCredentialsError,
    UnhandledProtocolError,
    aiohttp.ClientError,
    TimeoutError,
    # Missing fields in the stored session or in Apple's answer, such as 'searchPartyToken'.
    KeyError,
    ValueError,
)


def _fetch_copy(accessory: FindMyAccessory) -> FindMyAccessory:
    """A copy of an accessory without key cache, for the worker to search instead of it.

    Storage creates cached accessories, which are searched through windows. This keeps the
    rule that the worker gets nothing the event loop uses for any other accessory as well.
    """
    return FindMyAccessory.from_json(accessory.to_json())


def _apply_copy_alignment(copy: FindMyAccessory, accessory: FindMyAccessory) -> None:
    """Give an accessory the alignment the reports of a fetch led its copy to."""
    found, known = copy.to_json(), accessory.to_json()
    date, index = found["alignment_date"], found["alignment_index"]
    if date is None or index is None:
        return
    if (date, index) != (known["alignment_date"], known["alignment_index"]):
        accessory.update_alignment(datetime.fromisoformat(date), index)


def _is_auth_error(err: Exception) -> bool:
    """Whether a fetch failed because the account has to sign in again."""
    if isinstance(err, (UnauthorizedError, InvalidStateError, InvalidCredentialsError)):
        return True
    return isinstance(err, ValueError) and _MISSING_CREDENTIALS in str(err)


def _describe_fetch_error(err: Exception) -> str:
    """Short message for the log and the coordinator state; the cause keeps the details."""
    if isinstance(err, TimeoutError):
        return "Timeout while fetching location reports"
    if isinstance(err, aiohttp.ClientError):
        return f"Could not reach Apple: {err}"
    if isinstance(err, KeyError):
        return f"Apple account session is incomplete: missing {err}"
    if isinstance(err, UnhandledProtocolError):
        return f"Unexpected answer from Apple: {err}"
    return f"Could not fetch location reports: {err}"


@final
class FindMyCoordinator(DataUpdateCoordinator[FindMyLocationData | None]):
    def __init__(self, hass: HomeAssistant, storage: RuntimeStorage) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Location Reports",
            update_interval=None,
            always_update=False,
        )

        self._storage = storage

        self._refresh_handle: TimerHandle | None = None

    def schedule_refresh(self, delay_seconds: float = 10.0) -> None:
        """Schedule a refresh after a quiet period."""
        if self._refresh_handle is not None:
            self._refresh_handle.cancel()
            self._refresh_handle = None
        if self._shutdown_requested:
            # An entry set up while Home Assistant stops; it would not refresh anyway.
            return

        def _request_refresh() -> None:
            self._refresh_handle = None
            # A background task, like the refreshes Home Assistant schedules itself: the start
            # of Home Assistant does not wait for a fetch that the setup of an entry asked for,
            # and the task is cancelled when Home Assistant stops.
            _ = self.hass.async_create_background_task(
                self._async_delayed_refresh(),
                name="findmy delayed refresh",
            )

        self._refresh_handle = self.hass.loop.call_later(delay_seconds, _request_refresh)

    async def _async_delayed_refresh(self) -> None:
        """Run the delayed refresh; nobody awaits the task, so its errors are logged here."""
        try:
            # Not async_request_refresh: its debouncer runs the refresh in a task of its own,
            # which Home Assistant would track and wait for again. The timer above already
            # coalesces the setups of several entries into one refresh.
            await self.async_refresh()
        except Exception:
            # The coordinator handles what a fetch raises; whatever gets here is a bug.
            _LOGGER.exception("The delayed refresh of the location reports failed")

    @override
    async def async_shutdown(self) -> None:
        """Cancel every planned refresh, including the delayed one, and ignore new ones.

        Home Assistant calls this when it stops (see storage.py). The coordinator belongs to
        no config entry, so nothing else would: without it, a refresh planned before the stop
        could still start while Home Assistant shuts down.
        """
        if self._refresh_handle is not None:
            self._refresh_handle.cancel()
            self._refresh_handle = None
        await super().async_shutdown()

    def _fetchable_accounts(self) -> list[AccountRuntime]:
        """Accounts that take part in the rotation: all but those waiting for a new sign-in."""
        # The marker lives on the account object. A reloaded entry gets a new one, which is
        # what puts a re-authenticated account back into the rotation.
        return [account for account in self._storage.accounts if not account.reauth_required]

    def _budget_id(self, account: AccountRuntime) -> str:
        """The id an account is known by in the fetch budget.

        From the Apple ID, so that a reloaded, re-authenticated or re-added entry keeps the
        record of its account. The unique id of an account entry is the lower-cased Apple ID;
        the entry id is only the last resort, for an account that knows neither.
        """
        if account.account_name:
            return account_budget_id(account.account_name)
        entry = self.hass.config_entries.async_get_entry(account.entry_id)
        unique_id = entry.unique_id if entry is not None else None
        if isinstance(unique_id, str) and unique_id:
            return account_budget_id(unique_id)
        return account_budget_id(f"entry:{account.entry_id}")

    def _rotation(self) -> dict[str, AccountRuntime]:
        """The accounts in the rotation by budget id, in the order of the storage."""
        rotation: dict[str, AccountRuntime] = {}
        for account in self._fetchable_accounts():
            # Two entries of one Apple ID share its budget; the first one fetches.
            _ = rotation.setdefault(self._budget_id(account), account)
        return rotation

    async def reload(self) -> None:
        """Plan the next fetch again. Must be called after adding or removing an account."""
        await self._storage.budget.async_load()
        self._schedule_next_fetch()

    def _schedule_next_fetch(self) -> None:
        """Set the interval Home Assistant waits before the next scheduled refresh.

        The next fetch is due when the budget allows one of the accounts in the rotation, but
        not before 15/n minutes after the latest fetch of any of them (or after now, if none
        is known): without that spread, n accounts that are due together would fetch one right
        after the other. Only accounts in the rotation count; counting one that rests would
        fetch the others too often.
        """
        rotation = self._rotation()
        if not rotation:
            _LOGGER.debug("No Apple account can fetch; not scheduling any refresh")
            self.update_interval = None
            # Also a refresh that is already planned: it would find nothing to do (R3).
            self._async_unsub_refresh()
            return

        budget = self._storage.budget
        now = _utcnow()
        due = budget.next_due(rotation, now) or now
        latest = max(
            (last for budget_id in rotation if (last := budget.last_fetch(budget_id))),
            default=now,
        )
        due = max(due, latest + MIN_FETCH_INTERVAL / len(rotation))
        self.update_interval = max(due - now, _MIN_SCHEDULE_DELAY)
        _LOGGER.debug("Next scheduled refresh in %s", self.update_interval)

    def get_account_last_fetch(self, account: AccountRuntime) -> datetime | None:
        """When the account last fetched.

        The end of its latest fetch in this run, or else the start of the latest one before,
        as the budget remembers it across restarts and reloads.
        """
        if account.last_fetch is not None:
            return account.last_fetch
        return self._storage.budget.last_fetch(self._budget_id(account))

    def get_account_next_fetch(self, account: AccountRuntime) -> datetime | None:
        """The earliest time the budget lets the account fetch again.

        None while the account rests until it was re-authenticated, and for an account with no
        known fetch. Another account that is due as well may still go first.
        """
        if account.reauth_required:
            return None
        budget = self._storage.budget
        budget_id = self._budget_id(account)
        if budget.last_fetch(budget_id) is None:
            return None
        return budget.next_allowed(budget_id, _utcnow())

    def restore_report(self, device: FindMyDevice, entry_id: str) -> None:
        """Give a device that is being set up the latest report known from before, if any.

        Must be called before the entities of the device are created: this does not notify
        listeners. A device that already has a report keeps it.
        """
        restored = self._storage.reports.get(entry_id)
        if restored is None or (self.data or {}).get(device) is not None:
            return
        _LOGGER.debug("Restoring the last known location of %s", device)
        self.data = {**(self.data or {}), device: restored}

    @property
    def devices(self) -> list[FindMyDevice]:
        """Returns a list of all devices that have been registered with the coordinator."""
        return list({ctx for ctx in self.async_contexts() if isinstance(ctx, FindMyDevice)})  # pyright: ignore[reportAny]

    async def _async_fetch_sources(
        self, devices: list[FindMyDevice]
    ) -> dict[FetchSource, FindMyDevice]:
        """Prepare what the worker searches, each source mapped to the device it stands for.

        A cached accessory is searched through its top window, cut in an executor thread
        because that derives the keys the cache does not hold yet: all of them after a restart,
        a single index 15 minutes after the previous fetch. FindMy.py would otherwise derive
        them between two requests, hundreds of elliptic-curve operations per accessory.

        An accessory whose index fell below the top window would never be found again. Its
        older indices are searched bit by bit through catch-up windows, see key_cache.py.
        """
        now = datetime.now(tz=UTC)
        sources: dict[FetchSource, FindMyDevice] = {}
        for device in devices:
            if isinstance(device, CachedFindMyAccessory):
                window = await self.hass.async_add_executor_job(device.search_window, now)
                sources[window] = device
                planned = device.plan_catch_up(now)
                if planned is not None:
                    _LOGGER.debug("Catching up on key indices %i..%i of %s", *planned, device.name)
                    catch_up = await self.hass.async_add_executor_job(
                        device.catch_up_window, *planned
                    )
                    sources[catch_up] = device
            elif isinstance(device, FindMyAccessory):
                sources[_fetch_copy(device)] = device
            else:
                # Static key pairs and accessories with pre-generated keys: the fetch only
                # reads them and nothing ever changes them, so they are safe to share.
                sources[device] = device
        return sources

    def _apply_alignments(self, sources: dict[FetchSource, FindMyDevice]) -> None:
        """Realign the accessories with what the reports of a finished fetch showed.

        FindMy.py reports alignments to the source it searched, on the worker thread. The
        accessories themselves are only ever realigned here, on the event loop, where the
        Bluetooth matching reads and updates the same alignment (device_tracker.py).
        """
        for source, device in sources.items():
            if isinstance(source, SearchWindow):
                found = source.found_alignment
                if found is not None:
                    source.accessory.update_alignment(*found)
            elif source is not device and isinstance(device, FindMyAccessory):
                assert isinstance(source, FindMyAccessory)
                _apply_copy_alignment(source, device)

    async def _async_fetch(
        self,
        account: AccountRuntime,
        budget_id: str,
        sources: dict[FetchSource, FindMyDevice],
    ) -> dict[FetchSource, LocationReport | None]:
        """Run the fetch on the worker and take over the alignments it found."""
        budget = self._storage.budget

        def count_fetch() -> None:
            # The fetch counts against the limit of the account once its job starts on the
            # worker, whatever happens afterwards: from then on it may have reached Apple, and
            # a fetch that crashes, hangs or times out must not be repeated at once. A fetch
            # that never got that far did not leave Home Assistant and does not count: one
            # cancelled while its keys were derived or while it waited for the worker (Home
            # Assistant stopping, the account being unloaded), or one the stopped worker
            # turned away. Counting those would make the account wait up to 15 minutes after
            # a restart or a reload for a fetch that never happened.
            budget.record_fetch(budget_id, _utcnow())

        try:
            with notify_job_start(count_fetch):
                reports = await account.async_fetch(list(sources), timeout=_FETCH_TIMEOUT)
        except Exception:
            # The job on the worker is over, so the sources are no longer in use there. What
            # the reports of the accessories searched before the failure showed still holds.
            # A cancelled refresh does not get here: its job may still be unwinding.
            self._apply_alignments(sources)
            raise
        self._apply_alignments(sources)
        return reports

    def _signal_fetch(self, account: AccountRuntime) -> None:
        """Tell the sensors of an account that the state of its fetch changed."""
        async_dispatcher_send(self.hass, signal_account_fetch(account_name(account)))

    def _start_reauth(self, account: AccountRuntime) -> None:
        """Ask to re-authenticate the config entry of an account.

        All entries share this coordinator, which has no config entry of its own, so raising
        ConfigEntryAuthFailed from an update would not start re-authentication of any entry.
        """
        entry = self.hass.config_entries.async_get_entry(account.entry_id)
        if entry is None:
            _LOGGER.error("Could not find the config entry of account %s", account_name(account))
            return

        # Does nothing if a re-authentication of the entry is already in progress.
        entry.async_start_reauth(self.hass)

    def _require_reauth(self, account: AccountRuntime) -> None:
        """Take an account out of the rotation and ask the user to sign in again.

        Fetching with it again would only repeat the failure, and FindMy.py answers an expired
        session with a new sign-in attempt at Apple. That must not be repeated every few minutes
        with credentials Apple already refused, so the account rests until its entry was reloaded.
        """
        account.reauth_required = True
        # The schedule follows when the refresh ends (_async_update_data).
        self._start_reauth(account)

    @override
    async def _async_update_data(self) -> FindMyLocationData:
        budget = self._storage.budget
        await budget.async_load()
        if self.hass.is_stopping:
            # A refresh that was asked for while Home Assistant stops. Scheduled refreshes
            # and those after the shutdown of this coordinator never get here.
            _LOGGER.debug("Home Assistant is stopping; not fetching location reports")
            return self.data or {}
        try:
            return await self._async_fetch_with_next_account(budget)
        finally:
            # Also after a refresh that raised: Home Assistant plans the next one from this.
            self._schedule_next_fetch()

    async def _async_fetch_with_next_account(self, budget: FetchBudget) -> FindMyLocationData:
        """Fetch with the account the budget picks, or keep the data if none may fetch.

        Every refresh comes through here: the scheduled ones, the one after setup, reloads,
        re-authentication and manual refreshes. That makes this the one place that keeps the
        15-minute limit of each account.
        """
        rotation = self._rotation()
        if not rotation:
            if self._storage.accounts:
                # Only reached by a manual refresh: no refresh is scheduled in this state.
                msg = "Every Apple account needs to be re-authenticated"
                raise UpdateFailed(msg)
            _LOGGER.debug("Skipping data update due to missing accounts")
            # Not an empty result: the devices keep what is known about them, such as the
            # locations restored after a restart. Without an account nothing replaces them.
            return self.data or {}

        now = _utcnow()
        budget_id = budget.pick(rotation, now)
        if budget_id is None:
            # Not a failure: the entities keep their data and stay available.
            _LOGGER.debug(
                "No Apple account may fetch before %s; keeping the current data",
                budget.next_due(rotation, now),
            )
            return self.data or {}
        account = rotation[budget_id]
        _LOGGER.debug("Using lookup account: %s", account)

        # Counted against the budget once the worker starts the fetch, see _async_fetch.
        account.fetch_started()
        self._signal_fetch(account)

        devices: list[FindMyDevice] = self.devices
        _LOGGER.debug("Fetching reports for devices: %s", devices)
        success = False
        try:
            # Inside the try block, so that a failure still ends the fetch started above.
            sources = await self._async_fetch_sources(devices)
            device_reports = await self._async_fetch(account, budget_id, sources)
            success = True
        except WorkerNotRunningError as err:
            # Home Assistant is stopping; the worker has cancelled the fetch.
            msg = "Fetching location reports was interrupted by the shutdown"
            raise UpdateFailed(msg) from err
        except _FETCH_ERRORS as err:
            # Some of these are also what a programming error raises (KeyError, ValueError).
            # The translated message is short on purpose, so keep the traceback available.
            _LOGGER.debug("Fetching location reports failed", exc_info=True)

            if not _is_auth_error(err):
                raise UpdateFailed(_describe_fetch_error(err)) from err

            # Apple invalidates sessions periodically. No password is kept to sign in again
            # with (FindMy.py then raises the ValueError above), an account can sit in
            # REQUIRE_2FA (InvalidStateError), or Apple refuses the request outright. Every
            # later fetch fails the same way until the account entry is re-authenticated.
            _LOGGER.warning(
                "Apple account needs to be re-authenticated (state: %s): %s",
                account.login_state.name,
                err,
            )
            self._require_reauth(account)

            # Not ConfigEntryAuthFailed: this coordinator serves every account, and Home
            # Assistant stops scheduling refreshes of a coordinator that raised it. One expired
            # account would end the fetches of all the others.
            msg = "Apple account needs to be re-authenticated"
            raise UpdateFailed(msg) from err
        finally:
            account.fetch_finished(success=success)
            self._signal_fetch(account)
            # A fetch may have renewed the session even if it failed afterwards (R5).
            _ = account.persist_session(self.hass)

        return self._take_over_reports(sources, device_reports)

    def _take_over_reports(
        self,
        sources: dict[FetchSource, FindMyDevice],
        device_reports: dict[FetchSource, LocationReport | None],
    ) -> FindMyLocationData:
        """Merge the reports of a fetch that worked into the data of the coordinator."""
        # Only after a fetch that worked, so that a failed one searches the same windows again.
        for source in sources:
            if isinstance(source, CatchUpWindow):
                source.accessory.catch_up_searched(source)

        fetched: dict[FindMyDevice, LocationReport] = {}
        for source, report in device_reports.items():
            _LOGGER.debug("Got reports for device: %s - %s", source, report)
            # A report from a window or a copy is a report of the accessory it was made from.
            device = sources.get(source)
            if device is None:
                _LOGGER.warning("Got a report for something that was not asked for: %s", source)
                continue
            if not report:
                continue

            # The top window and a catch-up window of an accessory can both bring a report;
            # the newer one counts.
            known = fetched.get(device)
            if known is None or report.timestamp >= known.timestamp:
                fetched[device] = report

        # Kept across restarts, see last_reports.py.
        for device, report in fetched.items():
            entry_id = self._storage.get_entry_id(device)
            if entry_id is not None:
                self._storage.reports.remember(entry_id, report)

        data: FindMyLocationData = (self.data or {}).copy()
        data.update(fetched)
        return data
