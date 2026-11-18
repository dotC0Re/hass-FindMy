# pyright: reportImportCycles=false

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
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
from .key_cache import CachedFindMyAccessory

if TYPE_CHECKING:
    from asyncio import TimerHandle
    from collections.abc import Iterator

    from homeassistant.core import HomeAssistant

    from findmy.reports import AsyncAppleAccount

    from .storage import RuntimeStorage

_LOGGER = logging.getLogger(__name__)

FindMyDevice = KeyPair | FindMyAccessory | FixedRollingKeyPairAccessory
type FindMyLocationData = dict[FindMyDevice, LocationReport | None]

# Raised by FindMy.py when a session expired and no password is stored to sign in again.
_MISSING_CREDENTIALS = "No username or password specified"

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


# FindMy.py searches the last week of keys of a rolling-key accessory for reports.
_REPORT_KEY_WINDOW = timedelta(days=7)


class CoordinatorFetchStatus(Enum):
    """The result of a fetch attempt."""

    SUCCESS = "success"
    ERROR = "error"
    ONGOING = "ongoing"


class CoordinatorLogEvent(Enum):
    """Events that can be logged by the coordinator."""

    FETCH_START = "fetch_start"
    FETCH_ERROR = "fetch_error"
    FETCH_SUCCESS = "fetch_success"


@dataclass(frozen=True)
class CoordinatorLogEntry:
    """A log entry for a coordinator update."""

    timestamp: datetime
    account: AsyncAppleAccount
    event: CoordinatorLogEvent


