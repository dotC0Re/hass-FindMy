"""Run the FindMy library on a dedicated thread with its own asyncio event loop.

Purpose
-------
The FindMy library exposes coroutines, but between its network calls it does a lot of
synchronous work: key derivation (thousands of ``ec.derive_private_key`` calls per
accessory and fetch), report decryption, Anisette emulation and plist parsing. Run on
Home Assistant's event loop, that work stalls Home Assistant for seconds (finding R20).
The coroutines cannot simply be pushed into an executor, because the library's aiohttp
sessions are bound to the event loop they were created on.

:class:`FindMyWorker` therefore owns one daemon thread running one private event loop.
Every library object (accounts, accessories, their aiohttp sessions) is created on that
loop and only ever used there. The caller hands in *factories*, i.e. callables that
produce the coroutine, so that the coroutine and everything it creates come into
existence on the worker thread, and nothing is left behind unstarted if submission
fails.

This module deliberately depends on the standard library only, so it can be tested
without Home Assistant.

Guarantees
----------
* Jobs run strictly one after another, in submission order. Two fetches (or a fetch and
  a login) never overlap, so library objects never see concurrent use.
* Results and exceptions reach the caller unchanged (the very same exception object).
* ``timeout`` limits the job's own run time, not its time in the queue. On expiry the
  job is cancelled on the worker and the caller gets :class:`TimeoutError` once the job
  has unwound. A caller who wants a limit on queueing plus running wraps the call in
  ``asyncio.timeout`` itself; see the next point.
* Cancelling the caller cancels the job on the worker (or removes it from the queue).
  The caller is released right away; the next job starts only after the cancelled one
  has finished unwinding.
* Stopping cancels the running job and all queued jobs, runs the registered stop
  callbacks (e.g. closing aiohttp sessions) on the worker, shuts down async generators
  and the worker loop's default executor, closes the loop and ends the thread. The
  callers of cancelled jobs get :class:`WorkerNotRunningError`, not a stray
  ``CancelledError``. Waiting for the thread never blocks the caller's loop.
* After stopping, every use raises :class:`WorkerNotRunningError`. A worker cannot be
  restarted; create a new one instead.

Limits
------
* The GIL still applies. CPU-bound work on the worker competes with Home Assistant for
  the interpreter and makes its loop slower, but it can no longer freeze it: the
  interpreter switches threads every few milliseconds, so the caller's loop keeps
  running callbacks instead of stalling for the whole computation.
* Synchronous code cannot be interrupted. A function passed to :meth:`run_sync`, or a
  long synchronous stretch inside a coroutine, runs to its end before a timeout,
  cancellation or stop can take effect.
* A job must not wait for another job (``run`` from inside a job would deadlock and is
  rejected with ``RuntimeError``).

How the integration uses it
---------------------------
* Stored accounts share one worker, owned by ``RuntimeStorage`` (storage.py). It is
  started on first use and stopped with Home Assistant, not on the last unload: a worker
  cannot be restarted. ``AccountRuntime`` (account.py) creates each ``AsyncAppleAccount``
  inside a job, registers ``account.close`` with :meth:`add_stop_callback` and fetches
  with a time limit. Only plain data (reports, the new alignment, the serialised
  session) is taken over on Home Assistant's side.
* Every account config flow has a worker of its own for sign-in and 2FA
  (config_flow.py), because the shared one does not exist before the first entry is set
  up. It is stopped when the flow ends. Key files are parsed in Home Assistant's
  executor, not here.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import enum
import itertools
import logging
import threading
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)

# aiohttp may wait for a TLS shutdown when closing; one stuck callback must not keep the
# remaining ones (and the thread) from finishing.
_STOP_CALLBACK_TIMEOUT = 10.0


class WorkerNotRunningError(RuntimeError):
    """The worker was not started yet, or is stopping or stopped."""


class _State(enum.Enum):
    NEW = enum.auto()
    RUNNING = enum.auto()
    STOPPING = enum.auto()
    STOPPED = enum.auto()


@final
class FindMyWorker:
    """A daemon thread with a private event loop that runs jobs one at a time.

    All public methods are thread-safe. ``run``, ``run_sync`` and ``async_stop`` must be
    awaited from an event loop other than the worker's own.
    """

    def __init__(self, name: str = "findmy-worker") -> None:
        self._name = name
        # Guards the state transitions together with submitting to the loop, so that a
        # job is either queued before the shutdown request or rejected, never lost.
        self._state_lock = threading.Lock()
        self._state = _State.NEW
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        # asyncio primitives bind to a loop on first use, not on creation, so they can
        # be created here and used on the worker loop only.
        self._job_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._done: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._stop_callbacks: dict[int, Callable[[], Awaitable[object]]] = {}
        self._callback_ids = itertools.count()

    @property
    def is_running(self) -> bool:
        """Whether jobs are currently accepted."""
        return self._state is _State.RUNNING

    def start(self) -> None:
        """Start the thread. Does nothing if already running.

        Does not wait for the thread: jobs submitted right away are queued on the loop
        and run as soon as it spins up.
        """
        with self._state_lock:
            if self._state is _State.RUNNING:
                return
            if self._state is not _State.NEW:
                msg = f"{self._name} was stopped and cannot be restarted"
                raise WorkerNotRunningError(msg)
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=self._thread_main, args=(loop,), name=self._name, daemon=True
            )
            try:
                thread.start()
            except BaseException:
                loop.close()
                raise
            self._loop = loop
            self._thread = thread
            self._state = _State.RUNNING
        _LOGGER.debug("%s started", self._name)

    # ``timeout`` differs from wrapping the call in ``asyncio.timeout``: it limits the
    # run time on the worker only, so time spent queued behind other jobs is not counted.
    async def run[T](
        self,
        factory: Callable[[], Awaitable[T]],
        *,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> T:
        """Run ``factory()`` on the worker after all earlier jobs and return its result.

        ``factory`` is called on the worker thread. See the module docstring for the
        semantics of ``timeout``, cancellation and stopping.
        """
        self._reject_worker_thread("run")
        with self._state_lock:
            loop = self._require_running()
            job = self._execute(factory, timeout)
            try:
                future = asyncio.run_coroutine_threadsafe(job, loop)
            except BaseException:
                job.close()
                raise
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            # The caller was not cancelled, so the job was cancelled by the shutdown.
            # Reporting that as CancelledError would make the caller believe it was
            # cancelled itself.
            task = asyncio.current_task()
            if (
                self._state is not _State.RUNNING
                and future.cancelled()
                and task is not None
                and task.cancelling() == 0
            ):
                msg = f"{self._name} stopped before the job finished"
                raise WorkerNotRunningError(msg) from None
            raise

    async def run_sync[T, *Ts](self, func: Callable[[*Ts], T], *args: *Ts) -> T:
        """Call a blocking or CPU-bound function on the worker thread.

        The call is queued like any other job and blocks the worker loop while it runs;
        it cannot be interrupted once started.
        """

        async def call() -> T:
            return func(*args)

        return await self.run(call)

    def add_stop_callback(self, callback: Callable[[], Awaitable[object]]) -> Callable[[], None]:
        """Register ``callback`` to be awaited on the worker loop during stop.

        Callbacks run after all jobs have been cancelled, in reverse order of
        registration, each limited to a few seconds; failures are logged and do not
        stop the others. Returns a function that unregisters the callback again.
        """
        with self._state_lock:
            _ = self._require_running()
            key = next(self._callback_ids)
            self._stop_callbacks[key] = callback

        def remove() -> None:
            with self._state_lock:
                _ = self._stop_callbacks.pop(key, None)

        return remove

    async def async_stop(self) -> None:
        """Stop the worker and wait until its thread has finished. Idempotent.

        Safe to bound with ``asyncio.timeout`` or to cancel: the shutdown then still
        completes in the background.
        """
        self._reject_worker_thread("async_stop")
        with self._state_lock:
            thread = self._thread
            if thread is None:
                # Never started: nothing to shut down, but no later start either.
                self._state = _State.STOPPED
                return
            if self._state is _State.RUNNING:
                self._state = _State.STOPPING
                assert self._loop is not None
                _ = self._loop.call_soon_threadsafe(self._stop_event.set)
                _LOGGER.debug("%s stopping", self._name)
        # The shield keeps a cancelled or timed out waiter from cancelling ``_done``,
        # which other waiters and the thread itself still rely on.
        await asyncio.shield(asyncio.wrap_future(self._done))
        # ``_done`` is the thread's last action, so this takes microseconds. Nothing can
        # signal the exit itself, and polling keeps the blocking ``join`` (and any
        # dependency on the caller's executor) out of the caller's loop.
        while thread.is_alive():  # noqa: ASYNC110
            await asyncio.sleep(0.001)

    def _require_running(self) -> asyncio.AbstractEventLoop:
        if self._state is _State.NEW:
            msg = f"{self._name} has not been started"
            raise WorkerNotRunningError(msg)
        if self._state is not _State.RUNNING:
            msg = f"{self._name} is stopped"
            raise WorkerNotRunningError(msg)
        assert self._loop is not None
        return self._loop

    def _reject_worker_thread(self, method: str) -> None:
        thread = self._thread
        if thread is not None and thread.ident == threading.get_ident():
            msg = f"{method}() called from inside a job of {self._name} would deadlock"
            raise RuntimeError(msg)

    async def _execute[T](self, factory: Callable[[], Awaitable[T]], limit: float | None) -> T:
        async with self._job_lock, asyncio.timeout(limit):
            return await factory()

    def _thread_main(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            # Runner cancels leftover tasks, shuts down async generators and the default
            # executor (used by aiohttp for DNS lookups) and closes the loop.
            with asyncio.Runner(loop_factory=lambda: loop) as runner:
                try:
                    runner.run(self._serve())
                finally:
                    with self._state_lock:
                        self._state = _State.STOPPED
        except BaseException:
            _LOGGER.exception("%s terminated unexpectedly", self._name)
            raise
        finally:
            _LOGGER.debug("%s stopped", self._name)
            self._done.set_result(None)

    async def _serve(self) -> None:
        _ = await self._stop_event.wait()

        # Every job submitted before the stop request already has its task: submission
        # and the request are both queued with call_soon_threadsafe, in order.
        current = asyncio.current_task()
        jobs = [task for task in asyncio.all_tasks() if task is not current]
        for task in jobs:
            _ = task.cancel()
        _ = await asyncio.gather(*jobs, return_exceptions=True)

        with self._state_lock:
            callbacks = list(self._stop_callbacks.values())
            self._stop_callbacks.clear()
        for callback in reversed(callbacks):
            await self._run_stop_callback(callback)

    async def _run_stop_callback(self, callback: Callable[[], Awaitable[object]]) -> None:
        try:
            async with asyncio.timeout(_STOP_CALLBACK_TIMEOUT):
                _ = await callback()
        except Exception:
            _LOGGER.exception("Stop callback %r of %s failed", callback, self._name)
