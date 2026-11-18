"""Tests for the automation blueprints (R19).

What is covered: both files load with Home Assistant's YAML loader, pass the
schema Home Assistant applies to automation blueprints, accept inputs, and the
generated automation config passes Home Assistant's automation schema.  For
`tag_stale` the template trigger is additionally attached to a real state
machine to check that it fires exactly once per stale period.

What is NOT covered: running the generated automation inside a full Home
Assistant instance (the minutely re-evaluation of `now()` is read from the
Home Assistant source, not exercised here), and delivery of notifications.
"""

# ruff: noqa: ANN001, ANN201, ANN202, ANN401, ARG001, INP001, PLR2004
# pyright: basic, reportAttributeAccessIssue=false, reportReturnType=false, reportArgumentType=false

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from homeassistant.components.automation import config as automation_config
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.components.template import trigger as template_trigger
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.util.yaml import load_yaml

if TYPE_CHECKING:
    from collections.abc import Iterator

BLUEPRINT_DIR = Path(__file__).parent.parent / "blueprints" / "automation"
TAG = "device_tracker.tag"
INPUTS: dict[str, dict[str, Any]] = {
    "tag_stale": {"tag_entity": TAG, "stale_hours": 2, "notify_service": ""},
    "tag_zone_change": {
        "tag_entity": TAG,
        "zone_entity": "zone.home",
        "on_enter": True,
        "on_leave": False,
        "notify_service": "notify.mobile_app_phone",
    },
}
# Keys of the pre-2024.10 syntax that must not come back.
OLD_KEYS = {"platform", "service"}
OLD_TOP_LEVEL = {"trigger", "condition", "action"}


def _load(name: str) -> dict[str, Any]:
    return load_yaml(str(BLUEPRINT_DIR / f"{name}.yaml"))


def _blueprint(name: str) -> Blueprint:
    return Blueprint(
        _load(name),
        expected_domain="automation",
        schema=automation_config.AUTOMATION_BLUEPRINT_SCHEMA,
    )


def _substitute(name: str, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    config = {"use_blueprint": {"path": f"findmy/{name}.yaml", "input": inputs or INPUTS[name]}}
    blueprint_inputs = BlueprintInputs(_blueprint(name), config)
    blueprint_inputs.validate()
    return blueprint_inputs.async_substitute()


def _walk_keys(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item)


@pytest_asyncio.fixture
async def hass(tmp_path: Path):
    hass = HomeAssistant(str(tmp_path))
    yield hass
    await hass.async_stop(force=True)


@pytest.mark.parametrize("name", list(INPUTS))
def test_blueprint_loads_and_validates(name) -> None:
    blueprint = _blueprint(name)
    assert blueprint.domain == "automation"
    assert blueprint.metadata["source_url"].startswith(
        "https://github.com/dotC0Re/hass-FindMy/blob/main/blueprints/automation/"
    )
    assert blueprint.metadata["homeassistant"]["min_version"]
    assert blueprint.validate() is None


@pytest.mark.parametrize("name", list(INPUTS))
def test_uses_current_syntax(name) -> None:
    data = _load(name)
    assert not OLD_TOP_LEVEL & data.keys()
    assert {"triggers", "actions"} <= data.keys()
    body = {k: v for k, v in data.items() if k != "blueprint"}
    assert not OLD_KEYS & set(_walk_keys(body))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(INPUTS))
async def test_generated_automation_is_valid(hass, name) -> None:
    # Template validation needs a Home Assistant instance in the running loop.
    substituted = _substitute(name)
    validated = automation_config.PLATFORM_SCHEMA(substituted)
    assert validated["triggers"]
    assert validated["actions"]


@pytest.mark.parametrize("name", list(INPUTS))
def test_every_input_is_used(name) -> None:
    # An input nobody reads is a silent UI knob that does nothing.
    blueprint = _blueprint(name)
    text = (BLUEPRINT_DIR / f"{name}.yaml").read_text(encoding="utf-8")
    for input_name in blueprint.inputs:
        assert f"!input {input_name}" in text, input_name


def test_stale_has_no_polling_interval() -> None:
    # R19: a number fed to time_pattern only matches that minute of the hour.
    data = _load("tag_stale")
    assert "check_interval_min" not in data["blueprint"]["input"]
    assert all(t["trigger"] == "template" for t in data["triggers"])
    assert "now()" in data["triggers"][0]["value_template"]
    # trigger templates only see `trigger_variables`.
    assert set(data["trigger_variables"]) >= {"tag_entity_var", "stale_hours_var"}


