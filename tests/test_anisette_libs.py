"""Tests for the library bundle of the integrated Anisette provider (finding M3/S4).

The bundle is a fake one and the download is a fake HTTP session; the conversion and the
loading run through the real anisette package. Any attempt to reach the network fails the test.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from anisette import Anisette
from anisette import anisette as anisette_module
from homeassistant.core import HomeAssistant

from custom_components.findmy import anisette_libs
from custom_components.findmy.anisette_libs import (
    BUNDLE_FILE,
    CHECKSUM_FILE,
    AnisetteLibsChecksumError,
    AnisetteLibsUnavailableError,
    anisette_libs_path,
    async_ensure_anisette_libs,
)

from .offline import (
    ANISETTE_LIBRARIES,
    FakeDownload,
    install_network_guard,
    make_library_archive,
)

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(autouse=True)
def network_guard(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    attempts = install_network_guard(monkeypatch)
    yield
    assert not attempts, f"the network was used: {attempts}"


@pytest.fixture(autouse=True)
def no_recheck_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report a mismatch at once; the test for the repeated comparison sets its own delays."""
    monkeypatch.setattr(anisette_libs, "_RECHECK_DELAYS", ())


@pytest.fixture
def download(monkeypatch: pytest.MonkeyPatch) -> FakeDownload:
    fake = FakeDownload(make_library_archive())
    monkeypatch.setattr(anisette_libs, "async_get_clientsession", lambda _hass: fake)
    return fake


def storage_files(hass: HomeAssistant) -> set[str]:
    """What the integration left in the storage directory."""
    directory = anisette_libs_path(hass).parent
    if not directory.is_dir():
        return set()
    return {path.name for path in directory.iterdir() if path.name.startswith(BUNDLE_FILE)}


def recorded_checksum(hass: HomeAssistant) -> str:
    return anisette_libs_path(hass).with_name(CHECKSUM_FILE).read_text(encoding="ascii").split()[0]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_without_download(monkeypatch: pytest.MonkeyPatch, path: Path) -> Anisette:
    """Load a bundle the way a provider does, failing if the package wants to download."""

    def no_download(_file: object = None) -> None:
        msg = "the anisette package fell back to its own download"
        raise AssertionError(msg)

    monkeypatch.setattr(anisette_module, "_get_libs", no_download)
    return Anisette.load(path)


