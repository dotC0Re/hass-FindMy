"""The library bundle of the integrated Anisette provider: one copy, verified, fetched in time.

Why
---
The integrated ("local") Anisette provider emulates two Android libraries of Apple Music. The
``anisette`` package does not ship them. Left alone, it downloads an archive with them from a
server of its author on first use after every start of Home Assistant, in a thread, with no
time limit and with no check of what it received (finding M3/S4). A download that hangs outlives
the fetch that started it, and every later fetch adds another stuck thread.

What this module does
---------------------
* It keeps **one** copy of the bundle at ``<config>/.storage/findmy.anisette_libs`` and hands
  its path to every local provider (``LocalAnisetteProvider(libs_path=...)``,
  ``AsyncAppleAccount.from_json(..., anisette_libs_path=...)``). A provider that finds the file
  loads it and does not download anything.
* It downloads the archive itself, through Home Assistant's HTTP session and under a time
  limit, so nothing can hang and no thread is involved.
* It remembers the SHA-256 of the stored bundle in ``findmy.anisette_libs.sha256`` next to it
  (trust on first use) and compares it before every use. A bundle that no longer matches is
  not used: :class:`AnisetteLibsChecksumError` says which file to delete to accept a new one.

:func:`async_ensure_anisette_libs` is the only entry point. It must be awaited before a local
provider is created or restored, and returns the path to pass on.

The two formats
---------------
What the server sends (a tar or zip archive with the two ``.so`` files) is *not* what a
provider loads from its ``libs_path`` (a tar archive of the package's virtual file system, as
written by ``Anisette.save_libs``). A provider that is given the downloaded archive as it came
would ignore it and download again. The download is therefore converted once, in memory, with
the public API of the package: ``Anisette.init(archive).save_libs(target)``. That reads the two
libraries out of the archive and never starts the emulator or opens a connection. It also is
the check that the download was complete: an archive that is cut off or lacks a library fails
there, before anything is written.

The conversion is deterministic, and so is the library's own re-save described below, which is
what makes a checksum of the stored file meaningful.

What cannot be prevented
------------------------
* FindMy.py writes the bundle back to ``libs_path`` every time a provider initialises
  (``LocalAnisetteProvider._get_ani`` calls ``save_libs`` unconditionally), in place and not
  atomically. The bytes it writes are the ones it read, so the checksum keeps matching. While
  it writes, the file is incomplete for a moment: the comparison is therefore repeated a few
  times before a mismatch is reported. A crash in exactly that moment leaves a damaged file,
  which is then refused like any other change.
* If the file disappears between the check and the provider's first use, the package falls
  back to its own unlimited download. Only deleting the file while Home Assistant runs gets
  there; the check runs when an account entry is set up and before every sign-in attempt.

Nothing here runs file I/O or hashing on Home Assistant's event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

import aiohttp
from anisette import Anisette

# Not re-exported by the package, but a public name of its module. Following it means that a
# release of the package which moves the bundle is picked up without a change here.
from anisette.anisette import DEFAULT_LIBS_URL
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import STORAGE_DIR
from homeassistant.util.hass_dict import HassKey

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Both live in Home Assistant's storage directory, so they are part of every backup and a
# restored system does not have to download anything.
BUNDLE_FILE: Final = "findmy.anisette_libs"
CHECKSUM_FILE: Final = f"{BUNDLE_FILE}.sha256"

# The whole download: connecting, the response and its body.
_DOWNLOAD_TIMEOUT = 60.0
# The archive has a few megabytes. The limit only keeps a wrong or hostile answer from being
# read into memory without end.
_MAX_DOWNLOAD_SIZE = 64 * 1024 * 1024
_CHUNK_SIZE = 64 * 1024

# How long to wait before comparing again after a mismatch, see "What cannot be prevented".
# Re-saving a few megabytes takes well under a second on a desktop and a few seconds on a
# small board.
_RECHECK_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0)

# One check or download at a time per Home Assistant instance: several account entries are set
# up at once on start, and all of them want the same file.
_LOCK: HassKey[asyncio.Lock] = HassKey("findmy_anisette_libs_lock")


class AnisetteLibsError(HomeAssistantError):
    """The library bundle of the integrated Anisette provider cannot be used."""


class AnisetteLibsUnavailableError(AnisetteLibsError):
    """There is no bundle and none could be fetched or stored. Trying again later may work."""


class AnisetteLibsChecksumError(AnisetteLibsError):
    """The stored bundle is not the one whose checksum was recorded. Needs the user."""

    def __init__(self, path: Path, expected: str, actual: str) -> None:
        # Paths and checksums only; nothing in here is secret.
        msg = (
            f"The Anisette library bundle {path} does not match the checksum recorded when it "
            f"was stored (expected SHA-256 {expected}, found {actual}). It was changed or "
            "damaged and is not used. To accept a newly downloaded bundle, delete this file "
            "and reload the integration or sign in again."
        )
        super().__init__(msg)
        self.path: Path = path
        self.expected: str = expected
        self.actual: str = actual


def anisette_libs_path(hass: HomeAssistant) -> Path:
    """Where the bundle is kept. The file need not exist, see async_ensure_anisette_libs."""
    return Path(hass.config.path(STORAGE_DIR, BUNDLE_FILE))


async def async_ensure_anisette_libs(hass: HomeAssistant) -> Path:
    """Return the path of the verified bundle, fetching and storing it first if there is none.

    Raises :class:`AnisetteLibsChecksumError` if the stored bundle does not match its recorded
    checksum, and :class:`AnisetteLibsUnavailableError` if there is no bundle and the download
    failed, timed out or did not contain the libraries. In both cases the caller must not create
    a local provider: without the file it would download on its own.
    """
    bundle = anisette_libs_path(hass)
    checksum = bundle.with_name(CHECKSUM_FILE)

    async with hass.data.setdefault(_LOCK, asyncio.Lock()):
        if await hass.async_add_executor_job(_verify, bundle, checksum):
            return bundle

        _LOGGER.info("Downloading the Anisette library bundle from %s", DEFAULT_LIBS_URL)
        archive = await _async_download(hass)
        digest = await hass.async_add_executor_job(_convert_and_store, archive, bundle, checksum)
        _LOGGER.info("Stored the Anisette library bundle as %s (SHA-256 %s)", bundle, digest)
        return bundle


async def _async_download(hass: HomeAssistant) -> bytes:
    """Fetch the archive with the libraries. Never takes longer than the time limit."""
    session = async_get_clientsession(hass)
    chunks: list[bytes] = []
    size = 0
    try:
        # asyncio.timeout rather than a ClientTimeout: it also covers a body that trickles in,
        # and it ends the request by cancelling it, which closes the connection.
        async with asyncio.timeout(_DOWNLOAD_TIMEOUT), session.get(DEFAULT_LIBS_URL) as response:
            response.raise_for_status()
            async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
                size += len(chunk)
                if size > _MAX_DOWNLOAD_SIZE:
                    msg = (
                        "The download of the Anisette library bundle was stopped: it is larger "
                        f"than {_MAX_DOWNLOAD_SIZE // (1024 * 1024)} MB"
                    )
                    raise AnisetteLibsUnavailableError(msg)
                chunks.append(chunk)
    except TimeoutError as err:
        msg = (
            "The download of the Anisette library bundle did not finish within "
            f"{_DOWNLOAD_TIMEOUT:.0f} s"
        )
        raise AnisetteLibsUnavailableError(msg) from err
    except aiohttp.ClientError as err:
        # Also a body that ends early (ClientPayloadError). The URL is public and has no
        # credentials, so the text of the error is safe to show.
        msg = f"The download of the Anisette library bundle failed ({type(err).__name__}: {err})"
        raise AnisetteLibsUnavailableError(msg) from err
    return b"".join(chunks)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _temporary(path: Path) -> Path:
    # In the same directory as its target, so that the final rename cannot cross file systems.
    return path.with_name(f"{path.name}.tmp")


def _read_checksum(checksum: Path) -> str | None:
    """The recorded SHA-256, or None if there is no usable record."""
    try:
        fields = checksum.read_text(encoding="ascii").split()
    except OSError, ValueError:
        return None
    recorded = fields[0].lower() if fields else ""
    if len(recorded) != hashlib.sha256().digest_size * 2 or not _is_hex(recorded):
        return None
    return recorded


def _is_hex(text: str) -> bool:
    try:
        _ = int(text, 16)
    except ValueError:
        return False
    return True


def _verify(bundle: Path, checksum: Path) -> bool:
    """Whether a bundle that matches its recorded checksum is in place. Runs in the executor.

    False means that a bundle has to be downloaded. A bundle that is there but does not match
    raises AnisetteLibsChecksumError instead: replacing it is the user's decision.
    """
    # Left behind only by a write that was interrupted, e.g. by a power loss.
    for leftover in (_temporary(bundle), _temporary(checksum)):
        with contextlib.suppress(OSError):
            leftover.unlink(missing_ok=True)

    if not bundle.is_file():
        # The first use, or the user deleted the bundle to accept a new one.
        return False

    expected = _read_checksum(checksum)
    if expected is None:
        # The checksum is written before the bundle is moved into place, so this file was not
        # stored by this integration, or its record was removed. Either way there is nothing to
        # hold it against, and it is replaced by a download instead of being trusted.
        _LOGGER.warning(
            "The Anisette library bundle %s has no recorded checksum and is downloaded again",
            bundle,
        )
        try:
            bundle.unlink()
        except OSError as err:
            msg = f"The Anisette library bundle {bundle} cannot be replaced ({err})"
            raise AnisetteLibsUnavailableError(msg) from err
        return False

    actual = ""
    for delay in (*_RECHECK_DELAYS, None):
        try:
            actual = _sha256(bundle)
        except OSError as err:
            actual = f"unreadable: {type(err).__name__}"
        if actual == expected:
            return True
        if delay is not None:
            # A provider that is initialising may be re-saving the file right now, see the
            # module docstring. This is an executor thread; sleeping here blocks nothing else.
            time.sleep(delay)
    raise AnisetteLibsChecksumError(bundle, expected, actual)


def _convert_and_store(archive: bytes, bundle: Path, checksum: Path) -> str:
    """Turn a downloaded archive into the stored bundle and record its SHA-256.

    Runs in the executor. Either both files are complete afterwards or the bundle is missing;
    a partial bundle is never left under its final name.
    """
    converted = io.BytesIO()
    try:
        # No network and no emulator: `init` only notes where the libraries come from, and
        # `save_libs` reads them from the archive. See "The two formats" in the module docstring.
        Anisette.init(io.BytesIO(archive)).save_libs(converted)
    except Exception as err:
        # Whatever the package and the archive modules raise for an archive that is cut off,
        # of another format or without the libraries: none of it can be used.
        msg = (
            "The downloaded Anisette library bundle is incomplete or not a library bundle "
            f"({type(err).__name__}); nothing was stored"
        )
        raise AnisetteLibsUnavailableError(msg) from err
    data = converted.getvalue()
    digest = hashlib.sha256(data).hexdigest()

    previous = _read_checksum(checksum)
    if previous is not None and previous != digest:
        # The user deleted the old bundle, and the server now sends another one. That is what
        # deleting it asks for, but it should not happen without a trace.
        _LOGGER.warning(
            "Accepting an Anisette library bundle that differs from the one used before: %s -> %s",
            previous,
            digest,
        )

    bundle_tmp, checksum_tmp = _temporary(bundle), _temporary(checksum)
    try:
        bundle.parent.mkdir(parents=True, exist_ok=True)
        _write(bundle_tmp, data)
        # The format of `sha256sum`, so that the record can be checked by hand.
        _write(checksum_tmp, f"{digest}  {bundle.name}\n".encode("ascii"))
        # The checksum first: if the second rename never happens, a checksum without a bundle
        # is simply the state before the first download.
        _ = checksum_tmp.replace(checksum)
        _ = bundle_tmp.replace(bundle)
    except OSError as err:
        msg = f"The Anisette library bundle could not be stored as {bundle} ({err})"
        raise AnisetteLibsUnavailableError(msg) from err
    finally:
        for leftover in (bundle_tmp, checksum_tmp):
            with contextlib.suppress(OSError):
                leftover.unlink(missing_ok=True)
    return digest


def _write(path: Path, data: bytes) -> None:
    with path.open("wb") as file:
        _ = file.write(data)
        file.flush()
        # The rename that follows must not be able to outlive the data it names.
        os.fsync(file.fileno())
