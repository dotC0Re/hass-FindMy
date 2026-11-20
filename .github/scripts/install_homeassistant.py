"""Replace the locked Home Assistant in the current environment with another release.

Used by CI to run the tests against a newer Home Assistant without touching ``uv.lock``.
Home Assistant pins many libraries in ``package_constraints.txt``, which ships inside the
package but is not part of its metadata, and the components ``bluetooth`` depends on
(``bluetooth``, ``hassio``, ``usb``) install their requirements only at runtime. So this
installs the release without dependencies first, reads both from the installed files, and
then installs the release again with its own constraints and those requirements. The
``[tool.uv] constraint-dependencies`` of ``pyproject.toml`` belong to the locked release;
``uv pip`` would apply them too, so it runs with ``--no-config``.

Run inside the synced project environment, from the repository root:

    uv run --no-sync python .github/scripts/install_homeassistant.py 2026.11.1

Afterwards run the tests with ``uv run --no-sync pytest``; a plain ``uv run`` would sync
the environment back to the lock.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The components whose requirements the tests need installed up front (see the dev group).
COMPONENTS = ("bluetooth", "hassio", "usb")


def _uv_pip(*args: str) -> None:
    # `uv run` exports its own binary as UV. Prefer it over the `uv` on PATH, which inside
    # the environment is Home Assistant's uv dependency that this very install replaces.
    uv = os.environ.get("UV") or shutil.which("uv")
    if uv is None:
        msg = "uv is not on PATH"
        raise SystemExit(msg)
    command = [uv, "pip", "install", "--no-config", "--python", sys.executable, *args]
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)  # noqa: S603


def _package_dir() -> Path:
    # find_spec only locates the package; importing it here could load the old release.
    spec = importlib.util.find_spec("homeassistant")
    if spec is None or spec.origin is None:
        msg = "homeassistant is not installed"
        raise SystemExit(msg)
    return Path(spec.origin).parent


def _requirements(package: Path) -> list[str]:
    requirements: list[str] = []
    for component in COMPONENTS:
        manifest = package / "components" / component / "manifest.json"
        requirements.extend(json.loads(manifest.read_text("utf-8")).get("requirements", []))
    return requirements


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="exact Home Assistant version, e.g. 2026.11.1")
    version = parser.parse_args().version
    pin = f"homeassistant=={version}"

    _uv_pip("--no-deps", pin)
    package = _package_dir()
    requirements = _requirements(package)
    print("component requirements:", ", ".join(requirements), flush=True)

    with tempfile.TemporaryDirectory() as tmp:
        # Copied because the second install may rewrite the package directory it is read from.
        constraints = Path(tmp) / "package_constraints.txt"
        shutil.copyfile(package / "package_constraints.txt", constraints)
        _uv_pip("--constraint", str(constraints), pin, *requirements)


if __name__ == "__main__":
    main()
