"""Shared fixtures for the FindMy integration tests.

Provides a minimal ``hass`` instance and factories for FindMy library objects. Nothing here
talks to Apple: accounts are mocks, and accessories and key pairs use random key material so
that key derivation and matching run for real.

pytest-homeassistant-custom-component is deliberately not used; it cannot be loaded on
Windows (see tests/README.md).
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from collections.abc import AsyncGenerator, Callable, Generator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from findmy import AsyncAppleAccount, FindMyAccessory, KeyPair, KeyPairType, LoginState
from homeassistant import core as ha_core
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

if TYPE_CHECKING:
    from pathlib import Path

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


@pytest.fixture
def mock_account_factory() -> AccountFactory:
    """Create ``AsyncAppleAccount`` mocks that are logged in and return no reports.

    The spec makes typos in attribute names fail loudly. Tests set the return values of
    ``fetch_location`` and friends as needed.
    """

    def create(
        *,
        email: str | None = "user@example.com",
        first_name: str | None = "Jane",
        last_name: str | None = "Doe",
        login_state: LoginState = LoginState.LOGGED_IN,
    ) -> MagicMock:
        account = MagicMock(spec=AsyncAppleAccount)
        account.account_name = email
        account.first_name = first_name
        account.last_name = last_name
        account.login_state = login_state
        account.login = AsyncMock(return_value=login_state)
        account.fetch_location = AsyncMock(return_value=None)
        account.fetch_location_history = AsyncMock(return_value=None)
        account.close = AsyncMock(return_value=None)
        return account

    return create


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
