"""Tests for the account state kept in config entries and for the shared worker."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from custom_components.findmy.account import without_password
from custom_components.findmy.storage import RuntimeStorage

if TYPE_CHECKING:
    from findmy import AccountStateMapping
    from homeassistant.core import HomeAssistant


STORED_PASSWORD = "hunter2"


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
