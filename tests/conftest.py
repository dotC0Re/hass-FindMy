"""Shared fixtures for the FindMy integration tests.

Provides a minimal ``hass`` instance and factories for FindMy library objects. Nothing here
talks to Apple: accounts are mocks, and accessories and key pairs use random key material so
that key derivation and matching run for real.

Accounts live on the FindMy worker thread (see account.py). ``open_account`` puts a mocked
library account there the way a config entry would; the thread is stopped together with the
``hass`` instance of the test.

An account fetches at most once per 15 minutes (budget.py). Tests that fetch several times in
a row let that time pass with the ``fetch_clock`` fixture instead of waiting.

pytest-homeassistant-custom-component is deliberately not used; it cannot be loaded on
Windows (see tests/README.md).
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from collections.abc import AsyncGenerator, Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from findmy import AsyncAppleAccount, FindMyAccessory, KeyPair, KeyPairType, LoginState
from homeassistant import core as ha_core
from homeassistant import loader
from homeassistant.config_entries import SOURCE_USER, ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

from custom_components.findmy import coordinator as coordinator_module
from custom_components.findmy.account import AccountRuntime
from custom_components.findmy.budget import MIN_FETCH_INTERVAL
from custom_components.findmy.const import (
    CONFIG_FLOW_VERSION_MAJOR,
    CONFIG_FLOW_VERSION_MINOR,
    DOMAIN,
)

if TYPE_CHECKING:
    from pathlib import Path

    from findmy import AccountStateMapping

    from custom_components.findmy.storage import RuntimeStorage

type AccountFactory = Callable[..., MagicMock]
type AccessoryFactory = Callable[..., FindMyAccessory]
type KeyPairFactory = Callable[..., KeyPair]

# One primary key per 15 minutes.
KEYS_PER_DAY = 96


def _bluetooth_importable() -> bool:
    try:
        importlib.import_module("homeassistant.components.bluetooth")
    except ImportError:
        return False
    return True


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip Bluetooth-dependent tests on Windows if the component cannot be imported.

    With the dev dependencies installed it imports fine on Windows too; the skip only keeps
    a partial local environment usable. On other platforms a failing import must fail the
    tests, so CI cannot silently lose coverage.
    """
    marked = [item for item in items if item.get_closest_marker("requires_bluetooth")]
    if not marked or sys.platform != "win32" or _bluetooth_importable():
        return
    skip = pytest.mark.skip(reason="homeassistant.components.bluetooth is not importable")
    for item in marked:
        item.add_marker(skip)


@dataclass
class FetchClock:
    """The time the coordinator's fetch budget sees; moves only when a test says so."""

    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta = MIN_FETCH_INTERVAL) -> None:
        """Let time pass, by default the 15 minutes after which an account may fetch again."""
        self.now += delta


@pytest.fixture
def fetch_clock(monkeypatch: pytest.MonkeyPatch) -> FetchClock:
    """Replace the clock of the fetch budget, starting at the real time.

    Only the budget's decisions follow it. Key derivation and catch-up windows keep using the
    real time, which is why it starts there.
    """
    clock = FetchClock(datetime.now(tz=UTC))
    monkeypatch.setattr(coordinator_module, "_utcnow", clock)
    return clock


@pytest.fixture(autouse=True)
def forget_home_assistant_instance() -> Generator[None]:
    """Clear the instance Home Assistant remembers globally once a test is over.

    Creating a ``HomeAssistant`` stores it in a thread-local that nothing clears, and
    ``frame.async_setup`` keeps a second reference. Code that looks the instance up later,
    such as a deprecation report issued while a module is imported, would otherwise find the
    stopped instance of an earlier test and fail on its missing data.
    """
    yield
    ha_core._hass.hass = None
    frame._hass.hass = None


@pytest.fixture
async def hass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[HomeAssistant]:
    """A Home Assistant instance with config entries, but no integrations set up."""
    instance = HomeAssistant(str(tmp_path))
    # Requirements are installed by uv; never let a test try to pip-install anything.
    instance.config.skip_pip = True
    loader.async_setup(instance)
    frame.async_setup(instance)
    if sys.platform == "win32":
        # frame looks for "custom_components/" with a forward slash. On Windows it finds no
        # integration frame, treats the caller as core and raises RuntimeError.
        monkeypatch.setattr(frame, "report_usage", lambda *_args, **_kwargs: None)
    instance.config_entries = ConfigEntries(instance, {})
    await instance.config_entries.async_initialize()

    yield instance

    await instance.async_stop(force=True)


