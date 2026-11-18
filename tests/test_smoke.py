"""Import every module of the integration and check the shared test fixtures work.

Modules are discovered from the package directory, so new modules are covered without
touching this file. Modules that import Home Assistant's Bluetooth component are marked
``requires_bluetooth``; conftest skips them where that component cannot be imported.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from findmy import KeyPairType, LoginState
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from custom_components.findmy.const import DOMAIN

from .conftest import AccessoryFactory, AccountFactory, KeyPairFactory

PACKAGE = "custom_components.findmy"
PACKAGE_DIR = Path(__file__).parents[1] / "custom_components" / "findmy"
# Modules that import homeassistant.components.bluetooth, directly or through presence.py.
BLUETOOTH_MODULES = {"binary_sensor", "device_tracker", "presence", "sensor"}


def _module_params() -> list[object]:
    params: list[object] = []
    for path in sorted(PACKAGE_DIR.glob("*.py")):
        name = path.stem
        module = PACKAGE if name == "__init__" else f"{PACKAGE}.{name}"
        marks = [pytest.mark.requires_bluetooth] if name in BLUETOOTH_MODULES else []
        params.append(pytest.param(module, id=name, marks=marks))
    return params


@pytest.mark.parametrize("module", _module_params())
def test_module_imports(module: str) -> None:
    importlib.import_module(module)


def test_bluetooth_module_list_is_current() -> None:
    present = {path.stem for path in PACKAGE_DIR.glob("*.py")}
    assert present >= BLUETOOTH_MODULES


def test_manifest_matches_domain() -> None:
    manifest = json.loads((PACKAGE_DIR / "manifest.json").read_text("utf-8"))

    assert manifest["domain"] == DOMAIN
    # hassfest requires domain and name first, the remaining keys sorted.
    keys = list(manifest)
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])


async def test_hass_fixture_loads_custom_integration(hass: HomeAssistant) -> None:
    integration = await async_get_integration(hass, DOMAIN)

    assert integration.domain == DOMAIN
    assert not integration.is_built_in
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_mock_account_is_logged_in_and_returns_nothing(
    mock_account_factory: AccountFactory,
) -> None:
    account = mock_account_factory(email="someone@example.com")

    assert account.login_state is LoginState.LOGGED_IN
    assert account.account_name == "someone@example.com"
    assert await account.fetch_location([]) is None
    with pytest.raises(AttributeError):
        _ = account.not_an_account_attribute


def test_accessory_factory_creates_distinct_accessories(
    accessory_factory: AccessoryFactory,
) -> None:
    first, second = accessory_factory(), accessory_factory()

    assert first.identifier != second.identifier
    assert first.keys_at(0) != second.keys_at(0)


def test_keypair_factory_sets_type(keypair_factory: KeyPairFactory) -> None:
    key = keypair_factory(key_type=KeyPairType.PRIMARY)

    assert key.key_type is KeyPairType.PRIMARY
    assert len(key.adv_key_bytes) == 28
