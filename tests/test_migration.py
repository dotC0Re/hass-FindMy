"""Tests for the migration of stored config entries.

Version 2.1 removed the Apple ID password from account entries (finding S1); version 2.0 renamed
an entry type. Entries are real config entries in a real registry, but nothing is set up.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from custom_components.findmy import async_migrate_entry
from custom_components.findmy.const import CONFIG_FLOW_VERSION_MAJOR, CONFIG_FLOW_VERSION_MINOR

from .conftest import make_account_state, make_config_entry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

CURRENT_VERSION = (CONFIG_FLOW_VERSION_MAJOR, CONFIG_FLOW_VERSION_MINOR)


async def test_migration_renames_the_old_rolling_device_type(hass: HomeAssistant) -> None:
    """The existing 1 -> 2 migration."""
    device = {"master_key": "00", "name": "Tag"}
    entry = make_config_entry(
        hass, {"type": "device_rolling", "data": device}, version=1, minor_version=1
    )

    assert await async_migrate_entry(hass, entry) is True

    assert entry.data == {"type": "device_rolling_derived", "data": device}
    assert (entry.version, entry.minor_version) == CURRENT_VERSION


@pytest.mark.parametrize(("version", "minor_version"), [(1, 1), (2, 0)])
async def test_migration_removes_the_stored_password(
    hass: HomeAssistant, version: int, minor_version: int
) -> None:
    """S1: the Apple ID password was stored in plain text, and in every backup."""
    stored = make_account_state(password="hunter2")
    untouched = copy.deepcopy(stored)
    entry = make_config_entry(
        hass,
        {"type": "account", "account_data": stored},
        version=version,
        minor_version=minor_version,
    )

    assert await async_migrate_entry(hass, entry) is True

    assert entry.data == {"type": "account", "account_data": make_account_state(password=None)}
    assert "hunter2" not in repr(entry.data)
    assert (entry.version, entry.minor_version) == CURRENT_VERSION
    # The old data object is not changed in place; Home Assistant compares old and new.
    assert stored == untouched


async def test_migration_keeps_an_account_entry_that_is_already_clean(
    hass: HomeAssistant,
) -> None:
    stored = make_account_state(password=None)
    entry = make_config_entry(
        hass, {"type": "account", "account_data": stored}, version=2, minor_version=0
    )

    assert await async_migrate_entry(hass, entry) is True

    assert entry.data == {"type": "account", "account_data": stored}
    assert entry.data["account_data"] is stored
    # Still moved to the current version, or it would be migrated again on every start.
    assert (entry.version, entry.minor_version) == CURRENT_VERSION


@pytest.mark.parametrize(
    "data",
    [
        {"type": "device_static", "data": {"private_key": "AA=="}},
        {"type": "device_rolling_derived", "data": {"master_key": "00"}},
        # Damaged account entries are reported when they are set up, not by the migration.
        {"type": "account"},
        {"type": "account", "account_data": "broken"},
        {"type": "account", "account_data": {"account": None}},
    ],
)
async def test_migration_leaves_other_data_alone(hass: HomeAssistant, data: dict[str, Any]) -> None:
    entry = make_config_entry(hass, copy.deepcopy(data), version=2, minor_version=0)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.data == data
    assert (entry.version, entry.minor_version) == CURRENT_VERSION


@pytest.mark.parametrize(
    "minor_version", [CONFIG_FLOW_VERSION_MINOR, CONFIG_FLOW_VERSION_MINOR + 4]
)
async def test_current_and_newer_minor_versions_are_not_migrated(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, minor_version: int
) -> None:
    """Home Assistant also asks after a downgrade; the stored version must not be lowered."""
    entry = make_config_entry(
        hass,
        {"type": "account", "account_data": make_account_state(password="hunter2")},
        version=CONFIG_FLOW_VERSION_MAJOR,
        minor_version=minor_version,
    )
    update = MagicMock()
    monkeypatch.setattr(hass.config_entries, "async_update_entry", update)

    assert await async_migrate_entry(hass, entry) is True

    update.assert_not_called()
    assert entry.minor_version == minor_version
