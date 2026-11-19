# Tests

```powershell
uv sync --all-groups
uv run pytest
```

No test talks to Apple. Accounts are mocks (`mock_account_factory`); accessories and key
pairs are built from random keys (`accessory_factory`, `keypair_factory`). Key derivation and
matching therefore run for real, without network access.

Stored accounts live on the FindMy worker thread. `open_account(storage, entry_id)` from
`conftest.py` opens a mocked library account there the way a real one is opened and returns
the `AccountRuntime` Home Assistant's side works with; `library_mock(runtime)` gives the mock
behind it. The `hass` fixture stops the worker when the test ends.

## Why not `pytest-homeassistant-custom-component`

Version 0.13.366, which pins exactly Home Assistant 2026.9.3, was evaluated. On Linux it would
be the first choice. On Windows, however, the plugin cannot even be loaded:
`homeassistant/runner.py` imports `fcntl`, which does not exist there. Because pytest loads
plugins automatically at startup, every test run on Windows would break.

So there is one setup for Windows and Linux: `pytest` + `pytest-asyncio` (`asyncio_mode =
"auto"`) and an own `hass` fixture in `conftest.py`. It creates a `HomeAssistant` instance in a
temporary directory with the loader, the frame helper and config entries, but sets up no
integration. Anything the plugin would otherwise provide (e.g. `MockConfigEntry`) tests create
themselves when needed.

## Dependencies

- `homeassistant==2026.9.3` is only in the dev group. At runtime Home Assistant brings itself.
  The minimum version in `hacs.json` is exactly this version.
- At runtime Home Assistant enforces the versions from `package_constraints.txt`, but does not
  list them in its package metadata. They are therefore mirrored as
  `[tool.uv] constraint-dependencies` in `pyproject.toml`. After updating `homeassistant`:

  ```powershell
  uv run python .github/scripts/check_pins.py --write
  uv lock
  ```

  CI runs the same script (without `--write`) to check that the constraints, the `findmy`
  version in the manifest and the minimum version in `hacs.json` agree.
- `aiousbwatcher`, `serialx` and `aiohasupervisor` are needed by the `bluetooth` component via
  `usb` and `hassio`. Home Assistant installs them only at runtime. With them,
  `device_tracker.py`, `presence.py`, `sensor.py` and `binary_sensor.py` import on Windows too.

## Marker `requires_bluetooth`

For tests that import `homeassistant.components.bluetooth`. On Windows they are skipped if the
component cannot be imported (incomplete local environment). On other platforms they fail
instead, so CI never silently loses tests.

## Ruff and Pyright

`pyproject.toml` defines targeted exceptions for `tests/**` (missing annotations on fixtures,
private helpers, mocks typed as `Any`, literal test values). Test files therefore need no
`# ruff: noqa` or `# pyright:` header lines.
