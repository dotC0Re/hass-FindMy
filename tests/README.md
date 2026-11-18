# Tests

```powershell
uv sync --all-groups
uv run pytest
```

Kein Test spricht mit Apple. Konten sind Attrappen (`mock_account_factory`), Geräte und
Schlüssel entstehen aus Zufallsschlüsseln (`accessory_factory`, `keypair_factory`). So laufen
Schlüsselableitung und Abgleich echt, ohne Netz.

## Warum kein `pytest-homeassistant-custom-component`

Geprüft wurde Version 0.13.366, die genau Home Assistant 2026.9.3 festlegt. Unter Linux wäre
sie die erste Wahl. Unter Windows lässt sich das Plugin aber nicht einmal laden:
`homeassistant/runner.py` importiert `fcntl`, das es dort nicht gibt. Weil pytest Plugins beim
Start automatisch lädt, wäre damit jeder Testlauf unter Windows kaputt.

Deshalb gilt ein Weg für Windows und Linux: `pytest` + `pytest-asyncio` (`asyncio_mode =
"auto"`) und ein eigenes `hass`-Fixture in `conftest.py`. Es baut ein `HomeAssistant` im
temporären Verzeichnis mit Loader, Frame-Helfer und Config-Entries, richtet aber keine
Integration ein. Was das Plugin sonst mitbrächte (z. B. `MockConfigEntry`), legen Tests bei
Bedarf selbst an.

## Abhängigkeiten

- `homeassistant==2026.9.3` steht nur in der Dev-Gruppe. Zur Laufzeit bringt Home Assistant
  sich selbst mit. Die Mindestversion in `hacs.json` ist genau diese Version.
- Home Assistant erzwingt zur Laufzeit die Versionen aus `package_constraints.txt`, nennt sie
  aber nicht in seinen Paket-Metadaten. Sie stehen deshalb als
  `[tool.uv] constraint-dependencies` in `pyproject.toml`. Nach einem Update von
  `homeassistant`:

  ```powershell
  uv run python .github/scripts/check_pins.py --write
  uv lock
  ```

  CI prüft mit demselben Skript (ohne `--write`), dass Vorgaben, `findmy`-Version im Manifest
  und Mindestversion in `hacs.json` zusammenpassen.
- `aiousbwatcher`, `serialx` und `aiohasupervisor` braucht die Komponente `bluetooth` über
  `usb` und `hassio`. Home Assistant installiert sie erst zur Laufzeit. Mit ihnen lassen sich
  auch `device_tracker.py`, `presence.py`, `sensor.py` und `binary_sensor.py` unter Windows
  importieren.

## Marker `requires_bluetooth`

Für Tests, die `homeassistant.components.bluetooth` importieren. Unter Windows werden sie
übersprungen, falls die Komponente nicht importierbar ist (unvollständige lokale Umgebung).
Auf anderen Systemen schlagen sie dann fehl, damit CI keine Tests stillschweigend verliert.

## Ruff und Pyright

Für `tests/**` gelten in `pyproject.toml` gezielte Ausnahmen (fehlende Annotationen an
Fixtures, private Helfer, Mocks als `Any`, Testwerte). Testdateien brauchen deshalb keine
`# ruff: noqa`- oder `# pyright:`-Kopfzeilen.
