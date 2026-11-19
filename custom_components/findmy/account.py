"""Apple accounts: the state kept in a config entry, and the account while it is loaded.

Stored state
------------
The FindMy library serialises an account, including the Apple ID password, into one mapping.
Config entries are stored as plain text and end up in every backup, so the password must not
be part of what is written there (finding S1). The session tokens in the mapping are enough to
fetch location reports; once Apple invalidates them the user signs in again through the
re-authentication flow. :func:`without_password` is the one place that removes the password;
everything that stores or restores account state goes through it, so an account restored from
an entry does not hold a password in memory either.

Loaded account
--------------
A library account (``AsyncAppleAccount``) owns aiohttp sessions, which are bound to the event
loop they were created on, and it computes for a long time between its awaits (report
decryption, Anisette). It therefore lives on the FindMy worker thread (worker.py): it is
created, used and closed inside worker jobs and never touched from Home Assistant's loop.

:class:`AccountRuntime` is what Home Assistant's side holds instead. All of its attributes are
plain data that is read and written on Home Assistant's loop only: the names the entities are
built from, the login state, the serialised session and the state of the latest fetch. They are
copied out of the library account at the end of a job, never read from it in passing. The
library account itself is a private attribute that only the job functions in this module use.

Jobs of all accounts share one worker and run one after another, so a job of this module may
wait behind a location fetch of another account before it starts.

An account that uses the integrated Anisette provider is restored with the path of the one
verified library bundle (anisette_libs.py), so that the provider loads it from there instead of
downloading it on first use after every start.

Every path that drops a library account closes it on the worker first. That matters beyond
tidiness: FindMy.py closes an account that is garbage collected while still open, on whatever
event loop is running in the thread that collects it, which could be Home Assistant's. For an
account that was closed before, that last call does nothing.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Self, cast, final, override

from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)

from findmy import AsyncAppleAccount, LoginState

from .anisette_libs import (
    AnisetteLibsChecksumError,
    AnisetteLibsUnavailableError,
    async_ensure_anisette_libs,
)
from .worker import WorkerNotRunningError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import timedelta
    from pathlib import Path

    from homeassistant.core import HomeAssistant

    from findmy import AccountStateMapping, LocationReport, RollingKeyPairSource
    from findmy.keys import HasHashedPublicKey

    from .worker import FindMyWorker

    type FetchSource = HasHashedPublicKey | RollingKeyPairSource

_LOGGER = logging.getLogger(__name__)

# What restoring an account raises for stored data that is damaged: missing fields (KeyError,
# or ValueError where FindMy.py translates it), an unknown login state or Anisette type
# (ValueError), a wrong `type` marker (AssertionError), or a value of the wrong shape (TypeError).
_RESTORE_ERRORS = (ValueError, KeyError, AssertionError, TypeError)

# Closing waits for aiohttp to shut its connections down, which can hang on a dead network.
_CLOSE_TIMEOUT = 15.0


def without_password(account_data: AccountStateMapping) -> AccountStateMapping:
    """Return a copy of serialised account state that does not contain the password.

    The input is left untouched; it may be the read-only data of a config entry.
    """
    return {**account_data, "account": {**account_data["account"], "password": None}}


class FetchStatus(Enum):
    """How the latest location fetch of an account went."""

    SUCCESS = "success"
    ERROR = "error"
    ONGOING = "ongoing"


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """Plain data copied out of a library account at the end of a worker job."""

    login_state: LoginState
    # Without the password, and a deep copy: the library keeps using the dicts it serialises.
    session_data: AccountStateMapping


def _snapshot(account: AsyncAppleAccount) -> _Snapshot:
    """Copy what Home Assistant's side needs from an account. Runs inside a worker job."""
    return _Snapshot(
        login_state=account.login_state,
        session_data=copy.deepcopy(without_password(account.to_json())),
    )


@dataclass(frozen=True, slots=True)
class _Opened:
    """Result of the job that created a library account on the worker."""

    account: AsyncAppleAccount
    remove_stop_callback: Callable[[], None]
    snapshot: _Snapshot
    account_name: str | None
    first_name: str | None
    last_name: str | None


def _uses_local_anisette(account_data: object) -> bool:
    """Whether stored account state names the integrated Anisette provider.

    Damaged data counts as "no": restoring it reports the damage, and needs no bundle for it.
    """
    if not isinstance(account_data, Mapping):
        return False
    anisette: object = cast("Mapping[str, object]", account_data).get("anisette")
    return isinstance(anisette, Mapping) and (
        cast("Mapping[str, object]", anisette).get("type") == "aniLocal"
    )


