"""The coordinator that fetches the location reports of all devices.

All config entries share this one coordinator. Each refresh takes the next Apple account in
turn and asks it for the latest report of every device; the interval is divided between the
accounts so that each of them is used once per 15 minutes.

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
from .const import signal_account_fetch
from .key_cache import CachedFindMyAccessory, CatchUpWindow, SearchWindow
from .worker import WorkerNotRunningError

if TYPE_CHECKING:
    from asyncio import TimerHandle

    from homeassistant.core import HomeAssistant

    from .account import AccountRuntime, FetchSource
    from .storage import RuntimeStorage

_LOGGER = logging.getLogger(__name__)

FindMyDevice = KeyPair | FindMyAccessory | FixedRollingKeyPairAccessory
type FindMyLocationData = dict[FindMyDevice, LocationReport | None]

# Raised by FindMy.py when a session expired and no password is stored to sign in again. That
# is the normal end of a session: accounts are restored without their password (account.py).
_MISSING_CREDENTIALS = "No username or password specified"

# Time a fetch may run on the worker, in seconds. A fetch is a handful of requests of at most
# five seconds each plus the decryption of the reports, far less than this. The limit is for a
# request that never returns: without it, such a fetch would block every later job on the
# worker, including those of the other accounts.
_FETCH_TIMEOUT = 120.0

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
    # minimum time (in seconds) between location fetches on an account.
    _MIN_ACCOUNT_UPDATE_DELAY = 15 * 60

    def __init__(self, hass: HomeAssistant, storage: RuntimeStorage) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Location Reports",
            update_interval=None,
            always_update=False,
        )

        self._storage = storage

        self._cur_acc_index = 0
        self._refresh_handle: TimerHandle | None = None

    def schedule_refresh(self, delay_seconds: float = 10.0) -> None:
        """Schedule a refresh after a quiet period."""
        if self._refresh_handle is not None:
            self._refresh_handle.cancel()

        def _request_refresh() -> None:
            self._refresh_handle = None
            _ = self.hass.async_create_task(
                self.async_request_refresh(),
                name="findmy delayed startup refresh",
            )

        self._refresh_handle = self.hass.loop.call_later(delay_seconds, _request_refresh)

    def _fetchable_accounts(self) -> list[AccountRuntime]:
        """Accounts that take part in the rotation: all but those waiting for a new sign-in."""
        # The marker lives on the account object. A reloaded entry gets a new one, which is
        # what puts a re-authenticated account back into the rotation.
        return [account for account in self._storage.accounts if not account.reauth_required]

    def get_account(self) -> AccountRuntime | None:
        accounts = self._fetchable_accounts()
        if not accounts:
            return None

        account = accounts[self._cur_acc_index % len(accounts)]
        self._cur_acc_index += 1

        return account

    async def reload(self) -> None:
        """Updates coordinator intervals. Must be called after adding or removing a new account."""
        self._apply_account_interval()

    def _apply_account_interval(self) -> None:
        # Only accounts in the rotation share the interval. Counting one that is skipped would
        # fetch the remaining ones more often than once per _MIN_ACCOUNT_UPDATE_DELAY.
        accounts = self._fetchable_accounts()
        if not accounts:
            _LOGGER.debug("Coordinator: disabling updates due to missing account")
            self.update_interval = None
            return

        cur_interval = self.update_interval.total_seconds() if self.update_interval else None
        interval = self._MIN_ACCOUNT_UPDATE_DELAY // len(accounts)
        if cur_interval == interval:
            return

        _LOGGER.debug(
            "Coordinator: Updating interval: %i",
            self._MIN_ACCOUNT_UPDATE_DELAY // len(accounts),
        )
        self.update_interval = timedelta(seconds=self._MIN_ACCOUNT_UPDATE_DELAY // len(accounts))

    def get_account_next_fetch(self, account: AccountRuntime) -> datetime | None:
        """Returns the next time a fetch will be attempted for the given account."""
        last_fetch = account.last_fetch
        if last_fetch is None or account.reauth_required:
            return None
        # Accounts are fetched round-robin, one per update interval (see reload()),
        # so the same account is fetched again after _MIN_ACCOUNT_UPDATE_DELAY.
        return last_fetch + timedelta(seconds=self._MIN_ACCOUNT_UPDATE_DELAY)

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
        self, account: AccountRuntime, sources: dict[FetchSource, FindMyDevice]
    ) -> dict[FetchSource, LocationReport | None]:
        """Run the fetch on the worker and take over the alignments it found."""
        try:
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
        self._apply_account_interval()
        self._start_reauth(account)

    @override
    async def _async_update_data(self) -> FindMyLocationData:
        account = self.get_account()
        if account is None:
            if self._storage.accounts:
                # Only reached by a manual refresh: reload() disables the schedule in this state.
                msg = "Every Apple account needs to be re-authenticated"
                raise UpdateFailed(msg)
            _LOGGER.debug("Skipping data update due to missing accounts")
            return {}
        _LOGGER.debug("Using lookup account: %s", account)

        account.fetch_started()
        self._signal_fetch(account)

        devices: list[FindMyDevice] = self.devices
        _LOGGER.debug("Fetching reports for devices: %s", devices)
        success = False
        try:
            # Inside the try block, so that a failure still ends the fetch started above.
            sources = await self._async_fetch_sources(devices)
            device_reports = await self._async_fetch(account, sources)
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

        data: FindMyLocationData = (self.data or {}).copy()
        data.update(fetched)
        return data
