"""Tests for Apple accounts: the state kept in config entries, and the loaded account.

A loaded account lives on the FindMy worker thread. Where a test restores one from stored state,
FindMy.py runs for real; it opens no connection until it is asked to fetch. Nothing here talks to
Apple.
"""

from __future__ import annotations

import copy
import threading
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from findmy import AsyncAppleAccount, LocalAnisetteProvider, LoginState
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady

from custom_components.findmy import account as account_module
from custom_components.findmy import anisette_libs
from custom_components.findmy.account import (
    AccountRuntime,
    FetchStatus,
    async_local_anisette_libs,
    without_password,
)
from custom_components.findmy.anisette_libs import (
    AnisetteLibsChecksumError,
    AnisetteLibsUnavailableError,
    anisette_libs_path,
)
from custom_components.findmy.storage import RuntimeStorage

from .conftest import (
    library_mock,
    make_account_state,
    make_config_entry,
    make_mock_account,
    open_account,
    worker_thread_id,
)
from .offline import FakeDownload, install_network_guard, make_library_archive

if TYPE_CHECKING:
    from collections.abc import Generator

    from findmy import AccountStateMapping
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


STORED_PASSWORD = "hunter2"


@pytest.fixture(autouse=True)
def network_guard(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """No test of this file may reach the network, see offline.py."""
    attempts = install_network_guard(monkeypatch)
    yield
    assert not attempts, f"the network was used: {attempts}"


def account_state() -> AccountStateMapping:
    return {
        "type": "account",
        "ids": {"uid": "uid", "devid": "devid"},
        "account": {"username": "user@example.com", "password": STORED_PASSWORD, "info": None},
        "login": {"state": 0, "data": {}},
        "anisette": {"type": "aniRemote", "url": "http://localhost"},
    }


def test_without_password_removes_only_the_password() -> None:
    """S1: the password must not be part of what a config entry stores."""
    state = account_state()

    stripped = without_password(state)

    assert stripped["account"]["password"] is None
    assert stripped["account"]["username"] == "user@example.com"
    assert {key: value for key, value in stripped.items() if key != "account"} == {
        key: value for key, value in state.items() if key != "account"
    }


def test_without_password_leaves_its_input_untouched() -> None:
    """Config entry data is read-only; the function has to work on a copy."""
    state = account_state()
    entry_data = MappingProxyType(state)

    _ = without_password(cast("AccountStateMapping", entry_data))  # pyright: ignore[reportInvalidCast]

    assert state["account"]["password"] == STORED_PASSWORD


async def test_worker_is_started_on_first_use_and_stopped_with_home_assistant(
    hass: HomeAssistant,
) -> None:
    storage = RuntimeStorage(hass)

    worker = storage.worker

    assert worker.is_running
    assert storage.worker is worker
    assert await worker.run_sync(lambda: 42) == 42

    await hass.async_stop(force=True)

    assert not worker.is_running


@pytest.fixture
def storage(hass: HomeAssistant) -> RuntimeStorage:
    """The storage of the test; its worker thread stops with the ``hass`` fixture."""
    return RuntimeStorage(hass)


@pytest.fixture
def restored(monkeypatch: pytest.MonkeyPatch) -> list[tuple[AsyncAppleAccount, int]]:
    """Every library account restored from stored state, with the thread that restored it."""
    accounts: list[tuple[AsyncAppleAccount, int]] = []
    original = AsyncAppleAccount.from_json

    def from_json(data: AccountStateMapping, **kwargs: Any) -> AsyncAppleAccount:
        account = original(data, **kwargs)
        accounts.append((account, threading.get_ident()))
        return account

    monkeypatch.setattr(AsyncAppleAccount, "from_json", staticmethod(from_json))
    return accounts


async def test_account_is_restored_on_the_worker_and_described_by_plain_data(
    storage: RuntimeStorage, restored: list[tuple[AsyncAppleAccount, int]]
) -> None:
    """R20: the library account never exists on Home Assistant's loop."""
    state = make_account_state(email="jane@example.com", first_name="Jane", last_name="Doe")

    account = await AccountRuntime.async_load(storage.worker, "entry-1", state)

    assert len(restored) == 1
    library_account, thread = restored[0]
    assert thread == worker_thread_id(storage)
    assert thread != threading.get_ident()
    assert account._account is library_account
    # What the entities need is available without asking the library.
    assert account.entry_id == "entry-1"
    assert account.account_name == "jane@example.com"
    assert (account.first_name, account.last_name) == ("Jane", "Doe")
    assert account.login_state is LoginState.LOGGED_IN
    assert account.reauth_required is False
    assert account.last_fetch is None
    assert account.last_fetch_result is None
    assert account.last_fetch_duration is None
    # Neither name nor e-mail end up in debug logs through the object itself.
    assert "jane" not in repr(account).lower()
    # The worker closes the account if Home Assistant stops while it is loaded.
    assert list(storage.worker._stop_callbacks.values()) == [library_account.close]


async def test_restored_account_holds_no_password(
    storage: RuntimeStorage, restored: list[tuple[AsyncAppleAccount, int]]
) -> None:
    """S1: an entry written before the migration, or by an old flow, still has a password."""
    state = make_account_state(password=STORED_PASSWORD)
    untouched = copy.deepcopy(state)

    account = await AccountRuntime.async_load(storage.worker, "entry-1", state)

    library_account, _ = restored[0]
    assert library_account._password is None
    assert library_account.to_json()["account"]["password"] is None
    assert STORED_PASSWORD not in repr(vars(account))
    # The data of the entry is neither changed nor shared with the worker thread.
    assert state == untouched
    assert library_account._login_state_data is not state["login"]["data"]


def _damaged(change: dict[str, Any]) -> dict[str, Any]:
    return {**make_account_state(), **change}


@pytest.mark.parametrize(
    "account_data",
    [
        pytest.param(None, id="missing"),
        pytest.param({}, id="empty"),
        pytest.param({"type": "account"}, id="only-a-type"),
        pytest.param("C:/some/file.json", id="a-path-instead-of-data"),
        pytest.param([1, 2, 3], id="a-list"),
        pytest.param(_damaged({"type": "accessory"}), id="wrong-type"),
        pytest.param(_damaged({"ids": {}}), id="no-ids"),
        pytest.param(_damaged({"account": None}), id="no-account"),
        pytest.param(_damaged({"login": {"state": 99, "data": {}}}), id="unknown-login-state"),
        pytest.param(_damaged({"login": {"state": 3}}), id="no-login-data"),
        pytest.param(_damaged({"anisette": None}), id="no-anisette"),
        pytest.param(_damaged({"anisette": {"type": "aniOther"}}), id="unknown-anisette"),
        pytest.param(_damaged({"anisette": {"type": "aniRemote"}}), id="anisette-without-url"),
    ],
)
async def test_damaged_account_data_is_reported_as_config_entry_error(
    storage: RuntimeStorage, account_data: Any
) -> None:
    """R10: this used to fail the setup with whatever traceback FindMy.py produced."""
    with pytest.raises(ConfigEntryError, match="cannot be restored") as raised:
        _ = await AccountRuntime.async_load(storage.worker, "entry-1", account_data)

    # The message says what to do, and nothing from the stored session.
    assert "add the account again" in str(raised.value)
    assert "search-party-token" not in str(raised.value)
    assert storage.worker._stop_callbacks == {}


@pytest.mark.parametrize(
    "login_state",
    [LoginState.LOGGED_OUT, LoginState.REQUIRE_2FA, LoginState.AUTHENTICATED],
)
async def test_account_that_is_not_signed_in_asks_for_reauthentication(
    storage: RuntimeStorage,
    restored: list[tuple[AsyncAppleAccount, int]],
    login_state: LoginState,
) -> None:
    """R10: such an account was loaded as if all was well and failed on its first fetch."""
    state = make_account_state(login_state=login_state)

    with pytest.raises(ConfigEntryAuthFailed, match=login_state.name):
        _ = await AccountRuntime.async_load(storage.worker, "entry-1", state)

    # Nothing is left behind on the worker.
    library_account, _ = restored[0]
    assert library_account._closed is True
    assert storage.worker._stop_callbacks == {}


async def test_fetch_takes_login_state_and_errors_over_from_the_worker(
    storage: RuntimeStorage,
) -> None:
    account = await open_account(storage, "a")
    library_account = library_mock(account)
    error = aiohttp.ClientConnectionError("connection reset")
    fetch_threads: list[int] = []

    async def fetch(_sources: object) -> dict[object, object]:
        fetch_threads.append(threading.get_ident())
        library_account.login_state = LoginState.REQUIRE_2FA
        raise error

    library_account.fetch_location.side_effect = fetch

    with pytest.raises(aiohttp.ClientConnectionError) as raised:
        _ = await account.async_fetch([], timeout=5)

    assert raised.value is error
    assert fetch_threads == [worker_thread_id(storage)]
    assert account.login_state is LoginState.REQUIRE_2FA


async def test_fetch_state_follows_start_and_end(storage: RuntimeStorage) -> None:
    account = await open_account(storage, "a")

    account.fetch_started()
    assert account.last_fetch_result is FetchStatus.ONGOING
    assert account.last_fetch is None

    account.fetch_finished(success=False)
    assert account.last_fetch_result is FetchStatus.ERROR
    first_end = account.last_fetch
    assert first_end is not None
    assert account.last_fetch_duration is not None

    account.fetch_started()
    # The end of the previous fetch stays readable while the next one runs.
    assert account.last_fetch == first_end
    assert account.last_fetch_duration is None

    account.fetch_finished(success=True)
    assert account.last_fetch_result is FetchStatus.SUCCESS
    assert account.last_fetch >= first_end


async def test_close_runs_on_the_worker_and_releases_the_stop_callback(
    storage: RuntimeStorage,
) -> None:
    """R13: the sessions of an account belong to the worker's loop and are closed there."""
    account = await open_account(storage, "a")
    library_account = library_mock(account)
    close_threads: list[int] = []
    library_account.close.side_effect = lambda: close_threads.append(threading.get_ident())
    assert len(storage.worker._stop_callbacks) == 1

    await account.async_close()

    assert close_threads == [worker_thread_id(storage)]
    assert storage.worker._stop_callbacks == {}


async def test_close_after_the_worker_stopped_does_not_raise(storage: RuntimeStorage) -> None:
    """Home Assistant stops the worker first; entries may still be unloaded afterwards."""
    account = await open_account(storage, "a")
    library_account = library_mock(account)

    await storage.worker.async_stop()
    await account.async_close()

    # Closed once, by the worker itself when it stopped.
    library_account.close.assert_awaited_once()


def account_entry(hass: HomeAssistant, state: AccountStateMapping) -> ConfigEntry:
    return make_config_entry(hass, {"type": "account", "account_data": state})


@pytest.fixture
def entry_updates(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Counts the writes to config entries, which still go through to the real registry."""
    spy = MagicMock(wraps=hass.config_entries.async_update_entry)
    monkeypatch.setattr(hass.config_entries, "async_update_entry", spy)
    return spy


@pytest.fixture
def reloads(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Every way Home Assistant has to reload an entry, replaced by one mock."""
    spy = MagicMock()
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", spy)
    monkeypatch.setattr(hass.config_entries, "async_reload", spy)
    return spy


async def test_renewed_session_is_written_to_the_entry_exactly_once(
    hass: HomeAssistant, storage: RuntimeStorage, entry_updates: MagicMock, reloads: MagicMock
) -> None:
    """R5: renewed tokens were lost on every restart."""
    stored = make_account_state(token="old-token")
    entry = account_entry(hass, stored)
    library_account = make_mock_account(reports={})
    library_account.to_json.return_value = copy.deepcopy(stored)
    account = await open_account(storage, entry.entry_id, library_account)
    renewed = make_account_state(token="new-token")
    # The library keeps a password it was given; it must not come back through this path.
    renewed["account"]["password"] = STORED_PASSWORD

    for _ in range(3):
        _ = await account.async_fetch([], timeout=5)
        assert account.persist_session(hass) is False
    entry_updates.assert_not_called()

    library_account.to_json.return_value = renewed
    _ = await account.async_fetch([], timeout=5)
    assert account.persist_session(hass) is True

    assert entry.data["type"] == "account"
    assert entry.data["account_data"] == make_account_state(token="new-token")
    assert entry.data["account_data"]["account"]["password"] is None
    # A copy: the library goes on working with the dicts it serialised.
    assert entry.data["account_data"]["login"]["data"] is not renewed["login"]["data"]

    for _ in range(3):
        _ = await account.async_fetch([], timeout=5)
        assert account.persist_session(hass) is False
    entry_updates.assert_called_once()
    # Writing the entry must not reload it, or every renewal would restart the account.
    reloads.assert_not_called()
    assert entry.update_listeners == []


async def test_session_renewed_by_a_fetch_that_failed_is_stored_too(
    hass: HomeAssistant, storage: RuntimeStorage
) -> None:
    stored = make_account_state(token="old-token")
    entry = account_entry(hass, stored)
    library_account = make_mock_account(reports={})
    library_account.to_json.return_value = copy.deepcopy(stored)
    account = await open_account(storage, entry.entry_id, library_account)

    async def fetch(_sources: object) -> dict[object, object]:
        library_account.to_json.return_value = make_account_state(token="new-token")
        raise aiohttp.ClientConnectionError

    library_account.fetch_location.side_effect = fetch

    with pytest.raises(aiohttp.ClientConnectionError):
        _ = await account.async_fetch([], timeout=5)

    assert account.persist_session(hass) is True
    assert entry.data["account_data"] == make_account_state(token="new-token")


async def test_password_left_in_an_entry_is_removed_by_the_first_fetch(
    hass: HomeAssistant, storage: RuntimeStorage, entry_updates: MagicMock
) -> None:
    """S1: whatever wrote the entry, a password does not survive the account being used."""
    entry = account_entry(hass, make_account_state(password=STORED_PASSWORD))
    account = await AccountRuntime.async_load(
        storage.worker, entry.entry_id, entry.data["account_data"]
    )

    # The library for real; without sources it sends no request.
    assert await account.async_fetch([], timeout=5) == {}
    assert account.persist_session(hass) is True

    assert entry.data["account_data"] == make_account_state(password=None)

    _ = await account.async_fetch([], timeout=5)
    assert account.persist_session(hass) is False
    entry_updates.assert_called_once()


async def test_new_sign_in_is_not_overwritten_by_the_session_it_replaces(
    hass: HomeAssistant, storage: RuntimeStorage
) -> None:
    """The user signs in again while a fetch of the expired session is still running."""
    stored = make_account_state(token="expired-token")
    entry = account_entry(hass, stored)
    library_account = make_mock_account(reports={})
    library_account.to_json.return_value = copy.deepcopy(stored)
    account = await open_account(storage, entry.entry_id, library_account)
    signed_in_again = make_account_state(token="fresh-token")

    async def fetch(_sources: object) -> dict[object, object]:
        library_account.to_json.return_value = make_account_state(token="changed-while-expired")
        return {}

    library_account.fetch_location.side_effect = fetch
    # What the re-authentication flow does, just before it reloads the entry.
    _ = hass.config_entries.async_update_entry(
        entry, data={"type": "account", "account_data": signed_in_again}
    )
    _ = await account.async_fetch([], timeout=5)

    assert account.persist_session(hass) is False
    assert entry.data["account_data"] == signed_in_again


async def test_session_of_a_removed_entry_is_not_stored(
    hass: HomeAssistant, storage: RuntimeStorage, entry_updates: MagicMock
) -> None:
    library_account = make_mock_account(reports={})
    account = await open_account(storage, "removed-entry", library_account)
    library_account.to_json.return_value = make_account_state(token="new-token")
    _ = await account.async_fetch([], timeout=5)

    assert account.persist_session(hass) is False
    entry_updates.assert_not_called()


###############################################
### M3: the library bundle of local Anisette ###
###############################################


def local_account_state() -> AccountStateMapping:
    """A signed-in account that uses the integrated Anisette provider."""
    state = make_account_state()
    return {**state, "anisette": {"type": "aniLocal", "prov_data": None}}


@pytest.fixture
def ensure_libs(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The bundle is "in place"; anisette_libs.py has tests of its own."""
    mock = AsyncMock(return_value=anisette_libs_path(hass))
    monkeypatch.setattr(account_module, "async_ensure_anisette_libs", mock)
    return mock


async def test_local_anisette_account_is_restored_with_the_library_bundle(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    restored: list[tuple[AsyncAppleAccount, int]],
    ensure_libs: AsyncMock,
) -> None:
    """Without the path the anisette package downloads its libraries after every start."""
    state = local_account_state()

    path = await async_local_anisette_libs(hass, state)
    account = await AccountRuntime.async_load(
        storage.worker, "entry-1", state, anisette_libs_path=path
    )

    ensure_libs.assert_awaited_once_with(hass)
    assert path == anisette_libs_path(hass)
    library_account, _thread = restored[0]
    provider = library_account._anisette
    assert isinstance(provider, LocalAnisetteProvider)
    assert provider._libs_path == path
    assert account.login_state is LoginState.LOGGED_IN


@pytest.mark.parametrize(
    "account_data",
    [
        pytest.param(make_account_state(), id="remote-server"),
        pytest.param(None, id="missing"),
        pytest.param("damaged", id="not-a-mapping"),
        pytest.param({"anisette": "aniLocal"}, id="anisette-not-a-mapping"),
        pytest.param({"anisette": {"type": "aniOther"}}, id="unknown-provider"),
    ],
)
async def test_only_local_anisette_accounts_need_the_library_bundle(
    hass: HomeAssistant, ensure_libs: AsyncMock, account_data: Any
) -> None:
    """Damaged data is left to the restore, which reports it."""
    assert await async_local_anisette_libs(hass, account_data) is None

    ensure_libs.assert_not_awaited()


async def test_remote_anisette_account_is_restored_without_a_bundle(
    storage: RuntimeStorage, restored: list[tuple[AsyncAppleAccount, int]]
) -> None:
    _ = await AccountRuntime.async_load(storage.worker, "entry-1", make_account_state())

    assert len(restored) == 1


async def test_changed_library_bundle_fails_the_setup_with_instructions(
    hass: HomeAssistant, ensure_libs: AsyncMock
) -> None:
    """Not retried: only the user can decide to accept another bundle."""
    path = anisette_libs_path(hass)
    ensure_libs.side_effect = AnisetteLibsChecksumError(path, "a" * 64, "b" * 64)

    with pytest.raises(ConfigEntryError) as raised:
        _ = await async_local_anisette_libs(hass, local_account_state())

    assert str(path) in str(raised.value)
    assert "delete this file" in str(raised.value)


async def test_missing_library_bundle_makes_the_setup_retry(
    hass: HomeAssistant, ensure_libs: AsyncMock
) -> None:
    ensure_libs.side_effect = AnisetteLibsUnavailableError("the download timed out")

    with pytest.raises(ConfigEntryNotReady, match="the download timed out"):
        _ = await async_local_anisette_libs(hass, local_account_state())


async def test_local_anisette_account_is_not_restored_without_the_bundle(
    storage: RuntimeStorage, restored: list[tuple[AsyncAppleAccount, int]]
) -> None:
    """A caller that skips the check must not quietly get the library's own download back."""
    with pytest.raises(RuntimeError, match="needs anisette_libs_path"):
        _ = await AccountRuntime.async_load(storage.worker, "entry-1", local_account_state())

    assert restored == []
    assert storage.worker._stop_callbacks == {}


async def test_storage_restores_accounts_with_the_library_bundle(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    restored: list[tuple[AsyncAppleAccount, int]],
    ensure_libs: AsyncMock,
) -> None:
    """The one place that loads the account of a config entry."""
    entry = account_entry(hass, local_account_state())

    account = await storage.add_entry(entry)

    assert isinstance(account, AccountRuntime)
    ensure_libs.assert_awaited_once_with(hass)
    library_account, _thread = restored[0]
    assert cast("Any", library_account._anisette)._libs_path == anisette_libs_path(hass)


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        (AnisetteLibsUnavailableError("the download timed out"), ConfigEntryNotReady),
        (AnisetteLibsChecksumError(Path("bundle"), "a" * 64, "b" * 64), ConfigEntryError),
    ],
)
async def test_storage_restores_no_account_without_a_usable_bundle(  # noqa: PLR0917
    hass: HomeAssistant,
    storage: RuntimeStorage,
    restored: list[tuple[AsyncAppleAccount, int]],
    ensure_libs: AsyncMock,
    error: Exception,
    raised: type[Exception],
) -> None:
    """No provider may exist that could download the libraries on its own."""
    entry = account_entry(hass, local_account_state())
    ensure_libs.side_effect = error

    with pytest.raises(raised):
        _ = await storage.add_entry(entry)

    assert restored == []
    assert storage.accounts == []


async def test_storage_restores_remote_anisette_accounts_without_a_bundle(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    restored: list[tuple[AsyncAppleAccount, int]],
    ensure_libs: AsyncMock,
) -> None:
    entry = account_entry(hass, make_account_state())

    _ = await storage.add_entry(entry)

    ensure_libs.assert_not_awaited()
    assert len(restored) == 1


async def test_accounts_share_one_stored_bundle_across_restarts(
    hass: HomeAssistant,
    storage: RuntimeStorage,
    restored: list[tuple[AsyncAppleAccount, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real bundle store behind two account entries; only the download is a fake."""
    download = FakeDownload(make_library_archive())
    monkeypatch.setattr(anisette_libs, "async_get_clientsession", lambda _hass: download)
    first = account_entry(hass, local_account_state())
    second = account_entry(hass, local_account_state())

    _ = await storage.add_entry(first)
    _ = await storage.add_entry(second)
    # What the next start of Home Assistant does with the first entry.
    _ = await storage.del_entry(first)
    _ = await storage.add_entry(first)

    assert len(download.requests) == 1
    path = anisette_libs_path(hass)
    assert path.is_file()
    providers = [cast("Any", account._anisette) for account, _thread in restored]
    assert [provider._libs_path for provider in providers] == [path] * 3
