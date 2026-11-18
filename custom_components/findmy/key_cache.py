"""Rolling-key accessory that keeps its derived key pairs between uses.

FindMy.py derives a new :class:`KeyPair` (one elliptic-curve multiplication) for every key index
on every call. A location fetch walks a week of indices per accessory on the event loop, three
keys per index, and the local Bluetooth matching walks two days of them every 15 minutes. Between
two such passes a single index is new; everything else was derived again and again.

The accessory below derives every key once, hands out key pairs whose public key and hash are
stored, and can be warmed up from a worker thread so that the event loop only reads finished keys.

Scope: this module only holds keys in memory. It stores nothing on disk (keys are derived again
after a restart), talks to nobody, and does not decide when to warm up; the coordinator and the
device tracker do that. The proper home of such a cache is FindMy.py itself (FindMy.py #285). Once
the library keeps its key pairs, this module can be removed.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, final, override

from findmy import FindMyAccessory, KeyPair

if TYPE_CHECKING:
    from findmy import KeyPairType

_LOGGER = logging.getLogger(__name__)

# Primary indices that share one pair of secondary keys: a day of 15-minute indices. Mirrors
# FindMyAccessory._secondary_keys_at, which only looks at `index // 96`.
_SECONDARY_KEY_SPAN = 96

# Number of key indices kept per accessory (15 minutes each, so about 85 days). An accessory
# that was not located for longer than this has a larger search window; the indices beyond the
# limit are derived on demand.
MAX_CACHED_INDICES = 8192

# Number of newest key indices that are searched for an accessory. FindMy.py searches every
# index since the last alignment, which never ends for an accessory that is not located any more
# (empty battery, lost): after years that is ~100,000 indices, i.e. hundreds of requests to
# Apple per fetch and more than a minute of key derivation per local refresh. Such an accessory
# is only searched within this many indices (about 83 days) below the highest possible index;
# the margin to MAX_CACHED_INDICES keeps the whole searched range in the cache.
MAX_SEARCH_INDICES = MAX_CACHED_INDICES - 192

# Pairs of secondary keys kept per accessory: enough for every cached index, plus the day that
# is partly inside the window at either end.
_MAX_CACHED_SECONDARY_SPANS = MAX_CACHED_INDICES // _SECONDARY_KEY_SPAN + 2

# Guards every derivation and every change of a cache. FindMy.py's key generators keep state
# (the chain of secrets) that must not be advanced by two threads at once. One lock for all
# accessories, because deriving is CPU-bound and holds the GIL: several worker threads deriving
# at the same time, as after a restart with many accessories, would starve the event loop. The
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

    @override
    def get_min_index(self, dt: datetime) -> int:
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
            key_1, key_2 = self._secondary_keys_at(ind)
            keys = (CachedKeyPair.from_keypair(key_1), CachedKeyPair.from_keypair(key_2))
            self._secondary_cache[span] = keys
            _evict_farthest(self._secondary_cache, span, _MAX_CACHED_SECONDARY_SPANS)
        return keys
