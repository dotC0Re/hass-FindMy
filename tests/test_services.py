"""Tests for the `findmy.delete_devices` service (S3, R15)."""

# pyright: basic, reportAttributeAccessIssue=false

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized, UnknownUser

# Home Assistant swaps voluptuous for probatio at import time, so its errors
# are probatio's, not voluptuous'.
from probatio import Invalid

from custom_components.findmy import services
from custom_components.findmy.const import DOMAIN

if TYPE_CHECKING:
    from pathlib import Path

ENTRIES = [
    ("acc", "Apple account", "account"),
    ("stat", "Static tag", "device_static"),
    ("der", "Derived tag", "device_rolling_derived"),
    ("pre", "Pre-generated tag", "device_rolling_pre_generated"),
]

EXPECTED = {
    "all": {"stat", "der", "pre"},
    "static": {"stat"},
    "rolling": {"der", "pre"},
    "rolling_derived": {"der"},
    "rolling_pre_generated": {"pre"},
}


def _entry(entry_id: str, title: str, entry_type: str) -> SimpleNamespace:
    return SimpleNamespace(entry_id=entry_id, title=title, data={"type": entry_type})


@pytest_asyncio.fixture
async def hass(tmp_path: Path):
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = SimpleNamespace(
        async_entries=MagicMock(return_value=[_entry(*e) for e in ENTRIES]),
        async_remove=AsyncMock(return_value={"require_restart": False}),
    )
    hass.auth = MagicMock()
    services.async_register(hass)
    yield hass
    await hass.async_stop(force=True)


def _removed_ids(hass) -> set[str]:
    return {c.args[0] for c in hass.config_entries.async_remove.await_args_list}


async def _call(hass, data, context: Context | None = None):
    return await hass.services.async_call(
        DOMAIN,
        services.SERVICE_DELETE_DEVICES,
        data,
        blocking=True,
        return_response=True,
        context=context,
    )


def test_schema_requires_filter_and_confirm() -> None:
    with pytest.raises(Invalid):
        services.DELETE_SCHEMA({})
    with pytest.raises(Invalid):
        services.DELETE_SCHEMA({"confirm": True})
    with pytest.raises(Invalid):
        services.DELETE_SCHEMA({"filter": "all"})
    with pytest.raises(Invalid):
        services.DELETE_SCHEMA({"filter": "openhaystack", "confirm": True})
    assert services.DELETE_SCHEMA({"filter": "rolling", "confirm": True}) == {
        "filter": "rolling",
        "confirm": True,
    }


@pytest.mark.asyncio
async def test_bare_call_deletes_nothing(hass) -> None:
    with pytest.raises(Invalid):
        await _call(hass, {})
    hass.config_entries.async_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_false_is_rejected(hass) -> None:
    with pytest.raises(ServiceValidationError, match="confirm"):
        await _call(hass, {"filter": "all", "confirm": False})
    hass.config_entries.async_remove.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("filter_type", list(EXPECTED))
async def test_filter_matches_exactly_the_right_entries(hass, filter_type) -> None:
    response = await _call(hass, {"filter": filter_type, "confirm": True})

    assert _removed_ids(hass) == EXPECTED[filter_type]
    assert "acc" not in _removed_ids(hass)
    assert response["count"] == len(EXPECTED[filter_type])
    assert {r["entry_id"] for r in response["removed"]} == EXPECTED[filter_type]
    titles = {e[0]: e[1] for e in ENTRIES}
    assert {r["title"] for r in response["removed"]} == {titles[i] for i in EXPECTED[filter_type]}


@pytest.mark.asyncio
async def test_no_match_returns_empty_response(hass) -> None:
    hass.config_entries.async_entries.return_value = [_entry(*ENTRIES[0])]
    response = await _call(hass, {"filter": "all", "confirm": True})
    assert response == {"count": 0, "removed": []}
    hass.config_entries.async_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_admin_is_rejected(hass) -> None:
    hass.auth.async_get_user = AsyncMock(return_value=SimpleNamespace(is_admin=False))
    with pytest.raises(Unauthorized):
        await _call(hass, {"filter": "all", "confirm": True}, Context(user_id="user-1"))
    hass.config_entries.async_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_user_is_rejected(hass) -> None:
    hass.auth.async_get_user = AsyncMock(return_value=None)
    with pytest.raises(UnknownUser):
        await _call(hass, {"filter": "all", "confirm": True}, Context(user_id="ghost"))
    hass.config_entries.async_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_is_allowed(hass) -> None:
    hass.auth.async_get_user = AsyncMock(return_value=SimpleNamespace(is_admin=True))
    response = await _call(hass, {"filter": "static", "confirm": True}, Context(user_id="admin"))
    assert response["count"] == 1
    assert _removed_ids(hass) == {"stat"}


@pytest.mark.asyncio
async def test_register_is_idempotent(hass) -> None:
    services.async_register(hass)
    assert hass.services.has_service(DOMAIN, services.SERVICE_DELETE_DEVICES)