def make_account_state(
    *,
    email: str = "user@example.com",
    password: str | None = None,
    first_name: str = "Jane",
    last_name: str = "Doe",
    login_state: LoginState = LoginState.LOGGED_IN,
    token: str = "search-party-token",  # noqa: S107  # dummy value in test data
) -> AccountStateMapping:
    """Serialised account state as a config entry stores it; FindMy.py can restore it.

    The Anisette provider is a remote one, which opens no connection until it is used.
    """
    return {
        "type": "account",
        "ids": {"uid": "uid", "devid": "devid"},
        "account": {
            "username": email,
            "password": password,
            "info": {
                "account_name": email,
                "first_name": first_name,
                "last_name": last_name,
                "trusted_device_2fa": False,
            },
        },
        "login": {
            "state": login_state.value,
            "data": {"dsid": "1234", "mobileme_data": {"tokens": {"searchPartyToken": token}}},
        },
        "anisette": {"type": "aniRemote", "url": "http://anisette.invalid"},
    }


def make_mock_account(
    *,
    email: str = "user@example.com",
    first_name: str | None = "Jane",
    last_name: str | None = "Doe",
    login_state: LoginState = LoginState.LOGGED_IN,
    reports: object = None,
) -> MagicMock:
    """An ``AsyncAppleAccount`` mock that is logged in and returns no reports.

    The spec makes typos in attribute names fail loudly. Tests set the return values of
    ``fetch_location`` and friends as needed; ``reports`` is what both return at first. A real
    account answers a list of sources with a dict, which is what the coordinator needs.
    """
    account = MagicMock(spec=AsyncAppleAccount)
    account.account_name = email
    account.first_name = first_name
    account.last_name = last_name
    account.login_state = login_state
    account.login = AsyncMock(return_value=login_state)
    account.fetch_location = AsyncMock(return_value=reports)
    account.fetch_location_history = AsyncMock(return_value=reports)
    account.close = AsyncMock(return_value=None)
    account.to_json.return_value = make_account_state(email=email, login_state=login_state)
    return account


@pytest.fixture
def mock_account_factory() -> AccountFactory:
    """Create ``AsyncAppleAccount`` mocks, see :func:`make_mock_account`."""
    return make_mock_account


async def open_account(
    storage: RuntimeStorage, entry_id: str, account: MagicMock | None = None
) -> AccountRuntime:
    """Store a mocked library account under an entry id, opened on the worker like a real one.

    Returns what Home Assistant's side holds; the mock is reachable as ``._account``.
    """
    account = account or make_mock_account(email=f"{entry_id}@example.com", reports={})
    runtime = await AccountRuntime._async_open(
        storage.worker,
        entry_id,
        lambda: account,
        # What the entry of this account would hold, see make_config_entry.
        account.to_json.return_value,
    )
    storage._entries[entry_id] = runtime
    return runtime


def library_mock(account: AccountRuntime) -> MagicMock:
    """The mocked library account behind an account stored with :func:`open_account`."""
    return cast("MagicMock", account._account)


def worker_thread_id(storage: RuntimeStorage) -> int:
    """The id of the FindMy worker thread, to tell where something ran."""
    thread = storage.worker._thread
    assert thread is not None
    assert thread.ident is not None
    return thread.ident


def make_config_entry(
    hass: HomeAssistant,
    data: dict[str, Any],
    *,
    version: int = CONFIG_FLOW_VERSION_MAJOR,
    minor_version: int = CONFIG_FLOW_VERSION_MINOR,
    entry_id: str | None = None,
) -> ConfigEntry:
    """Register a config entry of the integration without setting it up.

    Needs the ``hass`` fixture of this module, whose config entry registry is a real one.
    """
    entry = ConfigEntry(
        data=data,
        discovery_keys=MappingProxyType({}),
        domain=DOMAIN,
        entry_id=entry_id,
        minor_version=minor_version,
        options={},
        source=SOURCE_USER,
        subentries_data=None,
        title="Test entry",
        unique_id=None,
        version=version,
    )
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


@pytest.fixture
def accessory_factory() -> AccessoryFactory:
    """Create ``FindMyAccessory`` objects with random key material.

    By default the accessory was paired two days ago and is aligned to the current time, so
    deriving keys around "now" stays cheap. Pass ``alignment_date``/``alignment_index`` to
    model an accessory that has not been seen for a while.
    """

    def create(
        *,
        name: str | None = "Test Tag",
        identifier: str | None = None,
        paired_at: datetime | None = None,
        alignment_date: datetime | None = None,
        alignment_index: int | None = None,
    ) -> FindMyAccessory:
        now = datetime.now(UTC)
        return FindMyAccessory(
            master_key=os.urandom(28),
            skn=os.urandom(32),
            sks=os.urandom(32),
            paired_at=paired_at or now - timedelta(days=2),
            name=name,
            identifier=identifier or str(uuid.uuid4()).upper(),
            alignment_date=alignment_date or now,
            alignment_index=2 * KEYS_PER_DAY if alignment_index is None else alignment_index,
        )

    return create


@pytest.fixture
def keypair_factory() -> KeyPairFactory:
    """Create static ``KeyPair`` objects (e.g. OpenHaystack tags) with random keys."""

    def create(
        *,
        key_type: KeyPairType = KeyPairType.UNKNOWN,
        name: str | None = "Test Key",
    ) -> KeyPair:
        return KeyPair(os.urandom(28), key_type=key_type, name=name)

    return create
