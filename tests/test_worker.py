"""Tests for ``custom_components.findmy.worker``.

The worker only depends on the standard library, so these tests need neither Home
Assistant nor the FindMy library. Warnings are errors here, and every test checks that
nothing was left unclosed: no never-awaited coroutines, open loops or aiohttp sessions.
"""

# pyright: basic

from __future__ import annotations

import asyncio
import gc
import logging
import sys
import threading
import time
from typing import TYPE_CHECKING

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric import ec

from custom_components.findmy.worker import FindMyWorker, WorkerNotRunningError, notify_job_start

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

pytestmark = pytest.mark.filterwarnings("error")

# Same order of work as one fetch of one accessory in the library (about 2,016 calls).
_DERIVATIONS = 2000
_PROBE_INTERVAL = 0.01


@pytest.fixture(autouse=True)
def _no_leaks(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Fail on anything reported only from ``__del__`` or by an event loop."""
    unraisable: list[sys.UnraisableHookArgs] = []
    previous_hook = sys.unraisablehook
    sys.unraisablehook = unraisable.append
    caplog.set_level(logging.WARNING)
    try:
        yield
        _ = gc.collect()
    finally:
        sys.unraisablehook = previous_hook
    assert [repr(u.exc_value) for u in unraisable] == []
    loop_errors = [
        r.getMessage() for r in caplog.records if r.name in {"asyncio", "aiohttp.client"}
    ]
    assert loop_errors == []


@pytest_asyncio.fixture
async def worker() -> AsyncIterator[FindMyWorker]:
    worker = FindMyWorker()
    worker.start()
    yield worker
    async with asyncio.timeout(10):
        await worker.async_stop()


async def _wait_for(event: threading.Event) -> None:
    """Wait for a flag set on the worker thread without blocking this loop."""
    assert await asyncio.to_thread(event.wait, 5)


def _derive_keys(count: int) -> None:
    curve = ec.SECP224R1()
    for i in range(count):
        _ = ec.derive_private_key(0x1234_5678_9ABC + i, curve)


async def _max_loop_lag(work: Callable[[], Awaitable[None]]) -> tuple[float, float]:
    """Run ``work`` while a probe task measures how late this loop wakes it up.

    Returns the longest delay beyond the requested sleep and the duration of ``work``.
    """
    loop = asyncio.get_running_loop()
    lags: list[float] = []
    done = asyncio.Event()

    async def probe() -> None:
        while not done.is_set():
            start = loop.time()
            await asyncio.sleep(_PROBE_INTERVAL)
            lags.append(loop.time() - start - _PROBE_INTERVAL)

    probe_task = asyncio.create_task(probe())
    await asyncio.sleep(0.05)
    start = time.perf_counter()
    await work()
    duration = time.perf_counter() - start
    await asyncio.sleep(3 * _PROBE_INTERVAL)
    done.set()
    await probe_task
    return max(lags), duration


# --- basic behaviour ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_runs_on_worker_thread_and_loop(worker: FindMyWorker) -> None:
    factory_thread: list[int] = []

    async def job() -> tuple[int, asyncio.AbstractEventLoop]:
        return threading.get_ident(), asyncio.get_running_loop()

    def factory() -> Awaitable[tuple[int, asyncio.AbstractEventLoop]]:
        factory_thread.append(threading.get_ident())
        return job()

    job_thread, job_loop = await worker.run(factory)

    assert job_thread != threading.get_ident()
    assert job_loop is not asyncio.get_running_loop()
    assert factory_thread == [job_thread]


@pytest.mark.asyncio
async def test_result_is_returned(worker: FindMyWorker) -> None:
    async def job() -> dict[str, int]:
        await asyncio.sleep(0)
        return {"answer": 42}

    assert await worker.run(job) == {"answer": 42}


@pytest.mark.asyncio
async def test_exception_reaches_caller_unchanged(worker: FindMyWorker) -> None:
    error = KeyError("missing")

    async def job() -> None:
        await asyncio.sleep(0)
        raise error

    with pytest.raises(KeyError) as exc_info:
        await worker.run(job)
    assert exc_info.value is error

    # The worker survives a failing job.
    assert await worker.run_sync(lambda: "still alive") == "still alive"


@pytest.mark.asyncio
async def test_run_sync_runs_on_worker_thread(worker: FindMyWorker) -> None:
    def add(a: int, b: int) -> tuple[int, int]:
        return a + b, threading.get_ident()

    result, thread = await worker.run_sync(add, 2, 3)

    assert result == 5
    assert thread != threading.get_ident()


@pytest.mark.asyncio
async def test_timeout_cancels_job_on_worker(worker: FindMyWorker) -> None:
    cancelled = threading.Event()

    async def job() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(TimeoutError):
        await worker.run(job, timeout=0.05)

    # TimeoutError is only raised once the job has unwound on the worker.
    assert cancelled.is_set()
    assert await worker.run_sync(lambda: 1) == 1


@pytest.mark.asyncio
async def test_timeout_does_not_count_time_in_queue(worker: FindMyWorker) -> None:
    async def slow() -> None:
        await asyncio.sleep(0.3)

    async def quick() -> str:
        await asyncio.sleep(0.05)
        return "ok"

    results = await asyncio.gather(worker.run(slow), worker.run(quick, timeout=0.2))

    assert results == [None, "ok"]


@pytest.mark.asyncio
async def test_caller_cancellation_cancels_job(worker: FindMyWorker) -> None:
    started = threading.Event()
    cancelled = threading.Event()

    async def job() -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    caller = asyncio.create_task(worker.run(job))
    await _wait_for(started)
    _ = caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller
    await _wait_for(cancelled)
    assert await worker.run_sync(lambda: "next job runs") == "next job runs"


@pytest.mark.asyncio
async def test_caller_timeout_also_cancels_job(worker: FindMyWorker) -> None:
    cancelled = threading.Event()

    async def job() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await worker.run(job)
    await _wait_for(cancelled)


@pytest.mark.asyncio
async def test_jobs_do_not_overlap(worker: FindMyWorker) -> None:
    events: list[str] = []
    active = 0
    max_active = 0

    def make_job(name: str) -> Callable[[], Awaitable[str]]:
        async def job() -> str:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            events.append(f"{name} start")
            await asyncio.sleep(0.05)
            events.append(f"{name} end")
            active -= 1
            return name

        return job

    def blocking() -> str:
        events.append("sync")
        return "sync"

    results = await asyncio.gather(
        worker.run(make_job("a")), worker.run_sync(blocking), worker.run(make_job("b"))
    )

    assert results == ["a", "sync", "b"]
    assert max_active == 1
    assert events == ["a start", "a end", "sync", "b start", "b end"]


@pytest.mark.asyncio
async def test_run_from_inside_a_job_is_rejected(worker: FindMyWorker) -> None:
    async def inner() -> None:
        pass

    async def outer() -> None:
        await worker.run(inner)

    with pytest.raises(RuntimeError, match="deadlock"):
        await worker.run(outer)


# --- lifecycle --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_stop_are_idempotent() -> None:
    worker = FindMyWorker(name="findmy-idempotent")
    worker.start()
    worker.start()
    threads = [t for t in threading.enumerate() if t.name == "findmy-idempotent"]
    assert len(threads) == 1
    assert threads[0].daemon

    await worker.async_stop()
    await worker.async_stop()

    assert not worker.is_running
    assert not threads[0].is_alive()


@pytest.mark.asyncio
async def test_concurrent_stops_wait_for_the_thread() -> None:
    worker = FindMyWorker(name="findmy-concurrent-stop")
    worker.start()
    thread = next(t for t in threading.enumerate() if t.name == "findmy-concurrent-stop")

    _ = await asyncio.gather(worker.async_stop(), worker.async_stop())

    assert not thread.is_alive()


@pytest.mark.asyncio
async def test_stop_without_start() -> None:
    worker = FindMyWorker()
    await worker.async_stop()
    await worker.async_stop()

    with pytest.raises(WorkerNotRunningError, match="cannot be restarted"):
        worker.start()


@pytest.mark.asyncio
async def test_use_before_start_raises() -> None:
    worker = FindMyWorker()
    called = False

    async def job() -> None:
        nonlocal called
        called = True

    with pytest.raises(WorkerNotRunningError, match="not been started"):
        await worker.run(job)
    assert not called
    await worker.async_stop()


@pytest.mark.asyncio
async def test_use_after_stop_raises() -> None:
    worker = FindMyWorker()
    worker.start()
    await worker.async_stop()
    calls: list[str] = []

    async def job() -> None:
        calls.append("job")

    def func() -> None:
        calls.append("func")

    async def cleanup() -> None:
        calls.append("cleanup")

    with pytest.raises(WorkerNotRunningError, match="stopped"):
        await worker.run(job)
    with pytest.raises(WorkerNotRunningError, match="stopped"):
        await worker.run_sync(func)
    with pytest.raises(WorkerNotRunningError, match="stopped"):
        _ = worker.add_stop_callback(cleanup)
    with pytest.raises(WorkerNotRunningError, match="cannot be restarted"):
        worker.start()
    assert calls == []


@pytest.mark.asyncio
async def test_stop_cancels_running_and_pending_jobs() -> None:
    worker = FindMyWorker(name="findmy-stop-jobs")
    worker.start()
    thread = next(t for t in threading.enumerate() if t.name == "findmy-stop-jobs")
    started = threading.Event()
    cancelled = threading.Event()
    pending_factory_calls = 0

    async def running() -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def pending_job() -> None:
        await asyncio.sleep(30)

    def pending_factory() -> Awaitable[None]:
        nonlocal pending_factory_calls
        pending_factory_calls += 1
        return pending_job()

    first = asyncio.create_task(worker.run(running))
    second = asyncio.create_task(worker.run(pending_factory))
    third = asyncio.create_task(worker.run_sync(lambda: None))
    await _wait_for(started)

    async with asyncio.timeout(10):
        await worker.async_stop()

    for caller in (first, second, third):
        with pytest.raises(WorkerNotRunningError, match="stopped before the job finished"):
            await caller
    assert cancelled.is_set()
    assert pending_factory_calls == 0
    assert not thread.is_alive()


@pytest.mark.asyncio
async def test_stop_callbacks_run_on_worker_in_reverse_order(
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = FindMyWorker()
    worker.start()
    calls: list[tuple[str, int]] = []

    def make_callback(name: str) -> Callable[[], Awaitable[None]]:
        async def callback() -> None:
            calls.append((name, threading.get_ident()))
            if name == "failing":
                msg = "cleanup failed"
                raise OSError(msg)

        return callback

    worker_thread = await worker.run_sync(threading.get_ident)
    _ = worker.add_stop_callback(make_callback("first"))
    _ = worker.add_stop_callback(make_callback("failing"))
    remove = worker.add_stop_callback(make_callback("removed"))
    _ = worker.add_stop_callback(make_callback("last"))
    remove()

    await worker.async_stop()

    assert calls == [("last", worker_thread), ("failing", worker_thread), ("first", worker_thread)]
    failures = [r for r in caplog.records if r.name == "custom_components.findmy.worker"]
    assert len(failures) == 1
    assert "failed" in failures[0].getMessage()
    caplog.clear()


# --- the reason for the worker: library-style work off Home Assistant's loop ------


@pytest.mark.asyncio
async def test_cpu_work_on_worker_does_not_block_caller_loop(worker: FindMyWorker) -> None:
    async def on_worker() -> None:
        await worker.run_sync(_derive_keys, _DERIVATIONS)

    async def in_loop() -> None:
        _derive_keys(_DERIVATIONS)

    worker_lag, worker_duration = await _max_loop_lag(on_worker)
    direct_lag, direct_duration = await _max_loop_lag(in_loop)

    print(  # noqa: T201
        f"\n{_DERIVATIONS} derivations: in loop {direct_duration:.3f} s, longest stall "
        f"{direct_lag * 1000:.1f} ms; on worker {worker_duration:.3f} s, longest stall "
        f"{worker_lag * 1000:.1f} ms"
    )
    # Sanity check that the measurement sees a blocked loop at all.
    assert direct_lag > 0.5 * direct_duration
    # Relative criterion for slow machines, plus a generous absolute ceiling.
    assert worker_lag < direct_lag / 4
    assert worker_lag < 0.5


# --- aiohttp sessions bound to the worker loop ------------------------------------


@pytest.mark.asyncio
async def test_aiohttp_session_lives_on_worker_and_is_closed_on_stop() -> None:
    worker = FindMyWorker()
    worker.start()
    worker_thread = await worker.run_sync(threading.get_ident)
    handler_threads: list[int] = []

    async def ping(request: web.Request) -> web.Response:
        handler_threads.append(threading.get_ident())
        return web.Response(text=f"pong {request.query.get('n', '')}")

    async def setup() -> tuple[aiohttp.ClientSession, TestServer]:
        app = web.Application()
        _ = app.router.add_get("/ping", ping)
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        _ = worker.add_stop_callback(server.close)
        session = aiohttp.ClientSession()
        # Registered last, so it is closed before the server.
        _ = worker.add_stop_callback(session.close)
        return session, server

    session, server = await worker.run(setup)

    async def fetch(n: int) -> str:
        async with session.get(server.make_url("/ping"), params={"n": n}) as response:
            response.raise_for_status()
            return await response.text()

    # The same session serves several jobs, as an account would across fetches.
    assert await worker.run(lambda: fetch(1), timeout=10) == "pong 1"
    assert await worker.run(lambda: fetch(2), timeout=10) == "pong 2"
    assert handler_threads == [worker_thread, worker_thread]
    assert not session.closed

    async with asyncio.timeout(10):
        await worker.async_stop()

    assert session.closed


async def test_job_start_is_reported_before_the_result(worker: FindMyWorker) -> None:
    events: list[str] = []

    async def job() -> str:
        return "done"

    with notify_job_start(lambda: events.append("started")):
        result = await worker.run(job)
    events.append(result)

    assert events == ["started", "done"]


async def test_job_start_is_reported_for_a_job_that_fails(worker: FindMyWorker) -> None:
    started = asyncio.Event()

    async def job() -> None:
        msg = "failed"
        raise ValueError(msg)

    with notify_job_start(started.set), pytest.raises(ValueError, match="failed"):
        await worker.run(job)

    assert started.is_set()


async def test_job_cancelled_in_the_queue_is_never_reported_as_started(
    worker: FindMyWorker,
) -> None:
    gate = threading.Event()
    reported: list[None] = []

    async def occupy() -> None:
        await _wait_for(gate)

    async def queued() -> None:
        pytest.fail("the cancelled job ran")

    busy = asyncio.create_task(worker.run(occupy))
    with notify_job_start(lambda: reported.append(None)):
        waiting = asyncio.create_task(worker.run(queued))
    await asyncio.sleep(0.05)
    _ = waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    gate.set()
    await busy
    # Give a stray report the time to arrive.
    await asyncio.sleep(0.01)

    assert reported == []


async def test_job_rejected_by_a_stopped_worker_is_never_reported_as_started() -> None:
    worker = FindMyWorker()
    worker.start()
    await worker.async_stop()
    reported: list[None] = []

    async def job() -> None:
        pytest.fail("the job ran")

    with notify_job_start(lambda: reported.append(None)), pytest.raises(WorkerNotRunningError):
        await worker.run(job)

    assert reported == []


async def test_job_start_is_only_reported_inside_the_block(worker: FindMyWorker) -> None:
    reported: list[None] = []

    async def job() -> None:
        return None

    with notify_job_start(lambda: reported.append(None)):
        pass
    await worker.run(job)
    await asyncio.sleep(0.01)

    assert reported == []