async def async_local_anisette_libs(hass: HomeAssistant, account_data: object) -> Path | None:
    """Put the library bundle in place if the stored account needs it, and return its path.

    Returns None for an account with a remote Anisette server, and for data that is damaged
    (:meth:`AccountRuntime.async_load` reports that). For an account with the integrated
    provider the bundle is verified, and downloaded first if there is none, see
    anisette_libs.py. This runs on Home Assistant's loop, before the account is restored on
    the worker: it needs Home Assistant's HTTP session and executor.

    Raises ConfigEntryError if the stored bundle does not match its recorded checksum, which
    only the user can resolve (the message names the file to delete), and ConfigEntryNotReady
    if there is no bundle and it could not be downloaded, so that Home Assistant tries the
    setup again later.
    """
    if not _uses_local_anisette(account_data):
        return None
    try:
        return await async_ensure_anisette_libs(hass)
    except AnisetteLibsChecksumError as err:
        raise ConfigEntryError(str(err)) from err
    except AnisetteLibsUnavailableError as err:
        raise ConfigEntryNotReady(str(err)) from err


def _broken_data_error(err: Exception) -> ConfigEntryError:
    # The exception names a field or a type, never a value, so it is safe to show. An
    # AssertionError of the library has no text at all.
    detail = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__
    msg = (
        f"The stored Apple account data cannot be restored ({detail}). "
        "Remove this entry and add the account again."
    )
    return ConfigEntryError(msg)


