"""Rolling-key accessory that keeps its derived key pairs between uses.

FindMy.py derives a new :class:`KeyPair` (one elliptic-curve multiplication) for every key index
on every call. A location fetch walks a week of indices per accessory on the event loop, three
keys per index, and the local Bluetooth matching walks two days of them every 15 minutes. Between
two such passes a single index is new; everything else was derived again and again.

The accessory below derives every key once and hands out key pairs whose public key and hash are
stored. For a location fetch it cuts search windows: fixed ranges of indices with finished keys.

It also limits how many key indices are searched per fetch, and plans the catch-up windows that
make up for that limit (see MAX_SEARCH_INDICES and CatchUpWindow).

Scope: this module only holds keys in memory and decides which indices to look at. It writes
nothing to disk itself (keys are derived again after a restart, and a catch-up starts over),
talks to nobody, and does not decide when to derive or to fetch; the coordinator and the device
tracker do that. The proper home of the cache is FindMy.py itself (FindMy.py #285). Once the
library keeps its key pairs, that part of this module can be removed.

The secret chain
----------------
FindMy.py derives the secret of key index ``i`` by applying a key derivation function ``i`` times
to the initial secret of the accessory, and keeps some of these secrets in its key generator so
that it walks the chain once per run. After a restart it walks it again from index 0: 96 steps
per day since pairing, close to half a second on a desktop for an accessory paired two and a half
years ago, several times that on a small server. :meth:`CachedFindMyAccessory.chain_checkpoints`
hands out a few of those secrets, which key_checkpoints.py keeps across restarts, and
:meth:`CachedFindMyAccessory.restore_chain_checkpoints` gives them back to a new generator, which
then walks from there.

FindMy.py has no public way to do either, so both use private attributes of its key generator.
:func:`_chain_access_works` checks once per run, on a throwaway accessory, that they still mean
what this module assumes; if not, no checkpoint is read or restored, and the chain is walked
from 0 as before, only slower. Each checkpoint carries a tag, an HMAC keyed with the initial
secret of its chain over the chain, the index and the secret: a checkpoint that does not belong
to the accessory, its chain or its index (a damaged or edited file, a record under the wrong
entry) is never seeded, since it would make every later key of the accessory wrong without
anything noticing. The checkpoints are also tied to the FindMy.py version that made
them (key_checkpoints.py).

Sharing the interpreter
-----------------------
Key derivation is CPU-bound and holds the interpreter lock while it runs. Next to it, the event
loop gets the lock back only every few milliseconds, each time it gave it up for a socket call:
a loop that serves many small requests falls to a few dozen per second. Derivation therefore
runs in short slices and pauses after every few milliseconds of work (``_PACER``), and the chain
is walked in slices as well. That makes a cold start a little slower and keeps the event loop at
a fair share of its usual throughput; tests/test_fetch_blocking.py measures both.

Threads
-------
Three kinds of threads use an accessory:

* Home Assistant's event loop: Bluetooth matching and realignment (device_tracker.py), planning
  of catch-up windows, serialising the accessory into its config entry.
* Executor threads: everything that derives keys (Bluetooth candidates, search windows).
* The FindMy worker thread (worker.py), on which FindMy.py fetches and decrypts reports.

The chosen model: **the worker thread never gets an accessory.** It gets a
:class:`SearchWindow` per accessory, an object made for that one fetch with its own key table,
fixed bounds and its own copy of the alignment. FindMy.py reads keys from the window and reports
alignments to it; nothing the event loop uses changes under it, and nothing the worker uses
changes under the worker. When the fetch is over, the coordinator applies the alignment a window
found to the accessory, on the event loop. The key pairs themselves are shared between a window
and the cache, which is safe because they are never changed after they were derived.

Why not share the accessory and lock it: FindMy.py's fetch asks an accessory for its bounds
again on every index and realigns it between two requests, while the device tracker reads the
alignment, updates it and compares the result in one synchronous stretch. Each single access can
be made safe with a lock, the sequences cannot, short of holding a lock across awaits. With
windows there is nothing to get wrong: the alignment of an accessory is written on the event
loop only, exactly as it was before the worker existed.

What is left to guard, and how:

* The caches and FindMy.py's key generators, used by executor threads: ``_DERIVE_LOCK``, held
  for one index, or one slice of the secret chain, at a time, and through the pause that may
  follow. Finished keys are read without it.
* The alignment, a pair of attributes that FindMy.py writes one after the other. It is written
  on the event loop and read in executor threads, which could see a new date with an old index.
  A per-accessory lock covers the write and every read of the pair; it is held for a few
  attribute accesses and never across an await.
* The catch-up cursor: event loop only, no lock.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple, cast, final, override

from findmy import FindMyAccessory, KeyPair, RollingKeyPairSource

if TYPE_CHECKING:
    import io
    from collections.abc import Callable, Generator, Mapping
    from pathlib import Path

    from findmy import FindMyAccessoryMapping, KeyPairType

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
# The local Bluetooth matching cannot wait for a report: it may run without any Apple account.
# It gets the keys between the last alignment and the top window from local_gap_keys, see
# MAX_LOCAL_GAP_INDICES.
MAX_SEARCH_INDICES = 7 * 96

# How far above its last alignment an accessory is still recognised locally once its index fell
# below the top window. The case this is for: a battery that was empty for more than a week.
# The index stood still meanwhile and goes on from where it stopped, which is at or above the
# index of the last sighting. Without these keys such an accessory would never be heard again
# unless a location report realigns it, and a setup without an Apple account has no reports.
#
# 90 days of indices above the alignment: at most 8,640 advertisement keys per accessory, kept
# as plain bytes (about 1.5 MB), derived once and only for an accessory that has such a gap.
# An accessory further ahead of its alignment than that is found by a location report only.
MAX_LOCAL_GAP_INDICES = 90 * 96

# Indices of that gap derived per call of local_gap_keys, lowest first. One call per local
# refresh (every 15 minutes) spreads the derivation of a long gap over a few hours; the week
# right above the alignment, where a tag with a fresh battery is most likely, comes first.
LOCAL_GAP_STEP = MAX_SEARCH_INDICES

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
# accessories, because deriving is CPU-bound: several executor threads deriving at the same time,
# as after a restart with many accessories, would compete with the event loop for the CPU, and
# the pauses of one would be the working time of another. It is held for one index, or one slice
# of the secret chain (_CHAIN_SLICE), and through the pause that may follow (_PACER). Nothing on
# the event loop derives keys, so the loop never waits for it.
_DERIVE_LOCK = threading.Lock()

# Steps of the secret chain walked per slice: about a millisecond on a desktop.
_CHAIN_SLICE = 256

# Work done under _DERIVE_LOCK before a pause, and the length of the pause, in seconds. The
# pause hands the interpreter lock to the event loop, which otherwise waits up to a switch
# interval (5 ms) for it every time it gave it up for a socket call. Measured in
# tests/test_fetch_blocking.py: a third of the time as pauses keeps the loop at a fair share
# of its throughput, at the cost of a slower cold start.
_WORK_SLICE = 0.002
_PAUSE = 0.001

# Distance between two checkpoints of the secret chain that are kept across restarts, in key
# indices: a week. Walking from the nearest one below takes a few milliseconds.
_CHECKPOINT_SPACING = 7 * 96

# Names of the two secret chains of an accessory, and the attribute of FindMy.py's accessory
# that holds the key generator of each.
PRIMARY_CHAIN = "primary"
SECONDARY_CHAIN = "secondary"
_GENERATOR_ATTRIBUTES = {PRIMARY_CHAIN: "_primary_gen", SECONDARY_CHAIN: "_secondary_gen"}
_SECRET_LENGTH = 32

# Length of the tag that binds a checkpoint to its accessory, chain and index, in bytes.
CHECKPOINT_TAG_LENGTH = 16


class ChainCheckpoint(NamedTuple):
    """A secret of a key chain, and the tag that says whose chain and which index it is."""

    secret: bytes
    tag: bytes


type ChainCheckpoints = dict[str, dict[int, ChainCheckpoint]]


def _on_event_loop() -> bool:
    try:
        _ = asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@final
class _Pacer:
    """Pauses derivation after every _WORK_SLICE of work. Used with _DERIVE_LOCK held."""

    def __init__(self) -> None:
        self._busy = 0.0
        self._last_end = 0.0

    def account(self, started: float) -> None:
        """Count a piece of work that began at ``started`` and ends now; pause if it is time."""
        now = time.perf_counter()
        if started - self._last_end > _PAUSE:
            # The lock was free for a while since the last piece; the loop had its turn.
            self._busy = 0.0
        self._busy += now - started
        self._last_end = now
        # A test, or anything else, that derives on an event loop must not sleep on it.
        if self._busy < _WORK_SLICE or _on_event_loop():
            return
        # Still holding _DERIVE_LOCK, so that no other thread derives in the meantime.
        _sleep(_PAUSE)
        self._busy = 0.0
        self._last_end = time.perf_counter()


_PACER = _Pacer()
# Replaced by tests that count the pauses.
_sleep = time.sleep


@contextmanager
def _deriving() -> Generator[None]:
    """Hold _DERIVE_LOCK for one piece of derivation, and pause afterwards if it is time."""
    with _DERIVE_LOCK:
        started = time.perf_counter()
        yield
        _PACER.account(started)


# --- Access to the secret chain of FindMy.py's key generators -------------------------------
#
# Private attributes of FindMy.py 0.10.2 (findmy/accessory.py, _AccessoryKeyGenerator): the
# generator keeps chain secrets by index in the dicts of `_sk_caches`, adds one with
# `_update_caches(index, secret)` (only at indices its cache tiers keep: multiples of 4), and
# walks the chain with `_get_sk(index)` from the highest secret it holds below. _SecretChain is
# the one place that touches them. It is only made for a generator that has all three, and
# _chain_access_works checks once per run that they still mean this.


@final
@dataclass(frozen=True, slots=True)
class _SecretChain:
    """The parts of one of FindMy.py's key generators that checkpoints need."""

    walk_to: Callable[[int], object]
    add: Callable[[int, bytes], object]
    caches: list[object]

    @classmethod
    def of(cls, accessory: FindMyAccessory, chain: str) -> _SecretChain | None:
        """The secret chain of an accessory, or None if FindMy.py keeps it differently."""
        generator: object = getattr(accessory, _GENERATOR_ATTRIBUTES[chain], None)
        walk_to: object = getattr(generator, "_get_sk", None)
        add: object = getattr(generator, "_update_caches", None)
        caches: object = getattr(generator, "_sk_caches", None)
        if not callable(walk_to) or not callable(add) or not isinstance(caches, list):
            return None
        return cls(
            walk_to=cast("Callable[[int], object]", walk_to),
            add=cast("Callable[[int, bytes], object]", add),
            caches=cast("list[object]", caches),
        )

    def known(self) -> dict[int, bytes] | None:
        """Every secret the generator holds, by index; None if it holds them differently."""
        secrets: dict[int, bytes] = {}
        for cache in self.caches:
            if not isinstance(cache, dict):
                return None
            for ind, secret in list(cast("dict[object, object]", cache).items()):
                if not isinstance(ind, int) or not isinstance(secret, bytes):
                    return None
                secrets[ind] = secret
        return secrets

    def seed(self, secrets: Mapping[int, bytes]) -> bool:
        """Give the generator secrets; False if it did not keep every one of them."""
        for ind, secret in sorted(secrets.items()):
            _ = self.add(ind, secret)
        known = self.known()
        return known is not None and all(
            known.get(ind) == secret for ind, secret in secrets.items()
        )