def test_stale_has_no_dead_condition() -> None:
    data = _load("tag_stale")
    assert "conditions" not in data
    assert "trigger.event_data" not in (BLUEPRINT_DIR / "tag_stale.yaml").read_text("utf-8")


def test_zone_blueprint_substitution_uses_inputs() -> None:
    substituted = _substitute("tag_zone_change")
    assert [t["event"] for t in substituted["triggers"]] == ["enter", "leave"]
    assert all(t["entity_id"] == TAG and t["zone"] == "zone.home" for t in substituted["triggers"])
    assert substituted["variables"]["on_leave_var"] is False


# --- tag_stale trigger behaviour against a real state machine ---------------------------


async def _attach(hass, fired: list[Any]):
    substituted = _substitute("tag_stale")
    trigger_config = cv.TRIGGER_SCHEMA(substituted["triggers"])[0]
    trigger_config = template_trigger.TRIGGER_SCHEMA(trigger_config)
    variables = dict(substituted["trigger_variables"])  # literals, no rendering needed

    def action(run_variables, context=None) -> None:
        fired.append(run_variables)

    return await template_trigger.async_attach_trigger(
        hass,
        trigger_config,
        action,
        {"name": "test", "trigger_data": {"id": "stale", "idx": "0"}, "variables": variables},
    )


async def _set(hass, detected: Any, **extra: Any) -> None:
    hass.states.async_set(TAG, "not_home", {"detected_at": detected, **extra})
    await hass.async_block_till_done()


def _ago(hours: float) -> datetime:
    return datetime.now(tz=UTC) - timedelta(hours=hours)


@pytest.mark.asyncio
async def test_stale_fires_once_per_stale_period(hass) -> None:
    fired: list[Any] = []
    await _set(hass, _ago(0.1))
    unsub = await _attach(hass, fired)

    await _set(hass, _ago(1))  # fresher than the 2 h threshold
    assert fired == []

    await _set(hass, _ago(3))  # crosses the threshold
    assert len(fired) == 1

    await _set(hass, _ago(3), status=1)  # still stale: no repeat
    await _set(hass, _ago(4))
    assert len(fired) == 1

    await _set(hass, _ago(0.1))  # reports again ...
    assert len(fired) == 1
    await _set(hass, _ago(5))  # ... and goes stale again
    assert len(fired) == 2

    unsub()


@pytest.mark.asyncio
async def test_stale_at_start_does_not_fire_until_rearmed(hass) -> None:
    fired: list[Any] = []
    await _set(hass, _ago(10))
    unsub = await _attach(hass, fired)

    await _set(hass, _ago(11), status=1)
    assert fired == []

    await _set(hass, _ago(0.1))
    await _set(hass, _ago(3))
    assert len(fired) == 1
    unsub()


@pytest.mark.asyncio
@pytest.mark.parametrize("detected", [None, "not a date"])
async def test_missing_or_garbage_detected_at_never_fires(hass, detected) -> None:
    fired: list[Any] = []
    await _set(hass, _ago(0.1))
    unsub = await _attach(hass, fired)

    await _set(hass, detected)
    assert fired == []
    unsub()


@pytest.mark.asyncio
async def test_iso_string_detected_at_is_understood(hass) -> None:
    fired: list[Any] = []
    await _set(hass, _ago(0.1).isoformat())
    unsub = await _attach(hass, fired)

    await _set(hass, _ago(3).isoformat())
    assert len(fired) == 1
    unsub()


@pytest.mark.asyncio
async def test_unavailable_tag_without_attributes_does_not_fire(hass) -> None:
    fired: list[Any] = []
    await _set(hass, _ago(0.1))
    unsub = await _attach(hass, fired)

    hass.states.async_set(TAG, "unavailable")
    await hass.async_block_till_done()
    assert fired == []
    unsub()


@pytest.mark.asyncio
async def test_stale_message_renders(hass) -> None:
    substituted = _substitute("tag_stale")
    hass.states.async_set(TAG, "home", {"detected_at": _ago(3.5), "friendly_name": "Backpack"})
    action_variables = cv.SCRIPT_VARIABLES_SCHEMA(substituted["actions"][0]["variables"])
    rendered = action_variables.async_render(hass, dict(substituted["variables"]))
    assert rendered["tag_name"] == "Backpack"
    assert rendered["age_h"] == pytest.approx(3.5, abs=0.1)
    assert "Backpack hasn't reported for" in rendered["message"]
    assert "threshold: 2 h" in rendered["message"]