@final
class AccountRuntime:
    """An Apple account of a config entry, as seen from Home Assistant's event loop.

    Created with :meth:`async_load`. Every attribute and method is for Home Assistant's loop;
    the methods that talk to Apple run their work as jobs on the FindMy worker.
    """

    def __init__(
        self,
        *,
        worker: FindMyWorker,
        entry_id: str,
        opened: _Opened,
        stored_data: AccountStateMapping | None,
    ) -> None:
        self._worker = worker
        # Only ever used inside worker jobs.
        self._account = opened.account
        self._remove_stop_callback = opened.remove_stop_callback

        self.entry_id = entry_id
        # Captured once: the entities are named after them, and they only change with a new
        # sign-in, which reloads the entry and creates a new object.
        self.account_name = opened.account_name
        self.first_name = opened.first_name
        self.last_name = opened.last_name
        self.login_state = opened.snapshot.login_state
        # Set by the coordinator when the session expired. The account then rests until its
        # entry is reloaded, which replaces this object.
        self.reauth_required = False

        self._session_data = opened.snapshot.session_data
        # What the config entry held (the password aside) when this account was loaded from it
        # or last wrote to it; see persist_session.
        self._stored_data = stored_data

        self._fetching = False
        self._fetch_started: datetime | None = None
        self._last_fetch: datetime | None = None
        self._last_fetch_ok: bool | None = None
        self._last_fetch_duration: timedelta | None = None

    @override
    def __repr__(self) -> str:
        # Neither name nor e-mail: this ends up in debug logs.
        return f"AccountRuntime(entry_id={self.entry_id!r})"

    @classmethod
    async def async_load(
        cls,
        worker: FindMyWorker,
        entry_id: str,
        account_data: AccountStateMapping | None,
        *,
        anisette_libs_path: Path | None = None,
    ) -> Self:
        """Restore the account of a config entry on the worker.

        ``anisette_libs_path`` is the library bundle :func:`async_local_anisette_libs` returned
        for this account data. An account that uses the integrated Anisette provider is not
        restored without it; an account with a remote Anisette server has no use for it.

        Raises ConfigEntryError if the stored data is missing or damaged, and
        ConfigEntryAuthFailed if it restores to an account that is not signed in, so that Home
        Assistant asks the user to sign in again. In both cases nothing is left behind on the
        worker.
        """
        try:
            if account_data is None:
                msg = "no account data"
                raise ValueError(msg)  # reported below like any other damaged data
            # Copied before it crosses to the worker thread: the library keeps the nested
            # dicts it is given, and those of a config entry belong to Home Assistant's loop.
            stored = copy.deepcopy(without_password(account_data))
        except _RESTORE_ERRORS as err:
            raise _broken_data_error(err) from err

        if anisette_libs_path is None and _uses_local_anisette(stored):
            # Restored without the bundle, the provider would download it on its own on first
            # use: unchecked and with no time limit, which anisette_libs.py is there to
            # prevent. A caller that gets here skipped async_local_anisette_libs.
            msg = "an account with the integrated Anisette provider needs anisette_libs_path"
            raise RuntimeError(msg)

        def restore() -> AsyncAppleAccount:
            # A second copy, so that `stored` stays what the entry held whatever the library
            # does with the mapping it was restored from.
            return AsyncAppleAccount.from_json(
                copy.deepcopy(stored), anisette_libs_path=anisette_libs_path
            )

        return await cls._async_open(worker, entry_id, restore, stored)

    @classmethod
    async def _async_open(
        cls,
        worker: FindMyWorker,
        entry_id: str,
        factory: Callable[[], AsyncAppleAccount],
        stored_data: AccountStateMapping | None = None,
    ) -> Self:
        """Create a library account on the worker and wrap it. Tests pass their own factory."""

        async def open_account() -> _Opened:
            try:
                account = factory()
            except _RESTORE_ERRORS as err:
                raise _broken_data_error(err) from err

            try:
                state = account.login_state
                if state is not LoginState.LOGGED_IN:
                    msg = f"The Apple account is not signed in (state: {state.name})"
                    raise ConfigEntryAuthFailed(msg)  # noqa: TRY301  # closes the account below
                return _Opened(
                    account=account,
                    # Registered here, on the worker, so that no stop can slip in between
                    # creating the account and making sure it gets closed.
                    remove_stop_callback=worker.add_stop_callback(account.close),
                    snapshot=_snapshot(account),
                    # The library only hands these out for an account that is signed in.
                    account_name=cast("str | None", account.account_name),
                    first_name=cast("str | None", account.first_name),
                    last_name=cast("str | None", account.last_name),
                )
            except BaseException:
                await account.close()
                raise

        opened = await worker.run(open_account)
        return cls(worker=worker, entry_id=entry_id, opened=opened, stored_data=stored_data)

    async def async_fetch(
        self,
        sources: Sequence[FetchSource],
        *,
        timeout: float,  # noqa: ASYNC109  # limits the run time on the worker, see FindMyWorker.run
    ) -> dict[FetchSource, LocationReport | None]:
        """Fetch the latest location report of each source, on the worker.

        ``sources`` are used on the worker thread while this call waits. They must be objects
        nothing else changes or relies on in the meantime: static key pairs, or windows and
        copies made for this fetch (see key_cache.py).

        Raises whatever the library raises, :class:`TimeoutError` after ``timeout`` seconds on
        the worker, and ``WorkerNotRunningError`` while Home Assistant is stopping. The login
        state and the session data are refreshed whether the fetch worked or not.
        """
        account = self._account

        async def fetch() -> tuple[
            dict[FetchSource, LocationReport | None] | None, Exception | None, _Snapshot
        ]:
            reports = None
            error = None
            try:
                reports = await account.fetch_location(sources)
            except Exception as err:  # noqa: BLE001  # raised again below, on the caller's loop
                error = err
            # Also after a failure: a fetch that renewed the session may still fail later on.
            return reports, error, _snapshot(account)

        reports, error, snapshot = await self._worker.run(fetch, timeout=timeout)
        self.login_state = snapshot.login_state
        self._session_data = snapshot.session_data
        if error is not None:
            raise error
        assert reports is not None
        return reports

    def persist_session(self, hass: HomeAssistant) -> bool:
        """Write the session data back to the config entry if a fetch has renewed it (R5).

        Returns whether the entry was written. Writing does not reload the entry: nothing
        listens for updates of account entries.
        """
        entry = hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return False
        current = cast("AccountStateMapping | None", entry.data.get("account_data"))
        if current == self._session_data:
            return False

        try:
            untouched = current is not None and without_password(current) == self._stored_data
        except KeyError, TypeError:
            untouched = False
        if not untouched:
            # Someone else has written the entry since this account was loaded from it: the
            # user signed in again. The reload that follows replaces this account, and its
            # old session must not overwrite the new one.
            return False

        _LOGGER.debug("Storing renewed session data of account entry %s", self.entry_id)
        _ = hass.config_entries.async_update_entry(
            entry, data={**entry.data, "account_data": self._session_data}
        )
        self._stored_data = self._session_data
        return True

    async def async_close(self) -> None:
        """Close the account's network sessions on the worker. Does not raise."""
        try:
            await self._worker.run(self._account.close, timeout=_CLOSE_TIMEOUT)
        except WorkerNotRunningError:
            # Home Assistant is stopping. The worker closes the account itself, through the
            # stop callback registered when the account was opened.
            return
        except TimeoutError:
            _LOGGER.warning("Closing the Apple account of entry %s timed out", self.entry_id)
        except Exception:
            # Unloading the entry has to go on; there is nothing else to do with the account.
            _LOGGER.exception("Closing the Apple account of entry %s failed", self.entry_id)
        self._remove_stop_callback()

    def fetch_started(self) -> None:
        """Record that a location fetch with this account begins now."""
        self._fetching = True
        self._fetch_started = datetime.now(tz=UTC)

    def fetch_finished(self, *, success: bool) -> None:
        """Record how the fetch that was started last ended."""
        now = datetime.now(tz=UTC)
        self._fetching = False
        self._last_fetch = now
        self._last_fetch_ok = success
        self._last_fetch_duration = (
            None if self._fetch_started is None else now - self._fetch_started
        )

    @property
    def last_fetch(self) -> datetime | None:
        """When the latest finished fetch ended, whatever its result."""
        return self._last_fetch

    @property
    def last_fetch_result(self) -> FetchStatus | None:
        """The result of the latest fetch, or ONGOING while one is running."""
        if self._fetching:
            return FetchStatus.ONGOING
        if self._last_fetch_ok is None:
            return None
        return FetchStatus.SUCCESS if self._last_fetch_ok else FetchStatus.ERROR

    @property
    def last_fetch_duration(self) -> timedelta | None:
        """How long the latest fetch took; None while one is running."""
        return None if self._fetching else self._last_fetch_duration