@functools.cache
def _chain_access_works() -> bool:
    """Whether checkpoints can be read and restored with this version of FindMy.py.

    Compares, on throwaway accessories with fixed keys, a key derived from a restored checkpoint
    with the same key derived by FindMy.py from index 0, and checks that a wrong checkpoint does
    change the result, so that the generator really continues from what it was given.

    Must not be called with _DERIVE_LOCK held: the check derives a few keys itself and takes
    the lock for that, like every other derivation. After a start several executor threads ask
    at the same moment, before the first answer is remembered; without the lock each of them
    would run the check next to the others.
    """
    with _DERIVE_LOCK:
        try:
            return CachedFindMyAccessory.probe_chain_access()
        except Exception:
            _LOGGER.debug("Cannot use FindMy.py's key generator for checkpoints", exc_info=True)
            return False


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

    Everything that reads keys or the alignment may be called from the event loop and from
    executor threads at the same time. The alignment is only updated from the event loop, and
    the accessory is never handed to the FindMy worker thread: a fetch searches the windows cut
    by :meth:`search_window` and :meth:`catch_up_window` instead. See the module docstring.
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
        # Makes the alignment, which FindMy.py keeps in two attributes, one value for readers
        # in other threads. Reentrant, because the bounds below are computed from one another.
        self._alignment_lock = threading.RLock()
        # Advertisement keys between the last alignment and the top window, for the local
        # Bluetooth matching only; see local_gap_keys. Plain bytes instead of key pairs, since
        # nothing ever decrypts with them.
        self._gap_lock = threading.Lock()
        self._gap_anchor: int | None = None
        self._gap_primary: dict[int, bytes] = {}
        self._gap_secondary: dict[int, tuple[bytes, bytes]] = {}
        # Highest index of the primary secret chain known to have been reached, from a restored
        # checkpoint or a walk; see _walk_primary_chain.
        self._primary_reach = 0

    @property
    def alignment(self) -> tuple[datetime, int]:
        """The moment and the key index of the latest sighting the accessory was aligned to."""
        with self._alignment_lock:
            return self._alignment_date, self._alignment_index

    @override
    def update_alignment(self, dt: datetime, index: int) -> None:
        # Must be called on the event loop; see the module docstring.
        with self._alignment_lock:
            super().update_alignment(dt, index)

    @override
    def get_max_index(self, dt: datetime) -> int:
        with self._alignment_lock:
            return super().get_max_index(dt)

    @override
    def get_min_index(self, dt: datetime) -> int:
        with self._alignment_lock:
            # Never further down than the search limit allows, see MAX_SEARCH_INDICES.
            newest = self.get_max_index(datetime.now(tz=UTC))
            return max(super().get_min_index(dt), newest - MAX_SEARCH_INDICES + 1)

    @override
    def to_json(self, path: str | Path | io.TextIOBase | None = None, /) -> FindMyAccessoryMapping:
        # The lock keeps the alignment written to the config entry a pair that existed. Nothing
        # in this integration passes a path, so no file is written while it is held.
        with self._alignment_lock:
            return super().to_json(path)

    def _keys(self, ind: int) -> tuple[CachedKeyPair, ...]:
        """The finished keys of an index, derived first if nobody asked for them before."""
        # Reading one dict entry is atomic, so finished keys are handed out without the lock.
        # This is the only path taken once a range was derived.
        keys = self._key_cache.get(ind)
        if keys is None:
            self._walk_primary_chain(ind)
            with _deriving():
                keys = self._key_cache.get(ind)
                if keys is None:
                    keys = (self._derive_primary(ind), *self._secondary_keys(ind))
                    self._key_cache[ind] = keys
                    _evict_farthest(self._key_cache, ind, MAX_CACHED_INDICES)
        return keys

    @staticmethod
    def probe_chain_access() -> bool:
        """The check behind _chain_access_works; raises whatever a changed FindMy.py raises."""

        def throwaway() -> CachedFindMyAccessory:
            return CachedFindMyAccessory(
                master_key=bytes(range(1, 29)),
                skn=bytes(range(32)),
                sks=bytes(range(32, 64)),
                paired_at=datetime(2020, 1, 1, tzinfo=UTC),
            )

        reference = throwaway()
        expected = reference._primary_key_at(9).private_key_bytes  # noqa: SLF001
        chain = _SecretChain.of(reference, PRIMARY_CHAIN)
        secrets = None if chain is None else chain.known()
        if not secrets or 8 not in secrets:  # noqa: PLR2004  # FindMy.py keeps every 4th index
            return False

        restored = throwaway()
        restored_chain = _SecretChain.of(restored, PRIMARY_CHAIN)
        if restored_chain is None or not restored_chain.seed({8: secrets[8]}):
            return False
        if restored._primary_key_at(9).private_key_bytes != expected:  # noqa: SLF001
            return False

        wrong = throwaway()
        wrong_chain = _SecretChain.of(wrong, PRIMARY_CHAIN)
        if wrong_chain is None or not wrong_chain.seed({8: bytes(_SECRET_LENGTH)}):
            return False
        return wrong._primary_key_at(9).private_key_bytes != expected  # noqa: SLF001

    def _walk_primary_chain(self, ind: int) -> None:
        """Walk the primary secret chain up to an index in slices of _CHAIN_SLICE steps.

        FindMy.py would walk it in one go on the first key it is asked for, holding the
        interpreter lock for as long as that takes. Without access to its generator (see
        _chain_access_works) that is what happens.
        """
        reach = self._primary_reach
        if ind - reach <= _CHAIN_SLICE or not _chain_access_works():
            return
        chain = _SecretChain.of(self, PRIMARY_CHAIN)
        if chain is None:
            return
        for step in range(reach + _CHAIN_SLICE, ind, _CHAIN_SLICE):
            with _deriving():
                _ = chain.walk_to(step)
        # Several threads may walk at once; each walk is correct, only the record is shared.
        self._primary_reach = max(self._primary_reach, ind)

    def chain_checkpoints(self) -> ChainCheckpoints:
        """Secrets of both chains that let a new object of this accessory skip most of them.

        Taken from what FindMy.py's key generators hold: the highest secret at or below the
        lowest index a fetch or the Bluetooth matching can ask for (a week below the last
        alignment), and above it one secret per _CHECKPOINT_SPACING indices that were reached.
        Empty without access to the generators (see _chain_access_works).

        Meant to be run in an executor thread.
        """
        if not _chain_access_works():
            return {}
        with self._alignment_lock:
            anchor = self._alignment_index
        floor = max(0, anchor - MAX_SEARCH_INDICES - 1)
        # Secondary secrets advance once per _SECONDARY_KEY_SPAN primary indices.
        floors = {PRIMARY_CHAIN: floor, SECONDARY_CHAIN: floor // _SECONDARY_KEY_SPAN + 1}
        checkpoints: ChainCheckpoints = {}
        with _DERIVE_LOCK:
            for chain, low in floors.items():
                secret_chain = _SecretChain.of(self, chain)
                secrets = None if secret_chain is None else secret_chain.known()
                if not secrets:
                    continue
                chosen = [ind for ind in secrets if ind > low and ind % _CHECKPOINT_SPACING == 0]
                below = max((ind for ind in secrets if 0 < ind <= low), default=None)
                if below is not None:
                    chosen.append(below)
                if chosen:
                    checkpoints[chain] = {
                        ind: ChainCheckpoint(
                            secrets[ind], self._checkpoint_tag(chain, ind, secrets[ind])
                        )
                        for ind in sorted(chosen)
                    }
        return checkpoints

    def _checkpoint_tag(self, chain: str, ind: int, secret: bytes) -> bytes:
        """The tag of a chain secret: an HMAC keyed with the initial secret of the chain.

        The initial secrets (SKN, SKS) are in the config entry already. Nobody without them can
        make a tag, and a tag proves without walking the chain that the secret was stored for
        this accessory, this chain and this index.
        """
        key = self.skn if chain == PRIMARY_CHAIN else self.sks
        message = chain.encode() + b"\0" + ind.to_bytes(8, "big") + secret
        return hmac.new(key, message, hashlib.sha256).digest()[:CHECKPOINT_TAG_LENGTH]

    def _checkpoints_belong_here(
        self, checkpoints: Mapping[str, Mapping[int, ChainCheckpoint]]
    ) -> bool:
        """Whether every checkpoint is a secret of this accessory's chain at its index."""
        for chain, secrets in checkpoints.items():
            if chain not in _GENERATOR_ATTRIBUTES:
                return False
            for ind, checkpoint in secrets.items():
                if ind <= 0 or len(checkpoint.secret) != _SECRET_LENGTH:
                    return False
                expected = self._checkpoint_tag(chain, ind, checkpoint.secret)
                if not hmac.compare_digest(checkpoint.tag, expected):
                    return False
        return True

    def restore_chain_checkpoints(
        self, checkpoints: Mapping[str, Mapping[int, ChainCheckpoint]]
    ) -> bool:
        """Give the key generators secrets that chain_checkpoints of an earlier object returned.

        Must be called before the accessory is used by anything else. Returns whether they were
        taken; otherwise nothing changes and the chain is walked from the pairing. That is the
        case without access to the generators, and for checkpoints of which any one does not
        verify (see _checkpoint_tag): all of them are dropped then, with a warning, since a
        single wrong secret would make every key above it wrong.
        """
        if not checkpoints:
            return False
        if not self._checkpoints_belong_here(checkpoints):
            _LOGGER.warning(
                "The stored key chain checkpoints of %s do not belong to it; ignoring them",
                self.name,
            )
            return False
        if not _chain_access_works():
            return False
        taken = False
        for chain, stored in checkpoints.items():
            secret_chain = _SecretChain.of(self, chain)
            valid = {ind: checkpoint.secret for ind, checkpoint in stored.items()}
            if secret_chain is None or not valid:
                continue
            if not secret_chain.seed(valid):
                _LOGGER.debug("FindMy.py did not keep the checkpoints of %s", self.name)
                continue
            taken = True
            if chain == PRIMARY_CHAIN:
                self._primary_reach = max(self._primary_reach, *valid)
        return taken

    @override
    def keys_at(self, ind: int) -> set[KeyPair]:
        if ind < 0:
            return set()
        # A new set per call: the caller owns what it gets.
        return set(self._keys(ind))

    def search_window(self, now: datetime) -> SearchWindow:
        """Cut the window a location fetch searches: the indices possibly in use this week.

        Derives whatever the cache does not hold yet, which is everything after a restart and a
        single index 15 minutes after the previous fetch. Meant to be run in an executor
        thread; the returned window is then handed to the FindMy worker thread.
        """
        with self._alignment_lock:
            alignment = self.alignment
            highest = self.get_max_index(now)
            lowest = self.get_min_index(now - REPORT_WINDOW)

        derived = 0
        keys: dict[int, tuple[CachedKeyPair, ...]] = {}
        # Newest first, so that an eviction, if the range ever outgrows the cache, drops the
        # old end. The window keeps its keys either way.
        for ind in range(highest, max(lowest, 0) - 1, -1):
            if ind not in self._key_cache:
                derived += 1
            keys[ind] = self._keys(ind)

        if derived:
            _LOGGER.debug(
                "Derived keys for %i of %i indices of %s (%i indices cached)",
                derived,
                len(keys),
                self.name,
                len(self._key_cache),
            )
        return SearchWindow(self, lowest, highest, keys, alignment)

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

    def local_gap_keys(self, covered_from: int) -> list[tuple[int, bytes, bool]]:
        """Advertisement keys between the last alignment and the lowest index covered anyway.

        For the local Bluetooth matching of an accessory whose index fell below the top window
        (see MAX_LOCAL_GAP_INDICES). ``covered_from`` is the lowest index the caller already has
        keys for. Returns ``(index, advertisement key, is primary)`` for everything derived so
        far, and derives up to LOCAL_GAP_STEP more indices per call, lowest first. Empty for an
        accessory without such a gap, which is the normal case and costs nothing.

        A match realigns the accessory, which moves the top window to where it was heard; the
        gap is then gone, and its keys with it.

        Meant to be run in an executor thread.
        """
        with self._alignment_lock:
            anchor = self._alignment_index

        with self._gap_lock:
            if anchor != self._gap_anchor:
                self._gap_anchor = anchor
                self._gap_primary = {}
                self._gap_secondary = {}

            highest = min(covered_from - 1, anchor + MAX_LOCAL_GAP_INDICES - 1)
            # The top window moves up with the clock and takes over the upper end of the gap.
            for ind in [ind for ind in self._gap_primary if ind > highest]:
                del self._gap_primary[ind]

            derived = 0
            for ind in range(max(anchor, 0), highest + 1):
                if ind in self._gap_primary:
                    continue
                if derived >= LOCAL_GAP_STEP:
                    break
                span = ind // _SECONDARY_KEY_SPAN
                self._walk_primary_chain(ind)
                with _deriving():
                    self._gap_primary[ind] = self._primary_key_at(ind).adv_key_bytes
                    if span not in self._gap_secondary:
                        key_1, key_2 = self._secondary_keys_at(ind)
                        self._gap_secondary[span] = (key_1.adv_key_bytes, key_2.adv_key_bytes)
                derived += 1

            spans = {ind // _SECONDARY_KEY_SPAN for ind in self._gap_primary}
            for span in [span for span in self._gap_secondary if span not in spans]:
                del self._gap_secondary[span]

            if derived:
                _LOGGER.debug(
                    "Derived %i local keys above the last alignment of %s (%i of %i indices)",
                    derived,
                    self.name,
                    len(self._gap_primary),
                    highest - max(anchor, 0) + 1,
                )

            keys = [(ind, key, True) for ind, key in self._gap_primary.items()]
            for span, pair in self._gap_secondary.items():
                # A secondary key belongs to a whole day of indices; it cannot align, so the
                # index it is reported with only has to lie in that day.
                first = min(ind for ind in self._gap_primary if ind // _SECONDARY_KEY_SPAN == span)
                keys.extend((first, key, False) for key in pair)
            return keys

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

        Meant to be run in an executor thread. The keys go into the returned window only: the
        cache of the top window, which every fetch needs, stays as it is.
        """
        keys: dict[int, tuple[CachedKeyPair, ...]] = {}
        secondary: dict[int, tuple[CachedKeyPair, CachedKeyPair]] = {}
        for ind in range(max(lowest, 0), highest + 1):
            self._walk_primary_chain(ind)
            with _deriving():
                span = ind // _SECONDARY_KEY_SPAN
                if span not in secondary:
                    secondary[span] = self._derive_secondary(ind)
                keys[ind] = (self._derive_primary(ind), *secondary[span])
        return CatchUpWindow(self, lowest, highest, keys, self.alignment)

    def catch_up_searched(self, window: CatchUpWindow) -> None:
        """Move the cursor below a window that a fetch has searched.

        Must be called on the event loop. Not called for a fetch that failed, so that its
        window is searched again instead of being skipped until the next sweep.
        """
        self._catch_up_cursor = window.lowest - 1


class SearchWindow(RollingKeyPairSource):
    """A fixed range of key indices of an accessory with finished keys, made for one fetch.

    FindMy.py searches whatever lies between the lowest and the highest index a key source
    reports. A window reports the bounds it was cut with and hands out the keys it was given,
    so searching it derives nothing and reads nothing that another thread may change.

    A window is used by one thread at a time: it is made in an executor thread, searched on the
    FindMy worker thread and read on the event loop when the fetch is over. The accessory it
    was cut from is not touched in between; :attr:`accessory` only says whose reports these are.
    """

    def __init__(
        self,
        accessory: CachedFindMyAccessory,
        lowest: int,
        highest: int,
        keys: dict[int, tuple[CachedKeyPair, ...]],
        alignment: tuple[datetime, int],
    ) -> None:
        self._accessory: CachedFindMyAccessory = accessory
        self._lowest: int = lowest
        self._highest: int = highest
        self._keys: dict[int, tuple[CachedKeyPair, ...]] = keys
        # The alignment of the accessory when the window was cut, moved on by the reports the
        # fetch finds. Always replaced as a whole, so every value read from it is a pair that
        # was really observed.
        self._initial_alignment: tuple[datetime, int] = alignment
        self._alignment: tuple[datetime, int] = alignment

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

    @property
    def found_alignment(self) -> tuple[datetime, int] | None:
        """The alignment the reports of this window lead to, or None if they changed nothing.

        To be passed to ``update_alignment`` of the accessory on the event loop.
        """
        return None if self._alignment == self._initial_alignment else self._alignment

    @override
    def get_min_index(self, dt: datetime) -> int:
        return self._lowest

    @override
    def get_max_index(self, dt: datetime) -> int:
        return self._highest

    @override
    def update_alignment(self, dt: datetime, index: int) -> None:
        # Same rule as FindMyAccessory.update_alignment, which FindMy.py calls once per report
        # and index: only ever forwards. The accessory applies the rule again to the result, so
        # an alignment it got from Bluetooth in the meantime is not overwritten by an older one.
        date, current = self._alignment
        if dt < date or index < current:
            return
        self._alignment = (dt, index)

    @override
    def keys_at(self, ind: int) -> set[KeyPair]:
        return set(self._keys.get(ind, ()))

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._accessory.name!r}, {self._lowest}..{self._highest})"


@final
class CatchUpWindow(SearchWindow):
    """A search window over older key indices, below the range every fetch searches.

    Handed to the fetch next to the top window of the same accessory. The alternative, widening
    the top window now and then, would send its indices a second time and put its keys and
    those of the older range through the same cache.

    A report found here is what closes the gap: applied to the accessory, its alignment moves
    the top window to where the accessory was seen.
    """