@final
class FindMyCoordinator(DataUpdateCoordinator[FindMyLocationData | None]):
    # minimum time (in seconds) between location fetches on an account.
    _MIN_ACCOUNT_UPDATE_DELAY = 15 * 60
    # Fetch log entries kept per account. The account sensors read the latest start and end of
    # a fetch; the rest is slack. Kept per account, so that an account that is not fetched (it
    # waits for a new sign-in) keeps its last result however often the others are fetched.
    _MAX_LOG_ENTRIES_PER_ACCOUNT = 10

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
        self._logs: dict[AsyncAppleAccount, deque[CoordinatorLogEntry]] = {}
        # Accounts whose session expired; compared by identity (see _fetchable_accounts).
        self._reauth_required: set[AsyncAppleAccount] = set()
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

    def _fetchable_accounts(self) -> list[AsyncAppleAccount]:
        """Accounts that take part in the rotation: all but those waiting for a new sign-in."""
        accounts = self._storage.accounts
        # A reloaded entry gets a new account object, so dropping the markers of objects that
        # left the storage is what puts a re-authenticated account back into the rotation.
        self._reauth_required.intersection_update(accounts)
        return [account for account in accounts if account not in self._reauth_required]

    def get_account(self) -> AsyncAppleAccount | None:
        accounts = self._fetchable_accounts()
        if not accounts:
            return None

        account = accounts[self._cur_acc_index % len(accounts)]
        self._cur_acc_index += 1

        return account

    async def reload(self) -> None:
        """Updates coordinator intervals. Must be called after adding or removing a new account."""
        self._apply_account_interval()

        # A removed or reloaded account leaves its log behind; drop it with the account object.
        accounts = self._storage.accounts
        for account in [account for account in self._logs if account not in accounts]:
            del self._logs[account]

    def _account_logs(self, account: AsyncAppleAccount) -> Iterator[CoordinatorLogEntry]:
        """The fetch log of an account, newest entry first."""
        return reversed(self._logs.get(account, ()))

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

    def get_account_last_fetch(self, account: AsyncAppleAccount) -> datetime | None:
        """Returns the last time a fetch was attempted for the given account."""
        for log in self._account_logs(account):
            if log.event in (
                CoordinatorLogEvent.FETCH_SUCCESS,
                CoordinatorLogEvent.FETCH_ERROR,
            ):
                return log.timestamp
        return None

    def get_account_next_fetch(self, account: AsyncAppleAccount) -> datetime | None:
        """Returns the next time a fetch will be attempted for the given account."""
        last_fetch = self.get_account_last_fetch(account)
        if last_fetch is None or account in self._reauth_required:
            return None
        # Accounts are fetched round-robin, one per update interval (see reload()),
        # so the same account is fetched again after _MIN_ACCOUNT_UPDATE_DELAY.
        return last_fetch + timedelta(seconds=self._MIN_ACCOUNT_UPDATE_DELAY)

    def get_account_last_fetch_result(
        self, account: AsyncAppleAccount
    ) -> CoordinatorFetchStatus | None:
        """Returns the result of the last fetch for the given account."""
        for log in self._account_logs(account):
            match log.event:
                case CoordinatorLogEvent.FETCH_START:
                    return CoordinatorFetchStatus.ONGOING
                case CoordinatorLogEvent.FETCH_SUCCESS:
                    return CoordinatorFetchStatus.SUCCESS
                case CoordinatorLogEvent.FETCH_ERROR:
                    return CoordinatorFetchStatus.ERROR

        return None

    def get_account_last_fetch_duration(self, account: AsyncAppleAccount) -> timedelta | None:
        """Returns the duration of the last fetch for the given account."""
        end_time: datetime | None = None
        for log in self._account_logs(account):
            match log.event:
                case CoordinatorLogEvent.FETCH_SUCCESS | CoordinatorLogEvent.FETCH_ERROR:
                    end_time = log.timestamp
                case CoordinatorLogEvent.FETCH_START:
                    if end_time is not None:
                        return end_time - log.timestamp

                    # still ongoing
                    return None

        return None

    @property
    def devices(self) -> list[FindMyDevice]:
        """Returns a list of all devices that have been registered with the coordinator."""
        return list({ctx for ctx in self.async_contexts() if isinstance(ctx, FindMyDevice)})  # pyright: ignore[reportAny]

    async def _async_warm_keys(self, devices: list[FindMyDevice]) -> None:
        """Derive the keys the fetch is going to ask for outside the event loop.

        FindMy.py builds its key batches synchronously between two requests. Without this,
        hundreds of elliptic-curve key derivations per accessory block the event loop on
        every fetch. Afterwards only indices that are new since the last fetch are derived.
        """
        now = datetime.now(tz=UTC)
        for device in devices:
            if not isinstance(device, CachedFindMyAccessory):
                continue
            _ = await self.hass.async_add_executor_job(
                device.warm_keys,
                now - _REPORT_KEY_WINDOW,
                now + device.interval,
            )

    def _log(self, account: AsyncAppleAccount, event: CoordinatorLogEvent) -> None:
        logs = self._logs.setdefault(account, deque(maxlen=self._MAX_LOG_ENTRIES_PER_ACCOUNT))
        logs.append(
            CoordinatorLogEntry(timestamp=datetime.now(tz=UTC), account=account, event=event)
        )
        async_dispatcher_send(self.hass, signal_account_fetch(account_name(account)))

    def _start_reauth(self, account: AsyncAppleAccount) -> None:
        """Ask to re-authenticate the config entry of an account.

        All entries share this coordinator, which has no config entry of its own, so raising
        ConfigEntryAuthFailed from an update would not start re-authentication of any entry.
        """
        entry_id = self._storage.get_entry_id(account)
        entry = self.hass.config_entries.async_get_entry(entry_id) if entry_id else None
        if entry is None:
            _LOGGER.error("Could not find the config entry of account %s", account_name(account))
            return

        # Does nothing if a re-authentication of the entry is already in progress.
        entry.async_start_reauth(self.hass)

    def _require_reauth(self, account: AsyncAppleAccount) -> None:
        """Take an account out of the rotation and ask the user to sign in again.

        Fetching with it again would only repeat the failure, and FindMy.py answers an expired
        session with a new sign-in attempt at Apple. Repeated failing sign-ins are what gets an
        Apple account locked, so the account rests until its entry was reloaded.
        """
        self._reauth_required.add(account)
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

        self._log(account, CoordinatorLogEvent.FETCH_START)

        devices: list[FindMyDevice] = self.devices
        _LOGGER.debug("Fetching reports for devices: %s", devices)
        success = False
        try:
            # Inside the try block, so that a failure still ends the log entry started above.
            await self._async_warm_keys(devices)
            device_reports = await account.fetch_location(devices)
            success = True
        except _FETCH_ERRORS as err:
            if not _is_auth_error(err):
                raise UpdateFailed(_describe_fetch_error(err)) from err

            # Apple invalidates sessions periodically. The account then either sits in
            # REQUIRE_2FA (InvalidStateError), or signing in again with the stored password is
            # refused (InvalidCredentialsError), and every later fetch fails the same way until
            # the account entry is re-authenticated.
            _LOGGER.warning(
                "Apple account needs to be re-authenticated (state: %s): %s",
                getattr(account, "login_state", "unknown"),
                err,
            )
            self._require_reauth(account)

            # Not ConfigEntryAuthFailed: this coordinator serves every account, and Home
            # Assistant stops scheduling refreshes of a coordinator that raised it. One expired
            # account would end the fetches of all the others.
            msg = "Apple account needs to be re-authenticated"
            raise UpdateFailed(msg) from err
        finally:
            self._log(
                account,
                (CoordinatorLogEvent.FETCH_SUCCESS if success else CoordinatorLogEvent.FETCH_ERROR),
            )

        data: FindMyLocationData = (self.data or {}).copy()
        for device, report in device_reports.items():
            _LOGGER.debug("Got reports for device: %s - %s", device, report)
            if not isinstance(device, FindMyDevice):
                _LOGGER.warning("Device not supported yet: %s", device)
                continue

            if report:
                data[device] = report

        return data
