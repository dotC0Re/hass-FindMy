"""Rolling-key accessory that keeps its derived key pairs between uses.

FindMy.py derives a new :class:`KeyPair` (one elliptic-curve multiplication) for every key index
on every call. A location fetch walks a week of indices per accessory on the event loop, three
keys per index, and the local Bluetooth matching walks two days of them every 15 minutes. Between
two such passes a single index is new; everything else was derived again and again.

The accessory below derives every key once, hands out key pairs whose public key and hash are
stored, and can be warmed up from a worker thread so that the event loop only reads finished keys.

It also limits how many key indices are searched per fetch, and plans the catch-up windows that
make up for that limit (see MAX_SEARCH_INDICES and CatchUpWindow).

Scope: this module only holds keys in memory and decides which indices to look at. It stores
nothing on disk (keys are derived again after a restart, and a catch-up starts over), talks to
nobody, and does not decide when to warm up or to fetch; the coordinator and the device tracker
do that. The proper home of the cache is FindMy.py itself (FindMy.py #285). Once the library
keeps its key pairs, that part of this module can be removed.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, final, override

from findmy import FindMyAccessory, KeyPair, RollingKeyPairSource

if TYPE_CHECKING:
    from findmy import KeyPairType

_LOGGER = logging.getLogger(__name__)

# Primary indices that share one pair of secondary keys: a day of 15-minute indices. Mirrors
# FindMyAccessory._secondary_keys_at, which only looks at `index // 96`.
_SECONDARY_KEY_SPAN = 96

# Number of newest key indices that are searched for an accessory, per fetch and per local
# Bluetooth refresh: one week of 15-minute indices. This is what FindMy.py searches anyway for an
# accessory that was located recently, and it only asks Apple for the reports of the last week.
#
# Without the limit, FindMy.py searches every index between the last alignment and the highest
# one the accessory can have reached, because the index may advance slower than the clock. That
# range grows by 96 indices per day without a report. For an accessory that is not located any
# more (empty battery, lost, never seen since the import) it reaches ~100,000 indices after a few
# years. Upstream PR #69 reports ~330 requests to Apple per fetch and ~80 s of key derivation
# per local refresh for such an accessory; with the limit it is three requests.
#
# What the limit costs: only the top of that range is searched on every fetch, where an accessory
# is whose index kept pace with the clock. One whose index fell more than a week behind since its
# last alignment is below that window. That happens: a battery that was empty for more than a
# week, or an accessory that advances its index far less often than every 15 minutes while it
# lies where no Apple device passes by (FindMy.py #274).
#
# Such an accessory is not lost, only found later. The rest of the range is searched one
# catch-up window at a time, see CATCH_UP_INTERVAL and CachedFindMyAccessory.plan_catch_up. A
# report found there realigns the accessory, which puts it back into the top window.
#
# Not covered: the local Bluetooth matching only knows the keys of the top window. An accessory
# below it is not recognised locally until a location report has realigned it.
MAX_SEARCH_INDICES = 7 * 96

# Minimum time between two catch-up windows of one accessory. A window holds at most
# MAX_SEARCH_INDICES indices, which FindMy.py asks for in three requests, so an accessory with a
# gap below its top window costs at most three more requests per interval. One without a gap
# costs nothing. A gap of n indices is searched completely after about n / 672 intervals: three
# hours for an accessory that is a month behind.
CATCH_UP_INTERVAL = timedelta(hours=1)

# FindMy.py looks for the reports of the last week, and derives the lowest index to search from
# the start of that week.
REPORT_WINDOW = timedelta(days=7)

# Number of key indices kept per accessory. Holds the searched range plus a day: the Bluetooth
# matching looks up to 12 hours (48 indices) ahead of the newest index, and the range moves up
# by one index every 15 minutes.
MAX_CACHED_INDICES = MAX_SEARCH_INDICES + _SECONDARY_KEY_SPAN

# Pairs of secondary keys kept per accessory: enough for every cached index, plus the day that
# is partly inside the window at either end.
_MAX_CACHED_SECONDARY_SPANS = MAX_CACHED_INDICES // _SECONDARY_KEY_SPAN + 2

# Guards every derivation and every change of a cache. FindMy.py's key generators keep state
# (the chain of secrets) that must not be advanced by two threads at once. One lock for all
# accessories, because deriving is CPU-bound: several worker threads deriving at the same time,
# as after a restart with many accessories, would compete with the event loop for the CPU. The
# lock is held for a single index only, so the event loop waits one derivation at most.
_DERIVE_LOCK = threading.Lock()


def _evict_farthest[T](cache: dict[int, T], newest: int, limit: int) -> None:
    """Shrink a cache to its limit by dropping the entries farthest from the one just added.

    The indices in use form a window that moves up by one every 15 minutes, and jumps down when
    a report or an advertisement shows that the index of the accessory lags behind the clock.
    Whichever way it moves, the end far from the latest request is what is not needed any more.
    """
    while len(cache) > limit:
        lowest, highest = min(cache), max(cache)
        del cache[lowest if newest - lowest >= highest - newest else highest]


@final
class CachedKeyPair(KeyPair):
    """A key pair that remembers its public key and hash instead of computing them per access.

    A fetch reads these several times per key (set membership, request ids, lookup tables),
    which adds up to a noticeable pause over a week of indices.
    """

    _adv_key_bytes: bytes
    _hashed_adv_key_bytes: bytes
    _hash: int

    def __init__(
        self,
        private_key: bytes,
        key_type: KeyPairType,
        name: str | None = None,
    ) -> None:
        super().__init__(private_key, key_type, name)
        self._remember_public_key()

    @classmethod
    def from_keypair(cls, key: KeyPair) -> CachedKeyPair:
        """Turn a finished key pair into a cached one without deriving its key again.

        KeyPair.__init__ is the expensive part, so it is skipped: the new object takes over the
        state of the given one, including the private key object that was already derived.
        """
        cached = cls.__new__(cls)
        vars(cached).update(vars(key))
        cached._remember_public_key()  # noqa: SLF001  # own class, on the instance made above
        return cached

    def _remember_public_key(self) -> None:
        self._adv_key_bytes = super().adv_key_bytes
        self._hashed_adv_key_bytes = hashlib.sha256(self._adv_key_bytes).digest()
        # Same value as HasHashedPublicKey.__hash__, so plain and cached key pairs that are
        # equal also hash alike.
        self._hash = int.from_bytes(self._hashed_adv_key_bytes, "big")

    @property
    @override
    def adv_key_bytes(self) -> bytes:
        return self._adv_key_bytes

    @property
    @override
    def hashed_adv_key_bytes(self) -> bytes:
        return self._hashed_adv_key_bytes

    @override
    def __hash__(self) -> int:
        return self._hash

    @override
    def __eq__(self, other: object) -> bool:
        if isinstance(other, CachedKeyPair):
            return self._hashed_adv_key_bytes == other._hashed_adv_key_bytes
        return super().__eq__(other)


@final
class CachedFindMyAccessory(FindMyAccessory):
    """A :class:`FindMyAccessory` whose keys are derived once per index.

    :meth:`keys_at` and :meth:`warm_keys` may be called from the event loop and from worker
    threads at the same time. The alignment is still only updated from the event loop. A worker
    thread that reads it in that moment may warm a range that is off by a few indices; the
    missing ones are then derived when they are asked for.
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        master_key: bytes,
        skn: bytes,
        sks: bytes,
        paired_at: datetime,
        name: str | None = None,
        model: str | None = None,
        identifier: str | None = None,
        group_identifier: str | None = None,
        serial_number: str | None = None,
        alignment_date: datetime | None = None,
        alignment_index: int | None = None,
    ) -> None:
        super().__init__(
            master_key=master_key,
            skn=skn,
            sks=sks,
            paired_at=paired_at,
            name=name,
            model=model,
            identifier=identifier,
            group_identifier=group_identifier,
            serial_number=serial_number,
            alignment_date=alignment_date,
            alignment_index=alignment_index,
        )

        # Finished key pairs per key index: the primary key followed by both secondary keys.
        self._key_cache: dict[int, tuple[CachedKeyPair, ...]] = {}
        # Both secondary keys per span of _SECONDARY_KEY_SPAN indices, which all share them.
        self._secondary_cache: dict[int, tuple[CachedKeyPair, CachedKeyPair]] = {}
        # Catch-up below the top window, see plan_catch_up. Only touched on the event loop.
        # Highest index that the catch-up windows of the current sweep have not covered yet.
        self._catch_up_cursor: int | None = None
        self._last_catch_up: datetime | None = None

    @override
    def get_min_index(self, dt: datetime) -> int:
        # Never further down than the search limit allows, see MAX_SEARCH_INDICES.
        newest = self.get_max_index(datetime.now(tz=UTC))
        return max(super().get_min_index(dt), newest - MAX_SEARCH_INDICES + 1)

    @override
    def keys_at(self, ind: int) -> set[KeyPair]:
        if ind < 0:
            return set()

        # Reading one dict entry is atomic, so finished keys are handed out without the lock.
        # This is the only path the event loop takes after a warm-up.
        keys = self._key_cache.get(ind)
        if keys is None:
            with _DERIVE_LOCK:
                keys = self._key_cache.get(ind)
                if keys is None:
                    keys = (self._derive_primary(ind), *self._secondary_keys(ind))
                    self._key_cache[ind] = keys
                    _evict_farthest(self._key_cache, ind, MAX_CACHED_INDICES)
        # A new set per call: the caller owns what it gets.
        return set(keys)

    def warm_keys(self, start: datetime, end: datetime) -> int:
        """Derive the keys possibly in use between two moments; returns the derived index count.

        Meant to be run in a worker thread before the same range is read on the event loop.
        """
        max_index = self.get_max_index(end)
        min_index = max(self.get_min_index(start), max_index - MAX_CACHED_INDICES + 1, 0)

        derived = 0
        # Newest first: this is the order in which a location fetch asks for the keys.
        for ind in range(max_index, min_index - 1, -1):
            if ind not in self._key_cache:
                _ = self.keys_at(ind)
                derived += 1

        if derived:
            _LOGGER.debug(
                "Derived keys for %i indices of %s (%i indices cached)",
                derived,
                self.name,
                len(self._key_cache),
            )
        return derived

    def _derive_primary(self, ind: int) -> CachedKeyPair:
        """Derive the primary key of an index. The caller holds the lock."""
        return CachedKeyPair.from_keypair(self._primary_key_at(ind))

    def _secondary_keys(self, ind: int) -> tuple[CachedKeyPair, CachedKeyPair]:
        """Get both secondary keys of an index. The caller holds the lock.

        FindMy.py derives them for every index although a whole day of indices shares them:
        two thirds of all derivations of a fetch produced the same handful of keys.
        """
        span = ind // _SECONDARY_KEY_SPAN
        keys = self._secondary_cache.get(span)
        if keys is None:
            keys = self._derive_secondary(ind)
            self._secondary_cache[span] = keys
            _evict_farthest(self._secondary_cache, span, _MAX_CACHED_SECONDARY_SPANS)
        return keys

    def _derive_secondary(self, ind: int) -> tuple[CachedKeyPair, CachedKeyPair]:
        """Derive both secondary keys of an index. The caller holds the lock."""
        key_1, key_2 = self._secondary_keys_at(ind)
        return CachedKeyPair.from_keypair(key_1), CachedKeyPair.from_keypair(key_2)

    def plan_catch_up(self, now: datetime) -> tuple[int, int] | None:
        """Pick the next range of indices below the top window to search, if one is due.

        Returns the lowest and highest index of the range, or None when the accessory has no
        gap below its top window or the last catch-up is less than CATCH_UP_INTERVAL ago.

        The gap reaches from the lowest index FindMy.py would search down from the top window.
        A cursor walks it from the top down, one window per call, and starts over at the top
        once it reached the bottom. The accessory keeps advancing meanwhile, by up to one index
        per 15 minutes. Each window therefore overlaps the previous one by as many indices as
        the accessory can have advanced since then, so it cannot slip from a window that is
        still to come into one that was already searched.

        Must be called on the event loop.
        """
        lowest_possible = super().get_min_index(now - REPORT_WINDOW)
        top_floor = self.get_max_index(now) - MAX_SEARCH_INDICES + 1
        gap_top = top_floor - 1
        # A recently located accessory has 672 or 673 indices to search, depending on how the
        # two halves of the week round. A single index is that rounding, not a gap.
        if lowest_possible >= gap_top:
            self._catch_up_cursor = None
            return None

        elapsed = None if self._last_catch_up is None else now - self._last_catch_up
        if elapsed is not None and elapsed < timedelta(0):
            # The clock was set back. What the cursor stood for is unknown now; start over.
            elapsed = None
        if elapsed is not None and elapsed < CATCH_UP_INTERVAL:
            return None

        cursor = self._catch_up_cursor
        highest = gap_top
        if cursor is not None and elapsed is not None and cursor >= lowest_possible:
            # Continue the sweep below the last window, plus the overlap. The gap grows at
            # its top as fast as an accessory can advance, so this stays inside the gap; the
            # bound only guards against an alignment that changed in between.
            highest = min(gap_top, cursor + elapsed // self.interval + 1)

        # Recorded when the window is planned, not when it was searched: a fetch that fails
        # may have sent its requests already, and those count against the interval too.
        self._last_catch_up = now
        return max(lowest_possible, highest - MAX_SEARCH_INDICES + 1), highest

    def catch_up_window(self, lowest: int, highest: int) -> CatchUpWindow:
        """Derive the keys of a range planned by :meth:`plan_catch_up`.

        Meant to be run in a worker thread. The keys go into the returned window only: the
        cache of the top window, which every fetch needs, stays as it is.
        """
        keys: dict[int, tuple[CachedKeyPair, ...]] = {}
        secondary: dict[int, tuple[CachedKeyPair, CachedKeyPair]] = {}
        for ind in range(max(lowest, 0), highest + 1):
            with _DERIVE_LOCK:
                span = ind // _SECONDARY_KEY_SPAN
                if span not in secondary:
                    secondary[span] = self._derive_secondary(ind)
                keys[ind] = (self._derive_primary(ind), *secondary[span])
        return CatchUpWindow(self, lowest, highest, keys)

    def catch_up_searched(self, window: CatchUpWindow) -> None:
        """Move the cursor below a window that a fetch has searched.

        Must be called on the event loop. Not called for a fetch that failed, so that its
        window is searched again instead of being skipped until the next sweep.
        """
        self._catch_up_cursor = window.lowest - 1


@final
class CatchUpWindow(RollingKeyPairSource):
    """A fixed range of older key indices of an accessory, searched like an accessory itself.

    FindMy.py searches whatever lies between the lowest and the highest index a key source
    reports. This view reports the bounds of its range instead of those of the accessory, and is
    handed to the fetch next to the accessory. The alternative, widening the range of the
    accessory itself now and then, would send the top window a second time and put its keys
    and those of the range through the same cache.

    A report found in the range realigns the accessory. Its keys were derived when the window
    was made, so searching it leaves no key derivation to the event loop.
    """

    def __init__(
        self,
        accessory: CachedFindMyAccessory,
        lowest: int,
        highest: int,
        keys: dict[int, tuple[CachedKeyPair, ...]],
    ) -> None:
        self._accessory = accessory
        self._lowest = lowest
        self._highest = highest
        self._keys = keys

    @property
    def accessory(self) -> CachedFindMyAccessory:
        """The accessory this window belongs to; reports found in the window are its reports."""
        return self._accessory

    @property
    def lowest(self) -> int:
        """Lowest key index of the window."""
        return self._lowest

    @property
    def highest(self) -> int:
        """Highest key index of the window."""
        return self._highest

    @override
    def get_min_index(self, dt: datetime) -> int:
        return self._lowest

    @override
    def get_max_index(self, dt: datetime) -> int:
        return self._highest

    @override
    def update_alignment(self, dt: datetime, index: int) -> None:
        # This is what closes the gap: the accessory moves its top window to where it was seen.
        self._accessory.update_alignment(dt, index)

    @override
    def keys_at(self, ind: int) -> set[KeyPair]:
        return set(self._keys.get(ind, ()))

    @override
    def __repr__(self) -> str:
        return f"CatchUpWindow({self._accessory.name!r}, {self._lowest}..{self._highest})"
