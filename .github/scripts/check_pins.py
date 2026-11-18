"""Keep version pins that live in several files consistent.

Checks, and with ``--write`` repairs where it can:

* ``[tool.uv] constraint-dependencies`` in ``pyproject.toml`` equals the
  ``package_constraints.txt`` shipped with the installed Home Assistant. Home Assistant
  enforces these pins at runtime but does not publish them as package metadata, so uv
  cannot see them on its own.
* The ``findmy`` pin in ``pyproject.toml`` equals the requirement in ``manifest.json``,
  so development and users run the same library version.
* The ``homeassistant`` pin in the dev group equals the minimum version in ``hacs.json``,
  so the advertised minimum is the version the tests actually run against.

Only the constraint block is rewritten; the other two mismatches need a deliberate decision
and are reported only. Run from the repository root.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
MANIFEST = ROOT / "custom_components" / "findmy" / "manifest.json"
HACS = ROOT / "hacs.json"

BEGIN = "    # BEGIN generated from homeassistant/package_constraints.txt\n"
END = "    # END generated from homeassistant/package_constraints.txt\n"

_EXACT_PIN = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*==\s*([^\s;,]+)\s*$")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _exact_pin(requirements: list[str], package: str) -> str | None:
    for requirement in requirements:
        match = _EXACT_PIN.match(requirement)
        if match and _normalize(match.group(1)) == _normalize(package):
            return match.group(2)
    return None


def ha_constraints() -> list[str]:
    spec = importlib.util.find_spec("homeassistant")
    if spec is None or spec.origin is None:
        msg = "homeassistant is not installed; run `uv sync --all-groups` first"
        raise SystemExit(msg)
    path = Path(spec.origin).parent / "package_constraints.txt"
    lines = (line.split("#", 1)[0].strip() for line in path.read_text("utf-8").splitlines())
    return [line for line in lines if line]


def render_block(constraints: list[str]) -> str:
    return BEGIN + "".join(f'    "{line}",\n' for line in constraints) + END


def check_constraints(text: str, *, write: bool) -> list[str]:
    start, end = text.find(BEGIN), text.find(END)
    if start < 0 or end < start:
        return ["pyproject.toml: generated constraint markers are missing"]

    expected = render_block(ha_constraints())
    if text[start : end + len(END)] == expected:
        return []
    if write:
        PYPROJECT.write_text(text[:start] + expected + text[end + len(END) :], "utf-8")
        print("pyproject.toml: constraint block regenerated; run `uv lock` next")
        return []
    return [
        (
            "pyproject.toml: constraint-dependencies differ from Home Assistant's "
            "package_constraints.txt; run `uv run python .github/scripts/check_pins.py --write`"
        ),
    ]


def check_findmy(pyproject: dict) -> list[str]:
    manifest_pin = _exact_pin(json.loads(MANIFEST.read_text("utf-8"))["requirements"], "findmy")
    project_pin = _exact_pin(pyproject["project"]["dependencies"], "findmy")
    if manifest_pin is None or manifest_pin != project_pin:
        return [f"findmy pin differs: manifest.json={manifest_pin}, pyproject.toml={project_pin}"]
    return []


def check_homeassistant(pyproject: dict) -> list[str]:
    dev_pin = _exact_pin(pyproject["dependency-groups"]["dev"], "homeassistant")
    hacs_min = json.loads(HACS.read_text("utf-8")).get("homeassistant")
    if dev_pin is None or dev_pin != hacs_min:
        return [f"homeassistant differs: dev group pin={dev_pin}, hacs.json minimum={hacs_min}"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="regenerate the constraint block")
    args = parser.parse_args()

    text = PYPROJECT.read_text("utf-8")
    pyproject = tomllib.loads(text)
    errors = [
        *check_constraints(text, write=args.write),
        *check_findmy(pyproject),
        *check_homeassistant(pyproject),
    ]
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
