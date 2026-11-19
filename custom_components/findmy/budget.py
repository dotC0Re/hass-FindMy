"""Persisted per-account fetch budget: at most one fetch per account every 15 minutes.

Apple bans accounts that ask for location reports too often. The coordinator spaces its
scheduled fetches, but that spacing only lived in memory: every restart of Home Assistant,
every reload of an entry and every manual refresh fetched at once, however recently the account
had been used. This module keeps the time of the last fetch of each account on disk and is
meant to be asked at the one place where a fetch starts.

Two layers:

- ``FetchBudget`` holds the decision logic. It takes the current time as a parameter and knows
  nothing about Home Assistant, so it can be tested without waiting or patching clocks.
- ``PersistentFetchBudget`` adds a versioned ``Store`` (``findmy.fetch_budget``). It is loaded
  once with ``async_load`` and saved with a short delay, so a fetch never waits for the disk.

Guarantees:

- An account that is passed to ``pick`` is only returned if it has not been fetched within
  ``interval``. A fetch is recorded when it starts, so one that crashes or times out counts too.
- Among the accounts that may fetch, ``pick`` returns the one that waited longest; accounts
  that were never fetched come first, and ties keep the order of the caller's list. With n
  accounts asked every ``interval / n`` they take turns, so updates arrive every 15/n minutes
  while each account is still fetched at most once per 15 minutes. An account that must not
  take part (it waits for a new sign-in) is simply not passed in.
- Timestamps are wall-clock UTC, because a monotonic clock does not survive a restart. A
  stored timestamp in the future (the clock was set back) is pulled back to the time it is
  first seen, so it blocks the account for at most one interval instead of until the clock
  catches up.

Limits:

- If the clock jumps forward, the last fetch looks older than it is and the account may fetch
  early once. Nothing that only knows wall-clock time can tell this apart from a long pause.
- If the store is lost, corrupt or of an unknown version, the budget starts empty with a
  warning, and every account may fetch once right away. Setup never fails because of it.
- A record is written about ``SAVE_DELAY`` after the fetch started, and on shutdown. If Home
  Assistant dies within that window, that one fetch is forgotten.
- The budget is per Home Assistant instance. Two instances, or another client, using the same
  Apple account know nothing of each other.

Accounts are identified by an opaque string chosen by the caller, see ``account_budget_id``.
The store holds nothing but these ids and timestamps. Ids whose last fetch is older than
``RETENTION`` are dropped, so accounts that were removed do not stay in the file forever.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypedDict, override

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from collections.abc import Iterable

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "findmy.fetch_budget"
STORAGE_VERSION = 1

# The spacing Apple tolerates for one account, see the README.
MIN_FETCH_INTERVAL = timedelta(minutes=15)

# After this long a record cannot block anything any more and only ranks the account in pick(),
# where a record this old and no record at all mean the same: it waited longest.
RETENTION = timedelta(days=30)

# Seconds between recording a fetch and writing it. Short, so the record is on disk before a
# fetch that brings Home Assistant down could restart it; long enough to bundle the records of
# a burst (several entries set up at once) into one write.
SAVE_DELAY = 1.0

# Ranks never-fetched accounts before any real timestamp in pick().
_NEVER = datetime.min.replace(tzinfo=UTC)


def account_budget_id(apple_id: str) -> str:
    """The id an Apple account is known by in the budget.

    Derived from the Apple ID rather than from a config entry, so that removing and adding an
    account again does not reset its budget. Hashed, so the store holds no e-mail address. This
    is a pseudonym, not anonymity: whoever knows the Apple ID can compute it.
    """
    normalized = apple_id.strip().casefold()
    return hashlib.sha256(f"findmy-fetch-budget:{normalized}".encode()).hexdigest()


def _require_aware(now: datetime) -> datetime:
    """The time in UTC; naive times are a programming error, not something to guess about."""
    if now.tzinfo is None:
        msg = "FetchBudget needs timezone-aware datetimes"
        raise ValueError(msg)
    return now.astimezone(UTC)


class FetchBudget:
    """When each account last fetched, and which one may fetch next.

    Pure logic: every decision takes the current time as a parameter. Not thread-safe; meant
    to be used from the event loop only.
    """

    def __init__(
        self,
        interval: timedelta = MIN_FETCH_INTERVAL,
        retention: timedelta = RETENTION,
    ) -> None:
        if interval <= timedelta(0):
            msg = "interval must be positive"
            raise ValueError(msg)
        if retention < interval:
            # Pruning a record that still blocks its account would lift the limit.
            msg = "retention must not be shorter than interval"
            raise ValueError(msg)
        self.interval: timedelta = interval
        self.retention: timedelta = retention
        self._last_fetch: dict[str, datetime] = {}

    def _changed(self) -> None:
        """Called after the records changed; the persistent subclass saves them."""

    def _last(self, account_id: str, now: datetime) -> datetime | None:
        """The last fetch of an account, with a timestamp in the future pulled back to now.

        A timestamp in the future means the clock was set back. Treating it as "fetched just
        now" blocks the account for one interval. Leaving it would block it until the clock
        caught up, and computing from min(last, now) would move the block along with the
        clock and never release it.
        """
        last = self._last_fetch.get(account_id)
        if last is None or last <= now:
            return last
        _LOGGER.warning(
            "Last fetch %s is after now (%s), the clock was set back; counting it as now",
            last.isoformat(),
            now.isoformat(),
        )
        self._last_fetch[account_id] = now
        self._changed()
        return now

    def last_fetch(self, account_id: str) -> datetime | None:
        """When a fetch of the account last started, or None if none is known."""
        return self._last_fetch.get(account_id)

    def next_allowed(self, account_id: str, now: datetime) -> datetime:
        """The earliest time, not before ``now``, at which the account may fetch."""
        now = _require_aware(now)
        last = self._last(account_id, now)
        if last is None:
            return now
        return max(now, last + self.interval)

    def is_allowed(self, account_id: str, now: datetime) -> bool:
        """Whether the account may fetch at ``now``; exactly one interval after is allowed."""
        now = _require_aware(now)
        return self.next_allowed(account_id, now) <= now

    def pick(self, account_ids: Iterable[str], now: datetime) -> str | None:
        """The account that may fetch now and waited longest, or None if none may.

        Never-fetched accounts come first. Ties, including several never-fetched accounts,
        keep the order of ``account_ids``, so the caller's order decides and stays stable.
        """
        now = _require_aware(now)
        best: str | None = None
        best_last = now
        for account_id in dict.fromkeys(account_ids):
            if not self.is_allowed(account_id, now):
                continue
            last = self._last(account_id, now) or _NEVER
            # Strictly earlier, so the first of equal accounts wins.
            if best is None or last < best_last:
                best, best_last = account_id, last
        return best

    def next_due(self, account_ids: Iterable[str], now: datetime) -> datetime | None:
        """The earliest time, not before ``now``, at which any of the accounts may fetch.

        None if no account is given: nothing will ever be due.
        """
        now = _require_aware(now)
        return min((self.next_allowed(account_id, now) for account_id in account_ids), default=None)

    def record_fetch(self, account_id: str, now: datetime) -> None:
        """Record that a fetch of the account starts now.

        Called before the request goes out, so that a fetch that crashes, hangs or times out
        still counts. Also prunes records that ran out of retention.
        """
        now = _require_aware(now)
        self._last_fetch[account_id] = now
        _ = self._prune(now)
        self._changed()

    def forget(self, account_id: str) -> None:
        """Drop the record of an account, which may then fetch at once.

        For an account that is gone for good. An account that is only removed and added again
        should keep its record, which is why ids come from the Apple ID.
        """
        if self._last_fetch.pop(account_id, None) is not None:
            self._changed()

    def prune(self, now: datetime) -> None:
        """Drop records older than ``retention`` and pull timestamps in the future back to now."""
        now = _require_aware(now)
        if self._prune(now):
            self._changed()

    def _prune(self, now: datetime) -> bool:
        changed = False
        for account_id, last in list(self._last_fetch.items()):
            if last > now:
                # Also for ids nobody asks about any more, which would otherwise stay forever.
                self._last_fetch[account_id] = now
                changed = True
            elif now - last > self.retention:
                del self._last_fetch[account_id]
                changed = True
        return changed

    def as_dict(self) -> dict[str, datetime]:
        """A copy of the records, for diagnostics and tests."""
        return dict(self._last_fetch)


class _StoredAccount(TypedDict):
    last_fetch: str


class _StoredData(TypedDict):
    accounts: dict[str, _StoredAccount]


def _parse_records(data: object) -> tuple[dict[str, datetime], int]:
    """The records in loaded store data, and how many entries had to be skipped."""
    if not isinstance(data, Mapping):
        raise TypeError(type(data).__name__)
    accounts = data.get("accounts")  # pyright: ignore[reportUnknownMemberType]
    if not isinstance(accounts, Mapping):
        raise TypeError(type(accounts).__name__)

    records: dict[str, datetime] = {}
    skipped = 0
    for account_id, entry in accounts.items():  # pyright: ignore[reportUnknownVariableType]
        try:
            raw = entry["last_fetch"]  # pyright: ignore[reportIndexIssue, reportUnknownVariableType]
            if not isinstance(account_id, str) or not isinstance(raw, str):
                raise TypeError  # noqa: TRY301
            records[account_id] = _require_aware(datetime.fromisoformat(raw))
        except KeyError, TypeError, ValueError:
            skipped += 1
    return records, skipped


class PersistentFetchBudget(FetchBudget):
    """A ``FetchBudget`` that survives restarts in ``.storage/findmy.fetch_budget``.

    Call ``async_load`` once before the first decision. Until then the budget only knows what
    was recorded in this run, which is never less strict than knowing nothing.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        interval: timedelta = MIN_FETCH_INTERVAL,
        retention: timedelta = RETENTION,
    ) -> None:
        super().__init__(interval, retention)
        self._store: Store[_StoredData] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        # A second caller waits for the first load instead of deciding on half the records.
        self._load_lock: asyncio.Lock = asyncio.Lock()
        self._loaded: bool = False
        self._changed_before_load: bool = False

    async def async_load(self) -> None:
        """Load the records once. A broken store starts the budget empty, with a warning."""
        async with self._load_lock:
            if self._loaded:
                return
            try:
                await self._async_load_records()
            finally:
                self._loaded = True
                if self._changed_before_load:
                    self._changed()

    async def _async_load_records(self) -> None:
        try:
            stored = await self._store.async_load()
        except Exception:
            # A newer or older version without migration, unreadable file, anything else. The
            # budget only protects against fetching early; losing it must not stop setup.
            # Starting empty lets every account fetch once right away.
            _LOGGER.warning(
                "Could not load the fetch budget from %s, starting empty",
                self._store.path,
                exc_info=True,
            )
            return
        if stored is None:
            # No file yet; a corrupt file has been moved aside and reported by the Store.
            return

        try:
            records, skipped = _parse_records(stored)
        except TypeError as err:
            _LOGGER.warning(
                "The fetch budget in %s has an unexpected layout (%s), starting empty",
                self._store.path,
                err,
            )
            return
        if skipped:
            _LOGGER.warning(
                "Skipped %i unreadable entries of the fetch budget in %s",
                skipped,
                self._store.path,
            )

        # Something recorded before the load is newer than what was on disk; keep the later.
        for account_id, last in records.items():
            known = self._last_fetch.get(account_id)
            if known is None or last > known:
                self._last_fetch[account_id] = last
        self.prune(dt_util.utcnow())

    @override
    def _changed(self) -> None:
        if not self._loaded:
            # A pending save would be what Store.async_load returns instead of the file, and
            # the records on disk would be lost. Saved once the load has merged them.
            self._changed_before_load = True
            return
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    def _data_to_save(self) -> _StoredData:
        return {
            "accounts": {
                account_id: {"last_fetch": last.isoformat()}
                for account_id, last in self._last_fetch.items()
            }
        }