async def test_bundle_is_downloaded_once_and_stored_with_its_checksum(
    hass: HomeAssistant, download: FakeDownload, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = await async_ensure_anisette_libs(hass)

    assert path == Path(hass.config.path(".storage", BUNDLE_FILE))
    assert download.requests == [anisette_module.DEFAULT_LIBS_URL]
    assert storage_files(hass) == {BUNDLE_FILE, CHECKSUM_FILE}
    assert recorded_checksum(hass) == sha256(path)
    # In the format of `sha256sum`, so that it can be checked by hand.
    checksum_text = path.with_name(CHECKSUM_FILE).read_text(encoding="ascii")
    assert checksum_text == f"{sha256(path)}  {BUNDLE_FILE}\n"
    # A provider finds the libraries in the file and has no reason to download.
    loaded = await hass.async_add_executor_job(load_without_download, monkeypatch, path)
    library_store = loaded._ani_provider.library_store
    assert set(library_store.listdir()) == set(ANISETTE_LIBRARIES)


async def test_downloaded_archive_alone_would_not_stop_the_library_from_downloading(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the download is converted: the package ignores the archive in the server's format."""
    raw = tmp_path / "raw-archive"
    raw.write_bytes(make_library_archive())

    with pytest.raises(AssertionError, match="fell back to its own download"):
        await hass.async_add_executor_job(load_without_download, monkeypatch, raw)


async def test_bundle_is_reused_after_a_restart(
    hass: HomeAssistant, download: FakeDownload
) -> None:
    path = await async_ensure_anisette_libs(hass)
    stored = path.read_bytes()

    # The same instance again, and a new one on the same configuration directory.
    assert await async_ensure_anisette_libs(hass) == path
    restarted = HomeAssistant(hass.config.config_dir)
    try:
        assert await async_ensure_anisette_libs(restarted) == path
    finally:
        await restarted.async_stop(force=True)

    assert len(download.requests) == 1
    assert path.read_bytes() == stored


async def test_resave_by_the_library_keeps_the_checksum(
    hass: HomeAssistant, download: FakeDownload, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FindMy.py saves the libraries back to the file whenever a provider initialises.

    The check only holds if that writes the bytes that were read. If a release of the anisette
    package changes how it saves, this test fails and the checksum has to cover the content
    instead of the file.
    """
    path = await async_ensure_anisette_libs(hass)

    def resave() -> None:
        load_without_download(monkeypatch, path).save_libs(path)

    await hass.async_add_executor_job(resave)

    assert sha256(path) == recorded_checksum(hass)
    assert await async_ensure_anisette_libs(hass) == path
    assert len(download.requests) == 1


async def test_concurrent_callers_share_one_download(
    hass: HomeAssistant, download: FakeDownload
) -> None:
    """Several account entries are set up at once when Home Assistant starts."""
    paths = await asyncio.gather(*(async_ensure_anisette_libs(hass) for _ in range(3)))

    assert len(set(paths)) == 1
    assert len(download.requests) == 1


def body_ends_early(download: FakeDownload) -> None:
    download.fail_after_first_chunk = True


def body_is_cut_off(download: FakeDownload) -> None:
    # Arrives without an error, as from a server that closed the connection without having
    # announced a length.
    download.body = download.body[: len(download.body) // 2]


def body_is_empty(download: FakeDownload) -> None:
    download.body = b""


def body_is_not_an_archive(download: FakeDownload) -> None:
    download.body = b"<html>503 Service Unavailable</html>" * 100


def body_lacks_a_library(download: FakeDownload) -> None:
    download.body = make_library_archive(libraries=ANISETTE_LIBRARIES[:1])


def server_reports_an_error(download: FakeDownload) -> None:
    download.status = 503


def damage(path: Path) -> bytes:
    """Cut the bundle off, as a process killed while the library re-saves it would."""
    damaged = path.read_bytes()[:100]
    path.write_bytes(damaged)
    return damaged


async def test_damaged_bundle_is_repaired_from_a_download_with_the_recorded_checksum(
    hass: HomeAssistant,
    download: FakeDownload,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The server still sends the bundle that was recorded, so nothing new is accepted."""
    path = await async_ensure_anisette_libs(hass)
    original = path.read_bytes()
    expected = recorded_checksum(hass)
    checksum_record = path.with_name(CHECKSUM_FILE).read_bytes()
    damaged = damage(path)

    with caplog.at_level(logging.WARNING):
        assert await async_ensure_anisette_libs(hass) == path

    # One download for the repair, after the one that stored the bundle.
    assert len(download.requests) == 2
    assert path.read_bytes() == original
    assert path.with_name(CHECKSUM_FILE).read_bytes() == checksum_record
    assert storage_files(hass) == {BUNDLE_FILE, CHECKSUM_FILE}
    # The log says what happened, with the checksum of the damaged file and the recorded one.
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "replaced by a verified copy" in warning
    assert str(path) in warning
    assert hashlib.sha256(damaged).hexdigest() in warning
    assert expected in warning
    # The repaired file is a bundle a provider loads, and the next check needs no download.
    _ = await hass.async_add_executor_job(load_without_download, monkeypatch, path)
    assert await async_ensure_anisette_libs(hass) == path
    assert len(download.requests) == 2


async def test_bundle_is_not_replaced_by_a_download_with_another_checksum(
    hass: HomeAssistant, download: FakeDownload, caplog: pytest.LogCaptureFixture
) -> None:
    """Whatever happened to the file: a new checksum is never accepted on a mismatch."""
    path = await async_ensure_anisette_libs(hass)
    expected = recorded_checksum(hass)
    checksum_record = path.with_name(CHECKSUM_FILE).read_bytes()
    damaged = damage(path)
    download.body = make_library_archive("v2")

    with caplog.at_level(logging.WARNING), pytest.raises(AnisetteLibsChecksumError) as raised:
        _ = await async_ensure_anisette_libs(hass)

    message = str(raised.value)
    assert str(path) in message
    assert expected in message
    assert hashlib.sha256(damaged).hexdigest() in message
    assert raised.value.downloaded in message
    assert raised.value.downloaded not in {expected, hashlib.sha256(damaged).hexdigest()}
    assert "delete this file" in message
    assert raised.value.path == path
    # Nothing was replaced: the file is there for the user to look at.
    assert len(download.requests) == 2
    assert path.read_bytes() == damaged
    assert path.with_name(CHECKSUM_FILE).read_bytes() == checksum_record
    assert storage_files(hass) == {BUNDLE_FILE, CHECKSUM_FILE}
    assert "replaced by a verified copy" not in caplog.text

    # Asking again changes nothing, however often.
    with pytest.raises(AnisetteLibsChecksumError):
        _ = await async_ensure_anisette_libs(hass)
    assert path.read_bytes() == damaged


async def test_changed_bundle_of_full_size_is_treated_like_a_damaged_one(
    hass: HomeAssistant, download: FakeDownload
) -> None:
    """Not only a file that was cut off: any content that does not match."""
    path = await async_ensure_anisette_libs(hass)
    original = path.read_bytes()
    path.write_bytes(original + b"tampered")

    assert await async_ensure_anisette_libs(hass) == path

    assert path.read_bytes() == original
    assert len(download.requests) == 2


async def test_repair_that_times_out_is_retryable_and_replaces_nothing(
    hass: HomeAssistant, download: FakeDownload, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = await async_ensure_anisette_libs(hass)
    original = path.read_bytes()
    damaged = damage(path)
    download.hang = True
    monkeypatch.setattr(anisette_libs, "_DOWNLOAD_TIMEOUT", 0.05)

    with pytest.raises(AnisetteLibsUnavailableError) as raised:
        _ = await async_ensure_anisette_libs(hass)

    # The message tells both: why a download was needed and why it did not help.
    assert "does not match its recorded checksum" in str(raised.value)
    assert "did not finish within" in str(raised.value)
    assert str(path) in str(raised.value)
    assert download.cancelled
    assert path.read_bytes() == damaged
    assert storage_files(hass) == {BUNDLE_FILE, CHECKSUM_FILE}

    # The next attempt repairs it once the server answers.
    download.hang = False
    assert await async_ensure_anisette_libs(hass) == path
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "break_download",
    [
        body_ends_early,
        body_is_cut_off,
        body_is_empty,
        body_is_not_an_archive,
        body_lacks_a_library,
        server_reports_an_error,
    ],
)
async def test_failed_repair_download_is_retryable_and_replaces_nothing(
    hass: HomeAssistant, download: FakeDownload, break_download: Callable[[FakeDownload], None]
) -> None:
    path = await async_ensure_anisette_libs(hass)
    original = path.read_bytes()
    checksum_record = path.with_name(CHECKSUM_FILE).read_bytes()
    complete = download.body
    damaged = damage(path)
    break_download(download)

    with pytest.raises(AnisetteLibsUnavailableError):
        _ = await async_ensure_anisette_libs(hass)

    assert path.read_bytes() == damaged
    assert path.with_name(CHECKSUM_FILE).read_bytes() == checksum_record
    assert storage_files(hass) == {BUNDLE_FILE, CHECKSUM_FILE}

    download.body, download.status, download.fail_after_first_chunk = complete, 200, False
    assert await async_ensure_anisette_libs(hass) == path
    assert path.read_bytes() == original


async def test_repair_that_cannot_write_is_retryable_and_leaves_the_file(
    hass: HomeAssistant, download: FakeDownload, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E.g. a full disk, or the file being held open on Windows."""
    path = await async_ensure_anisette_libs(hass)
    damaged = damage(path)
    original_replace = Path.replace

    def replace(source: Path, target: Path) -> Path:
        if target.name == BUNDLE_FILE:
            msg = "access denied"
            raise OSError(msg)
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace)

    with pytest.raises(AnisetteLibsUnavailableError, match="could not be replaced"):
        _ = await async_ensure_anisette_libs(hass)

    assert path.read_bytes() == damaged
    assert storage_files(hass) == {BUNDLE_FILE, CHECKSUM_FILE}


async def test_bundle_that_is_whole_again_after_the_download_is_left_alone(
    hass: HomeAssistant, download: FakeDownload, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider needed longer to re-save the file than the comparison waited."""
    path = await async_ensure_anisette_libs(hass)
    original = path.read_bytes()
    _ = damage(path)
    real_download = anisette_libs._async_download
    writes: list[str] = []

    async def download_while_the_provider_finishes(hass: HomeAssistant) -> bytes:
        archive = await real_download(hass)
        path.write_bytes(original)
        return archive

    def no_write(target: Path, _data: bytes) -> None:
        writes.append(target.name)

    monkeypatch.setattr(anisette_libs, "_async_download", download_while_the_provider_finishes)
    monkeypatch.setattr(anisette_libs, "_write", no_write)

    assert await async_ensure_anisette_libs(hass) == path

    assert writes == []
    assert path.read_bytes() == original


async def test_mismatch_is_compared_again_before_it_is_reported(
    hass: HomeAssistant, download: FakeDownload, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that is re-saving the file makes it incomplete for a moment."""
    path = await async_ensure_anisette_libs(hass)
    complete = path.read_bytes()
    path.write_bytes(complete[: len(complete) // 2])
    waits: list[float] = []

    def finish_writing(delay: float) -> None:
        waits.append(delay)
        if len(waits) == 2:
            path.write_bytes(complete)

    monkeypatch.setattr(anisette_libs, "_RECHECK_DELAYS", (0.25, 0.5, 1.0))
    # Only the module's own view of `time`: the real sleep stays for everything else.
    monkeypatch.setattr(anisette_libs, "time", SimpleNamespace(sleep=finish_writing))

    assert await async_ensure_anisette_libs(hass) == path

    assert waits == [0.25, 0.5]
    assert len(download.requests) == 1


async def test_deleting_the_bundle_accepts_a_new_one(
    hass: HomeAssistant, download: FakeDownload, caplog: pytest.LogCaptureFixture
) -> None:
    """The documented way out of a mismatch, and of a bundle the server has replaced."""
    path = await async_ensure_anisette_libs(hass)
    old_checksum = recorded_checksum(hass)
    path.write_bytes(b"changed")
    download.body = make_library_archive("v2")
    with pytest.raises(AnisetteLibsChecksumError):
        _ = await async_ensure_anisette_libs(hass)

    path.unlink()
    with caplog.at_level(logging.WARNING):
        assert await async_ensure_anisette_libs(hass) == path

    # The first bundle, the attempt to repair it, and the one that is accepted.
    assert len(download.requests) == 3
    assert recorded_checksum(hass) == sha256(path) != old_checksum
    # The change of the bundle leaves a trace.
    assert old_checksum in caplog.text
    assert await async_ensure_anisette_libs(hass) == path
    assert len(download.requests) == 3


async def test_bundle_without_recorded_checksum_is_not_trusted(
    hass: HomeAssistant, download: FakeDownload, caplog: pytest.LogCaptureFixture
) -> None:
    """A file this integration did not store, e.g. one the library downloaded on its own."""
    path = anisette_libs_path(hass)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"from somewhere else")

    with caplog.at_level(logging.WARNING):
        assert await async_ensure_anisette_libs(hass) == path

    assert len(download.requests) == 1
    assert recorded_checksum(hass) == sha256(path)
    assert "no recorded checksum" in caplog.text


@pytest.mark.parametrize("record", ["", "not a checksum", "abc123  findmy.anisette_libs\n"])
async def test_unreadable_checksum_record_counts_as_none(
    hass: HomeAssistant, download: FakeDownload, record: str
) -> None:
    path = await async_ensure_anisette_libs(hass)
    path.with_name(CHECKSUM_FILE).write_text(record, encoding="ascii")

    assert await async_ensure_anisette_libs(hass) == path

    assert len(download.requests) == 2
    assert recorded_checksum(hass) == sha256(path)


async def test_download_that_hangs_ends_at_the_time_limit(
    hass: HomeAssistant, download: FakeDownload, monkeypatch: pytest.MonkeyPatch
) -> None:
    download.hang = True
    monkeypatch.setattr(anisette_libs, "_DOWNLOAD_TIMEOUT", 0.05)
    threads_before = set(threading.enumerate())

    with pytest.raises(AnisetteLibsUnavailableError, match="did not finish within"):
        _ = await async_ensure_anisette_libs(hass)

    # The request was cancelled, not abandoned, and no thread was started for it.
    assert download.cancelled
    assert set(threading.enumerate()) <= threads_before | executor_threads()
    assert storage_files(hass) == set()

    # Trying again works once the server answers.
    download.hang = False
    path = await async_ensure_anisette_libs(hass)
    assert recorded_checksum(hass) == sha256(path)


def executor_threads() -> set[threading.Thread]:
    """Pool threads of the event loop's executor; they are reused, not left behind."""
    return {
        thread
        for thread in threading.enumerate()
        if thread.name.startswith(("asyncio_", "ThreadPoolExecutor", "SyncWorker"))
    }


@pytest.mark.parametrize(
    "break_download",
    [
        body_ends_early,
        body_is_cut_off,
        body_is_empty,
        body_is_not_an_archive,
        body_lacks_a_library,
        server_reports_an_error,
    ],
)
async def test_failed_or_partial_download_leaves_nothing_behind(
    hass: HomeAssistant, download: FakeDownload, break_download: Callable[[FakeDownload], None]
) -> None:
    complete = download.body
    break_download(download)

    with pytest.raises(AnisetteLibsUnavailableError):
        _ = await async_ensure_anisette_libs(hass)

    assert storage_files(hass) == set()

    # Retryable: the next attempt downloads again and succeeds.
    download.body, download.status, download.fail_after_first_chunk = complete, 200, False
    path = await async_ensure_anisette_libs(hass)
    assert len(download.requests) == 2
    assert recorded_checksum(hass) == sha256(path)


async def test_oversized_download_is_stopped(
    hass: HomeAssistant, download: FakeDownload, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(anisette_libs, "_MAX_DOWNLOAD_SIZE", len(download.body) - 1)

    with pytest.raises(AnisetteLibsUnavailableError, match="larger than"):
        _ = await async_ensure_anisette_libs(hass)

    assert storage_files(hass) == set()


async def test_interrupted_store_leaves_no_bundle(
    hass: HomeAssistant, download: FakeDownload, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bundle appears under its name complete or not at all."""
    original_replace = Path.replace

    def replace(source: Path, target: Path) -> Path:
        if target.name == BUNDLE_FILE:
            msg = "disk full"
            raise OSError(msg)
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace)

    with pytest.raises(AnisetteLibsUnavailableError, match="could not be stored"):
        _ = await async_ensure_anisette_libs(hass)

    # The checksum is moved into place first; on its own it means "no bundle yet".
    assert storage_files(hass) == {CHECKSUM_FILE}

    monkeypatch.undo()
    monkeypatch.setattr(anisette_libs, "async_get_clientsession", lambda _hass: download)
    path = await async_ensure_anisette_libs(hass)
    assert storage_files(hass) == {BUNDLE_FILE, CHECKSUM_FILE}
    assert recorded_checksum(hass) == sha256(path)


async def test_leftovers_of_an_interrupted_write_are_removed(
    hass: HomeAssistant, download: FakeDownload
) -> None:
    """A power loss between writing a temporary file and renaming it."""
    path = anisette_libs_path(hass)
    path.parent.mkdir(parents=True)
    path.with_name(f"{BUNDLE_FILE}.tmp").write_bytes(b"half a bundle")
    path.with_name(f"{CHECKSUM_FILE}.tmp").write_bytes(b"half a checksum")

    _ = await async_ensure_anisette_libs(hass)

    assert storage_files(hass) == {BUNDLE_FILE, CHECKSUM_FILE}


async def test_files_are_read_and_written_off_the_event_loop(
    hass: HomeAssistant, download: FakeDownload, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread = threading.get_ident()
    ran_on: dict[str, int] = {}

    def spy(name: str) -> None:
        original = getattr(anisette_libs, name)

        def wrapper(*args: Any) -> Any:
            ran_on[name] = threading.get_ident()
            return original(*args)

        monkeypatch.setattr(anisette_libs, name, wrapper)

    names = ("_verify", "_convert_and_store", "_repair", "_convert", "_sha256", "_write")
    for name in names:
        spy(name)

    path = await async_ensure_anisette_libs(hass)
    _ = await async_ensure_anisette_libs(hass)
    # And the repair of a damaged bundle.
    _ = damage(path)
    _ = await async_ensure_anisette_libs(hass)

    assert set(ran_on) == set(names)
    assert loop_thread not in ran_on.values()


async def test_error_messages_carry_no_traceback_details_of_the_archive(
    hass: HomeAssistant, download: FakeDownload
) -> None:
    """The message names what went wrong in words a user can act on."""
    body_is_not_an_archive(download)

    with pytest.raises(AnisetteLibsUnavailableError) as raised:
        _ = await async_ensure_anisette_libs(hass)

    assert "incomplete or not a library bundle" in str(raised.value)
    assert "nothing was stored" in str(raised.value)
