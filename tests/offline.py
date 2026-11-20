"""Helpers for tests that must stay offline: a network guard and a fake Anisette bundle.

Not a conftest: the test files that need these import them and wrap them in fixtures of their
own.
"""

from __future__ import annotations

import asyncio
import io
import socket
import tarfile
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, final

import aiohttp
import urllib3

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest

# The two libraries the anisette package looks for in a bundle.
ANISETTE_LIBRARIES = ("libstoreservicescore.so", "libCoreADI.so")

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def install_network_guard(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make every way the code under test could reach the network fail, and note the attempt.

    Returns the list of attempts. The caller asserts that it is empty when the test is over:
    the code under test catches network errors, so the failure raised here may never reach
    the test on its own.

    Covered: aiohttp requests (FindMy.py, Home Assistant), urllib3 requests (the anisette
    package's own bundle download), name resolution and blocking socket connections.
    Loopback stays open, because asyncio builds its wake-up channel from a local socket pair
    on Windows. Not covered: a non-blocking connect to a literal IP address, which nothing in
    this project does.
    """
    attempts: list[str] = []

    def refuse(what: str) -> None:
        attempts.append(what)
        msg = f"a test tried to use the network: {what}"
        raise AssertionError(msg)

    async def aiohttp_request(_session: object, method: str, url: object, **_kwargs: object):
        refuse(f"aiohttp {method} {url}")

    def urllib3_request(_pool: object, method: str, url: str, **_kwargs: object):
        refuse(f"urllib3 {method} {url}")

    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect

    def getaddrinfo(host: object, *args: object, **kwargs: object):
        if host is not None and str(host) not in _LOOPBACK:
            refuse(f"name resolution of {host}")
        return original_getaddrinfo(host, *args, **kwargs)  # pyright: ignore[reportArgumentType]

    def connect(sock: socket.socket, address: object) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if str(host) not in _LOOPBACK:
            refuse(f"connection to {address}")
        original_connect(sock, address)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(aiohttp.ClientSession, "_request", aiohttp_request)
    monkeypatch.setattr(urllib3.PoolManager, "urlopen", urllib3_request)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    return attempts


def make_library_archive(
    marker: str = "v1", *, libraries: tuple[str, ...] = ANISETTE_LIBRARIES
) -> bytes:
    """An archive shaped like the one the anisette package downloads, with fake libraries.

    The libraries are not real code, so an emulator cannot be started from it. Reading the
    archive, converting it and loading it back all work, which is what the tests need.
    ``marker`` makes two archives differ.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in libraries:
            data = f"{name}:{marker}:".encode() * 2000
            info = tarfile.TarInfo(name=f"libs/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@final
class FakeDownload:
    """Stands in for Home Assistant's HTTP session and serves one archive.

    ``body`` is sent in two halves. ``fail_after_first_chunk`` ends the body early the way
    aiohttp reports a connection that closed before the announced length; ``hang`` never
    answers.
    """

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.status = 200
        self.fail_after_first_chunk = False
        self.hang = False
        self.requests: list[str] = []
        self.cancelled = False
        self.loop_thread = threading.get_ident()

    def get(self, url: str, **_kwargs: Any) -> FakeDownload:
        self.requests.append(url)
        return self

    async def __aenter__(self) -> Self:
        if self.hang:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                # Only what the text of the error reads.
                request_info=SimpleNamespace(real_url=self.requests[-1]),  # pyright: ignore[reportArgumentType]
                history=(),
                status=self.status,
                message="Service Unavailable",
            )

    @property
    def content(self) -> FakeDownload:
        return self

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        half = len(self.body) // 2
        yield self.body[:half]
        if self.fail_after_first_chunk:
            msg = "Response payload is not completed"
            raise aiohttp.ClientPayloadError(msg)
        yield self.body[half:]
