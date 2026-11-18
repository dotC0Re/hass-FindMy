# Code-Review hass-FindMy

| | |
|---|---|
| **Stand** | 18.11.2026, Commit `a46c8a9` (Fork `dotC0Re/hass-FindMy`, identisch mit `malmeloo/hass-FindMy` bis `b2e6785`) |
| **Umfang** | Gesamte Integration `custom_components/findmy/` (≈ 3.300 Zeilen), Blueprints, Projekt- und CI-Konfiguration, relevante Teile der Bibliotheken `FindMy` 0.10.2 und `anisette` |
| **Ziel** | Grundlage für ein Refactoring mit Fokus auf **Sicherheit** und **Stabilität**, bei guter Wartbarkeit, als Basis für den weiteren Ausbau |
| **Methode** | Vollständiges manuelles Lesen aller Module; Abgleich jeder Annahme gegen den Quellcode von Home Assistant 2026.9.3 und FindMy 0.10.2 in `.venv`; einzelne Verhaltensannahmen per Skript geprüft |
| **Nachprüfung** | Alle Befunde wurden ein zweites Mal geprüft, viele per Test. Ergebnis, drei neue Befunde (R20–R22) und die Strategie zur Behebung stehen in [REFACTORING_STRATEGY.md](REFACTORING_STRATEGY.md). Korrekturen sind hier eingearbeitet und mit „Nachprüfung:“ markiert. |

## Inhalt

1. [Zusammenfassung](#1-zusammenfassung)
2. [Architekturüberblick](#2-architekturüberblick)
3. [Befunde: Sicherheit und Datenschutz](#3-befunde-sicherheit-und-datenschutz)
4. [Befunde: Stabilität und Korrektheit](#4-befunde-stabilität-und-korrektheit)
5. [Befunde: Wartbarkeit und Dokumentation](#5-befunde-wartbarkeit-und-dokumentation)
6. [Abhängigkeiten: Bestand und Reduktion](#6-abhängigkeiten-bestand-und-reduktion)
7. [Entwicklungsumgebung, Tests und CI](#7-entwicklungsumgebung-tests-und-ci)
8. [Refactoring-Plan](#8-refactoring-plan)
9. [Ausbau-Perspektiven](#9-ausbau-perspektiven)
10. [Anhang: Geprüfte Fakten zu Home Assistant und FindMy.py](#10-anhang-geprüfte-fakten-zu-home-assistant-und-findmypy)

**Schweregrade:** 🔴 Hoch = Sicherheitslücke, Datenverlust, Sperrrisiko beim Apple-Konto oder stiller Funktionsausfall · 🟠 Mittel = Fehlverhalten in realistischen Situationen · 🟡 Niedrig = Randfall, Schönheitsfehler, technische Schuld

**Status:** ✅ verifiziert (im Quellcode oder per Test nachgewiesen) · 🔍 plausibel (Ablauf schlüssig, aber nicht live reproduziert) · 🌐 extern berichtet (aus den Original-Repos, nicht selbst geprüft)

---

## 1. Zusammenfassung

Die Integration ist funktional reichhaltig: Abruf über das Find-My-Netzwerk, lokale Bluetooth-Erkennung inklusive DULT-Format der zweiten AirTag-Generation, Präsenz- und Batteriesensoren. Der Code ist typisiert, wird mit Ruff (`select = ["ALL"]`) und basedpyright geprüft, und die neueren Module (`local_bluetooth.py`, `presence.py`) sind gut kommentiert.

Die Schwächen liegen vor allem in vier Bereichen:

1. **Geheimnisse:** Das Apple-ID-**Passwort** liegt im Klartext in `core.config_entries` und damit in jedem Backup (S1). Private Schlüssel von Tags landen im Debug-Log (S2). Ein Dienst, den jeder Benutzer aufrufen kann, löscht ohne Parameter **alle** Geräte (S3).
2. **Sitzungsablauf:** Wenn Apple eine Sitzung beendet, startet die Re-Authentifizierung **nie** (R1). Fehlschläge werden als Erfolg protokolliert (R2). Erneuerte Tokens gehen beim Neustart verloren (R5).
3. **Rate-Limit:** Die zugesagte Grenze von einer Abfrage pro 15 Minuten und Konto wird nicht erzwungen. Nach dem Entfernen eines Kontos wird sie sogar dauerhaft überschritten (R3, R4). Damit steigt das Risiko einer Sperre durch Apple.
4. **Struktur:** Ein globaler, prozessweiter Zustand (`hass.data[DOMAIN]`) wird nie abgebaut. Ein einzelner Coordinator für alle Konten verhindert eine saubere Fehlerzuordnung pro Konto. Es gibt keine Tests.

Die meisten Befunde lassen sich mit überschaubarem Aufwand beheben. Kapitel 8 schlägt eine Reihenfolge vor, die zuerst die risikoreichsten Punkte schließt und gleichzeitig eine Testbasis aufbaut. Die ausgearbeitete Fassung mit Arbeitspaketen steht in [REFACTORING_STRATEGY.md](REFACTORING_STRATEGY.md).

Die Nachprüfung hat drei weitere Befunde ergeben, die in der Strategie beschrieben sind: Der Abruf **blockiert die Event-Loop** von Home Assistant für Sekunden bis über eine Minute (R20), ein Abruf besteht aus **vielen HTTP-Anfragen** (R21), und Apple hat im Oktober 2026 die Anmeldung umgestellt, sodass **neue Anmeldungen voraussichtlich fehlschlagen** (R22).

### Befundübersicht

| ID | Titel | Grad | Status |
|---|---|---|---|
| [S1](#s1-apple-id-passwort-im-klartext-gespeichert) | Apple-ID-Passwort im Klartext gespeichert | 🔴 | ✅ |
| [S2](#s2-private-schlüssel-und-2fa-codes-im-log) | Private Schlüssel und 2FA-Codes im Log | 🔴 | ✅ |
| [S3](#s3-löschdienst-ohne-admin-prüfung-mit-gefährlichem-default) | Löschdienst ohne Admin-Prüfung, Default „alles löschen“ | 🔴 | ✅ |
| [S4](#s4-lokale-anisette-lädt-ungeprüfte-binärdateien-nach) | Lokale Anisette lädt ungeprüfte Binärdateien nach | 🟠 | ✅ |
| [S5](#s5-bluetooth-replay-kann-präsenz-vortäuschen-und-die-schlüsselausrichtung-verschieben) | Bluetooth-Replay täuscht Präsenz vor (🟠) und kann die Ortung verzögern (🟡) | 🟠 | ✅ |
| [S6](#s6-positionsverlauf-in-unbegrenzten-langzeitstatistiken) | Positionsverlauf in unbegrenzten Langzeitstatistiken | 🟠 | ✅ |
| [S7](#s7-personenbezogene-daten-in-logs-und-ids) | Personenbezogene Daten in Logs und IDs | 🟡 | ✅ |
| [R1](#r1-re-authentifizierung-wird-nie-gestartet) | Re-Authentifizierung wird nie gestartet; ein abgelaufenes Konto stoppt alle Abfragen | 🔴 | ✅ |
| [R2](#r2-fehlgeschlagene-abrufe-werden-als-erfolg-protokolliert) | Fehlgeschlagene Abrufe werden als Erfolg protokolliert | 🔴 | ✅ |
| [R3](#r3-abfrageintervall-wird-nach-dem-entfernen-eines-kontos-nicht-angepasst) | Abfrageintervall nach Entfernen eines Kontos nicht angepasst | 🔴 | ✅ |
| [R4](#r4-rate-limit-wird-nicht-erzwungen) | Rate-Limit wird nicht erzwungen | 🟠 | ✅ |
| [R5](#r5-erneuerte-sitzungsdaten-werden-nicht-gespeichert) | Erneuerte Sitzungsdaten werden nicht gespeichert | 🟠 | ✅ |
| [R6](#r6-config-flow-unbehandelte-ausnahmen-und-falsche-step-id) | Config-Flow: unbehandelte Ausnahmen, falsche Step-ID | 🟠 | ✅ |
| [R7](#r7-re-authentifizierung-akzeptiert-ein-anderes-konto) | Re-Authentifizierung akzeptiert ein anderes Konto | 🟠 | ✅ |
| [R8](#r8-geräte-können-doppelt-angelegt-werden) | Geräte können doppelt angelegt werden | 🟠 | ✅ |
| [R9](#r9-kontogeräte-kollidieren-bei-gleichem-lokalen-teil-der-e-mail) | Kontogeräte kollidieren bei gleichem lokalen Teil der E-Mail | 🟠 | ✅ |
| [R10](#r10-defekte-eintragsdaten-und-abgemeldete-konten-beim-start) | Defekte Eintragsdaten und abgemeldete Konten beim Start | 🟠 | ✅ |
| [R11](#r11-bluetooth-ist-harte-abhängigkeit-obwohl-optional) | Bluetooth ist harte Abhängigkeit, obwohl optional | 🟡 | ✅ |
| [R12](#r12-falsche-mindestversion-in-hacsjson) | Falsche Mindestversion in `hacs.json` | 🟠 | ✅ |
| [R13](#r13-auf--und-abbau-unvollständig) | Auf- und Abbau unvollständig | 🟡 | ✅ |
| [R14](#r14-http-sessions-im-config-flow-werden-nie-geschlossen) | HTTP-Sessions im Config-Flow werden nie geschlossen | 🟡 | ✅ |
| [R15](#r15-filter-rolling-im-löschdienst-ist-wirkungslos) | Filter `rolling` im Löschdienst ist wirkungslos | 🟡 | ✅ |
| [R16](#r16-sensor-nächster-abruf-falsch-berechnet) | Sensor „Nächster Abruf“ falsch berechnet | 🟡 | ✅ |
| [R17](#r17-abrufprotokoll-wächst-unbegrenzt) | Abrufprotokoll wächst unbegrenzt | 🟡 | ✅ |
| [R18](#r18-config-entry-wird-bei-jedem-abruf-neu-geschrieben) | Config-Entry wird bei jedem Abruf neu geschrieben | 🟡 | ✅ |
| [R19](#r19-blueprint-tag_stale-prüft-nur-einmal-pro-stunde) | Blueprint `tag_stale` prüft nur einmal pro Stunde | 🟡 | ✅ |
| [R20](REFACTORING_STRATEGY.md#r20-der-abruf-blockiert-die-event-loop-von-home-assistant) | Der Abruf blockiert die Event-Loop von Home Assistant (neu) | 🔴 | ✅ |
| [R21](REFACTORING_STRATEGY.md#r21-ein-abruf-sind-viele-http-anfragen) | Ein Abruf sind viele HTTP-Anfragen (neu) | 🟠 | ✅ |
| [R22](REFACTORING_STRATEGY.md#r22-apple-umstellung-im-oktober-2026-anmeldung-und-abruf-gestört) | Apple-Umstellung im Oktober 2026: Anmeldung und Abruf gestört (neu) | 🔴 | 🌐 |
| [W1–W10](#5-befunde-wartbarkeit-und-dokumentation) | Wartbarkeit und Dokumentation | 🟡 | ✅ |

---

## 2. Architekturüberblick

### Module

| Modul | Aufgabe |
|---|---|
| [`__init__.py`](../custom_components/findmy/__init__.py) | Setup, Unload und Migration der Config-Entries; verteilt auf die Plattformen |
| [`storage.py`](../custom_components/findmy/storage.py) | `RuntimeStorage`: globales Objekt in `hass.data[DOMAIN]` mit allen Konten und Geräten, Bluetooth-Zwischenständen und dem Coordinator |
| [`coordinator.py`](../custom_components/findmy/coordinator.py) | **Ein** `DataUpdateCoordinator` für alle Konten; wählt Konten reihum (Round-Robin) und führt ein Abrufprotokoll im Speicher |
| [`config_flow.py`](../custom_components/findmy/config_flow.py) | Anmeldung am Konto inklusive 2FA, Re-Authentifizierung, Import von Geräten (Schlüssel, `.json`, `.plist`), Options-Flow für die Abwesenheitszeit |
| [`device_tracker.py`](../custom_components/findmy/device_tracker.py) | Tracker-Entity; **zusätzlich** der gesamte lokale Bluetooth-Abgleich für Accessoires mit Rolling Keys |
| [`local_bluetooth.py`](../custom_components/findmy/local_bluetooth.py) | Reine Funktionen: Kandidatenschlüssel erzeugen, Offline-Finding- und DULT-Advertisements parsen und abgleichen |
| [`presence.py`](../custom_components/findmy/presence.py) | Präsenzsensor und Signalstärke, gespeist über Dispatcher-Signale vom Tracker |
| [`sensor.py`](../custom_components/findmy/sensor.py), [`binary_sensor.py`](../custom_components/findmy/binary_sensor.py) | Position, Batterie und Statuszähler pro Gerät; Abrufstatus pro Konto |
| [`_entity.py`](../custom_components/findmy/_entity.py) | Gemeinsame Helfer: IDs, Device-Info, Dekodierung der Batterie-Bits |
| [`services.py`](../custom_components/findmy/services.py) | Dienst `findmy.delete_devices` |

### Datenfluss

```
                      ┌──────────────── hass.data["findmy"] = RuntimeStorage (global) ────────────────┐
Config-Entries ──────►│ _entries: {entry_id: AsyncAppleAccount | KeyPair | FindMyAccessory | Fixed…}  │
 (Konto / Gerät)      │ local_observations / local_status / local_rssi  (nie bereinigt)              │
                      │ coordinator: FindMyCoordinator (ein einziger, ohne config_entry)             │
                      └───────────────┬──────────────────────────────────────────────┬───────────────┘
                                      │ alle 15 min / Anzahl Konten,                  │
                                      │ Konto reihum                                 │
                                      ▼                                              │
                      account.fetch_location(alle Geräte) ──► Apple                  │
                                      │                                              │
                                      ▼                                              │
     device_tracker / sensor / binary_sensor (je Gerät, über Coordinator-Kontext)    │
                                                                                     │
HA Bluetooth ──► FindMyDeviceTracker._async_track_service_info ──► local_bluetooth.match_*()
                      │  (pro Accessoire 3 Callbacks)                                │
                      └─ Dispatcher-Signal ──► Präsenz-, Signal-, Batteriesensoren ◄─┘
```

### Stärken, die erhalten bleiben sollten

- **Reine Funktionen in `local_bluetooth.py`:** gut testbar, und das Lookup über das Präfix `O(1)` ist effizient.
- **Schlüsselerzeugung im Executor** auf einem Snapshot (`build_candidate_key_lookup_from_json`), damit die Generatoren im Live-Objekt nicht verändert werden.
- **Schreibbremsen:** Recorder und Config-Entries werden gezielt gedrosselt (`_ATTRIBUTE_UPDATE_DELAY`, `_LOCAL_ALIGNMENT_SAVE_DELAY`, `_unrecorded_attributes` für die MAC-Adresse).
- **Strenge statische Analyse** mit Ruff `ALL` und basedpyright.
- **Migration** der Config-Entries ist vorhanden und versioniert.

---

## 3. Befunde: Sicherheit und Datenschutz

### S1 Apple-ID-Passwort im Klartext gespeichert

🔴 Hoch · ✅ verifiziert · [`config_flow.py:409-412`](../custom_components/findmy/config_flow.py#L409-L412), FindMy `reports/account.py:439-455`

**Problem:** `AsyncAppleAccount.to_json()` serialisiert neben den Sitzungstokens auch `account.username` **und `account.password`**. Die Bibliothek setzt das Passwort beim Login (`_gsa_authenticate`, Zeile 801-802), um nach einem HTTP-401 still neu anmelden zu können. Die Integration speichert dieses Mapping unverändert als `entry.data["account_data"]`.

**Auswirkung:** Das Passwort der Apple-ID liegt im Klartext in `/config/.storage/core.config_entries` und in **jedem Home-Assistant-Backup**, auch in Cloud-Backups. Wer Lesezugriff auf das Dateisystem oder ein Backup hat, übernimmt das Apple-Konto. Bei einer Apple-ID betrifft das typischerweise auch iCloud-Daten, Fotos und Gerätesperren.

**Empfehlung:**
1. Das Passwort **nicht** persistieren: Vor dem Speichern `account_data["account"]["password"] = None` setzen. Eine Migration entfernt das Passwort aus bestehenden Einträgen.
2. Folge: Das stille Re-Login nach einem 401 entfällt. Stattdessen startet ein Re-Auth-Flow (siehe R1), und die Sitzung wird gespeichert, sobald sie erneuert wurde (siehe R5). In der Praxis halten die Tokens Wochen bis Monate.
3. Optional: Eine Einstellung „Passwort für automatische Neuanmeldung speichern“, standardmäßig aus, mit klarem Warnhinweis.
4. Unabhängig davon `diagnostics.py` mit `async_redact_data` für `password`, `login.data`, `anisette` und private Schlüssel einführen (siehe Kapitel 9).
5. In der README dokumentieren, welche Geheimnisse wo liegen.

### S2 Private Schlüssel und 2FA-Codes im Log

🔴 Hoch · ✅ verifiziert · [`config_flow.py:551`](../custom_components/findmy/config_flow.py#L551), [`:373`](../custom_components/findmy/config_flow.py#L373), [`:349`](../custom_components/findmy/config_flow.py#L349)

**Problem:** Jeder Flow-Schritt loggt die komplette Benutzereingabe auf Debug-Ebene. Das Passwort wird in `acc_login` und `reauth_confirm` maskiert, andere Geheimnisse aber nicht:
- `dev_static` loggt `private_key` im Klartext. Mit diesem Schlüssel kann jeder die Position des Tags bei Apple abrufen.
- `acc_2fa_submit` loggt den 2FA-Code.

**Auswirkung:** Debug-Logs werden routinemäßig in GitHub-Issues und Foren geteilt. Ein geteilter privater Schlüssel ermöglicht dauerhaftes Tracking, denn statische Schlüssel rotieren nie.

**Empfehlung:** Eine zentrale Funktion `_redact(user_input)` mit einer Liste geheimer Felder (`password`, `private_key`, `code`, `file`) für **alle** Debug-Ausgaben der Flows. Ein Unit-Test stellt sicher, dass kein Feld der Liste je ungefiltert geloggt wird.

### S3 Löschdienst ohne Admin-Prüfung, mit gefährlichem Default

🔴 Hoch · ✅ verifiziert · [`services.py:20-26`](../custom_components/findmy/services.py#L20-L26), [`:75-80`](../custom_components/findmy/services.py#L75-L80)

**Problem:** `findmy.delete_devices` wird mit `hass.services.async_register` registriert, nicht mit `async_register_admin_service`. Damit kann **jeder** Home-Assistant-Benutzer den Dienst aufrufen, auch Nicht-Admins, Automationen, Skripte und Sprachassistenten. Der Default für `filter` ist `all`. Ein Aufruf ohne Parameter entfernt also **alle** Geräte-Einträge unwiderruflich, samt der importierten Schlüsseldateien.

**Auswirkung:** Datenverlust. Ein AirTag-Schlüssel aus einer `.plist` lässt sich nicht ohne Weiteres erneut beschaffen, und die Ausrichtung der Schlüssel geht verloren.

**Empfehlung:**
1. Mit `homeassistant.helpers.service.async_register_admin_service` registrieren.
2. `filter` zum Pflichtfeld machen und keinen Default `all` vorgeben. Zusätzlich ein Pflichtfeld `confirm: true` verlangen.
3. Einträge vor dem Löschen deaktivieren oder exportieren. Alternativ ganz auf den Dienst verzichten, denn Home Assistant kann Einträge bereits über die Oberfläche löschen.

### S4 Lokale Anisette lädt ungeprüfte Binärdateien nach

🟠 Mittel · ✅ verifiziert · [`config_flow.py:264-267`](../custom_components/findmy/config_flow.py#L264-L267), [`storage.py:88-92`](../custom_components/findmy/storage.py#L88-L92), `anisette/anisette.py:26`, `anisette/_util.py:44-50`

**Problem:** Ohne Anisette-URL nutzt die Integration `LocalAnisetteProvider()` **ohne** `libs_path`. Die Bibliothek `anisette` lädt dann bei **jedem Start und für jedes Konto** ein ARM64-Bibliotheksbündel von `https://anisette.dl.mikealmel.ooo/libs` und führt es im CPU-Emulator `unicorn` aus. Dabei gilt:
- Es gibt **keine Integritätsprüfung**, weder Hash noch Signatur. Vertraut wird allein TLS und dem Betreiber der Domain.
- Es gibt **keinen Timeout** (`urllib3.PoolManager().request()` ohne `timeout`). Ein hängender Server blockiert einen Executor-Thread unbegrenzt.
- Der HTTP-Status wird nicht geprüft. Eine Fehlerseite wird als Bibliothek interpretiert.

**Auswirkung:** Lieferkettenrisiko: Ein kompromittierter Download-Server liefert Code, der in Home Assistant geparst (`pyelftools`) und emuliert wird. Dazu kommt ein Stabilitätsrisiko: Ohne Internet oder bei einem Ausfall des Servers startet kein Konto.

**Empfehlung:**
1. `libs_path` auf eine Datei im Konfigurationsverzeichnis setzen, etwa `hass.config.path(".storage/findmy_anisette_libs.bin")`. Sowohl `LocalAnisetteProvider(libs_path=…)` als auch `AsyncAppleAccount.from_json(…, anisette_libs_path=…)` unterstützen das. Dann wird nur einmal geladen.
2. Den SHA-256 der bekannten Bibliotheksversion in der Integration hinterlegen und vor der Nutzung prüfen.
3. Einen Upstream-Fix in `anisette` vorschlagen: Timeout, Statusprüfung, optionaler Hash.
4. In der README die Wahl zwischen lokaler und entfernter Anisette mit ihren Vor- und Nachteilen erklären.

### S5 Bluetooth-Replay kann Präsenz vortäuschen und die Schlüsselausrichtung verschieben

🟠 Mittel (Präsenz) / 🟡 Niedrig (Ausrichtung) · ✅ verifiziert · [`device_tracker.py:298-312`](../custom_components/findmy/device_tracker.py#L298-L312), [`local_bluetooth.py:171-205`](../custom_components/findmy/local_bluetooth.py#L171-L205)

**Nachprüfung:** Punkt 2 war in der ersten Fassung zu stark formuliert. Die Bibliothek verwirft Ausrichtungen, deren Index oder Zeitpunkt zurückliegt (`accessory.py:368-381`). Ein Replay wirkt deshalb nur, wenn die gespeicherte Ausrichtung **älter** ist als das abgespielte Signal. Im Test (Ausrichtung 3 h alt, Signal 2 h alt) fragte der nächste Abruf Schlüssel ab, die 2 h hinter dem wahren Stand lagen. Der Rückstand blieb bestehen, bis ein echtes Signal ihn korrigierte. War die Ausrichtung frisch, blieb das Replay ohne Wirkung. Zusätzlich neu: Jedes solche Signal stößt in der Integration eine Neuberechnung der Kandidaten an, auch wenn die Bibliothek die Ausrichtung ablehnt.

**Problem:**
1. Advertisements sind **nicht authentifiziert**. Wer ein Advertisement des Tags mitschneidet (Reichweite ≈ 10-50 m) und in der Nähe des Home-Assistant-Scanners wieder aussendet, erzeugt einen Treffer. Beim DULT-Format reicht schon die MAC-Adresse (`match_dult_advertisement` vergleicht nur `_lookup_prefix(mac)`).
2. Ein Treffer mit `can_align=True` ruft `update_alignment(detected_at, key_index)` auf und **speichert** das Ergebnis stündlich im Config-Entry. Das Fenster der Kandidaten reicht 36 h zurück. Ein Replay eines bis zu 36 h alten Advertisements wird also als Treffer gewertet. Die Prüfung `observation.detected_at < previous.detected_at` greift nicht, weil der Replay neu empfangen wird. Ob die Ausrichtung dadurch tatsächlich verschoben wird, hängt vom gespeicherten Stand ab (siehe Nachprüfung oben).

**Auswirkung:**
- Präsenzsensoren melden „zu Hause“, obwohl der Tag fehlt. Das wird kritisch, wenn Automationen damit Türen, Alarmanlagen oder Ähnliches steuern.
- Ist die gespeicherte Ausrichtung älter als das abgespielte Signal, hinkt die Ortung über das Netzwerk um das Alter des Signals hinterher, bis ein echtes Advertisement korrigiert (per Simulation bestätigt). Der Schaden ist begrenzt und setzt voraus, dass der Angreifer in Funkreichweite des Scanners ist.

**Empfehlung:**
1. Die Ausrichtung aus Bluetooth nur übernehmen, wenn der Index nicht hinter der Erwartung aus der letzten bestätigten Ortung zurückliegt. Neuberechnungen der Kandidaten entprellen.
2. In der README und in der Beschreibung des Präsenzsensors klar sagen: **Keine Sicherheitsfunktion.** Nicht für Zutrittssteuerung verwenden.
3. Für DULT-Treffer prüfen, ob dieselbe MAC-Adresse vorher schon über das Offline-Finding-Format mit vollem Schlüssel bestätigt wurde.

### S6 Positionsverlauf in unbegrenzten Langzeitstatistiken

🟠 Mittel · ✅ verifiziert · [`sensor.py:182-205`](../custom_components/findmy/sensor.py#L182-L205)

**Problem:** Die Sensoren `Latitude` und `Longitude` haben `state_class: measurement` und die Einheit `°`. Home Assistant bildet daraus **Langzeitstatistiken** (stündlich Mittel, Minimum und Maximum). Diese werden von `recorder.purge_keep_days` **nicht** gelöscht.

**Auswirkung:** Ein dauerhafter, stündlich aufgelöster Bewegungsverlauf jedes Trackers, auch von getragenen Gegenständen und damit von Personen, ohne Ablaufdatum. Fachlich ist das außerdem sinnlos, denn das Mittel zweier Koordinaten ist kein Aufenthaltsort.

**Empfehlung:** `state_class` entfernen. Gegebenenfalls Lat/Lon-Sensoren standardmäßig deaktivieren (`entity_registry_enabled_default = False`), denn der Tracker trägt die Position bereits als Attribut. Vorhandene Statistiken sollten Nutzer über Entwicklerwerkzeuge → Statistiken bereinigen; ein Hinweis dazu gehört in die Release-Notes.

### S7 Personenbezogene Daten in Logs und IDs

🟡 Niedrig · ✅ verifiziert · [`config_flow.py:259`](../custom_components/findmy/config_flow.py#L259), [`:472`](../custom_components/findmy/config_flow.py#L472), [`_entity.py:46-66`](../custom_components/findmy/_entity.py#L46-L66)

- Die E-Mail-Adresse wird auf **Info-Ebene** geloggt, also standardmäßig sichtbar.
- Name und E-Mail des Kontos stehen im Gerätenamen (`Apple: Vorname Nachname <mail>`), und der lokale Teil der E-Mail steckt in den Entity-IDs. Für Screenshots und geteilte Dashboards ist das ungünstig.

**Empfehlung:** Die E-Mail nur auf Debug-Ebene loggen und dabei maskieren (`j***@example.com`). Als Gerätenamen „Apple-Konto 1“ oder nur den Vornamen verwenden, die E-Mail höchstens als Attribut.

---

## 4. Befunde: Stabilität und Korrektheit

### R1 Re-Authentifizierung wird nie gestartet

🔴 Hoch · ✅ verifiziert · [`coordinator.py:72-79`](../custom_components/findmy/coordinator.py#L72-L79), [`:217-235`](../custom_components/findmy/coordinator.py#L217-L235), [`storage.py:40`](../custom_components/findmy/storage.py#L40)

**Problem:** Der Coordinator entsteht in `async_setup` ohne `config_entry=`. Home Assistant nimmt dann `config_entries.current_entry.get()`, und das ist in `async_setup` `None`. Der Kommentar in Zeile 226-229 verspricht, dass `ConfigEntryAuthFailed` den Nutzer zur Neuanmeldung auffordert. Home Assistant ruft `async_start_reauth_if_available` aber **nur** auf, wenn `self.config_entry` gesetzt ist (`helpers/update_coordinator.py:553-554`). Selbst wenn ein Eintrag gesetzt wäre, wäre es der falsche: Der Coordinator ist für alle Konten zuständig, Re-Auth gehört aber zum Konto, das gerade fehlgeschlagen ist.

**Auswirkung:** Läuft eine Sitzung ab, werden **alle** Geräte-Entities `unavailable` (`last_update_success = False`). Der Nutzer bekommt keine Aufforderung, der Fehler ist still. 

**Nachprüfung (per Test bestätigt und verschärft):** Nach `ConfigEntryAuthFailed` plant der Coordinator **keinen weiteren Abruf** (`helpers/update_coordinator.py:585`). Ein einziges abgelaufenes Konto beendet damit die Abfragen **aller** Konten, bis Home Assistant neu startet oder jemand manuell aktualisiert.

**Empfehlung:** Bei einem Auth-Fehler explizit den Eintrag **des betroffenen Kontos** in Re-Auth schicken: `entry.async_start_reauth(hass)`. Das setzt voraus, dass die Zuordnung Konto → Entry bekannt ist (siehe Zielarchitektur in Kapitel 8). Der Fehler wird nur für dieses Konto behandelt. Ein Fehler bei Konto A darf Konto B nicht blockieren und nicht alle Geräte auf `unavailable` setzen.

### R2 Fehlgeschlagene Abrufe werden als Erfolg protokolliert

🔴 Hoch · ✅ verifiziert · [`coordinator.py:212-240`](../custom_components/findmy/coordinator.py#L212-L240)

**Problem:**
```python
success = False
try:
    success = True                                   # ← vor dem Aufruf!
    device_reports = await account.fetch_location(devices)
except UnauthorizedError: success = False; ...
except InvalidStateError: success = False; ...
finally:
    self._log(account, FETCH_SUCCESS if success else FETCH_ERROR)
```
Alle anderen Ausnahmen bleiben mit `success = True` stehen und werden als **Erfolg** protokolliert. Das betrifft `UnhandledProtocolError`, `aiohttp.ClientError`, `TimeoutError`, Anisette-Fehler und `KeyError` (etwa `'searchPartyToken'` nach der Apple-Umstellung, siehe R22), und genau diese Fehler sind in der Praxis die häufigsten. Per Test bestätigt für `UnhandledProtocolError` und `TimeoutError`. `EmptyResponseError` fängt die Bibliothek selbst ab. Weil sie außerdem nicht in `UpdateFailed` übersetzt werden, loggt Home Assistant sie als „Unexpected error“ mit Stacktrace.

**Auswirkung:** Die Sensoren `Last fetch result` zeigen `success`, obwohl nichts abgerufen wurde. Die Fehlersuche wird irreführend.

**Empfehlung:** `success = True` **nach** dem `await` setzen. Bekannte Fehler (`UnhandledProtocolError`, `EmptyResponseError`, `aiohttp.ClientError`, `TimeoutError`) gezielt fangen und in `UpdateFailed` übersetzen, mit kurzer Meldung und ohne Stacktrace. Nur wirklich Unerwartetes darf als Stacktrace erscheinen.

### R3 Abfrageintervall wird nach dem Entfernen eines Kontos nicht angepasst

🔴 Hoch · ✅ verifiziert · [`__init__.py:103-126`](../custom_components/findmy/__init__.py#L103-L126)

**Problem:** `async_setup_entry` ruft `coordinator.reload()` auf, `async_unload_entry` dagegen nicht. Das Intervall ist `900 s / Anzahl Konten`.

**Beispiel:** Bei zwei Konten liegt das Intervall bei 450 s. Wird ein Konto entfernt oder deaktiviert, bleibt es bei 450 s, und das verbleibende Konto wird **alle 7,5 Minuten** abgefragt, also doppelt so oft wie zugesagt. Das gilt auch, wenn bei einer Re-Authentifizierung `async_update_reload_and_abort` neu lädt, bis zum nächsten Setup.

**Auswirkung:** Höheres Sperrrisiko für das Apple-Konto. Genau davor warnt die README.

**Empfehlung:** Nach jedem Unload `reload()` aufrufen. Grundsätzlich besser: Das Rate-Limit pro Konto erzwingen statt über ein globales Intervall (siehe R4).

### R4 Rate-Limit wird nicht erzwungen

🟠 Mittel · ✅ verifiziert · [`__init__.py:95-98`](../custom_components/findmy/__init__.py#L95-L98), [`sensor.py:146-147`](../custom_components/findmy/sensor.py#L146-L147), [`:314-315`](../custom_components/findmy/sensor.py#L314-L315), [`binary_sensor.py:104-105`](../custom_components/findmy/binary_sensor.py#L104-L105)

**Problem:** Das Limit „1 Abruf pro 15 Minuten pro Konto“ ergibt sich nur aus dem Intervall, es wird nicht pro Konto geprüft. Zusätzliche Abrufe entstehen durch:
- **Jedes Setup eines Eintrags**, auch wenn ein neues Gerät hinzugefügt wird. `schedule_refresh()` holt nach 10 s Daten und nimmt dafür das **nächste** Konto der Reihe.
- **`homeassistant.update_entity`** auf irgendeine FindMy-Entity, auch aus Automationen. `async_update` ruft `async_request_refresh()` auf und wird nur 10 s entprellt.
- **Neustarts von Home Assistant**: Das Abrufprotokoll liegt nur im Speicher, nach 10 s wird unabhängig vom letzten Abruf neu abgefragt.

**Auswirkung:** Bei häufigen Neustarts, beim Import vieler Geräte oder bei Automationen mit `update_entity` steigt die Zahl der Anfragen beliebig.

**Empfehlung:** Einen Scheduler pro Konto mit **persistiertem** Zeitpunkt des letzten Abrufs (`helpers.storage.Store`). Ein Abruf erfolgt nur, wenn `now - last_fetch >= 15 min` ist. Manuelle Refreshes werden bis dahin zurückgestellt und lösen keinen weiteren Abruf aus. `async_update` aus den Entities entfernen, denn sie pollen nicht.

### R5 Erneuerte Sitzungsdaten werden nicht gespeichert

🟠 Mittel · ✅ verifiziert · [`coordinator.py:216`](../custom_components/findmy/coordinator.py#L216), FindMy `reports/account.py:690-700`

**Problem:** Bekommt FindMy ein HTTP 401, meldet es sich mit dem gespeicherten Passwort neu an und erneuert `login_state_data` mit neuen Tokens. Die Integration schreibt `account.to_json()` aber **nie** in den Config-Entry zurück, außer im Re-Auth-Flow.

**Auswirkung:** Nach jedem Neustart wird mit den veralteten Tokens begonnen. Das führt zu 401, Neuanmeldung mit Passwort, und das bei jedem Neustart erneut. Häufige Passwort-Logins von wechselnden Anisette-Identitäten sind ein typischer Auslöser für Sicherheitsabfragen und Sperren bei Apple. Ohne gespeichertes Passwort (Empfehlung zu S1) wäre jede Sitzungserneuerung nach dem Neustart verloren.

**Empfehlung:** Nach jedem Abruf `to_json()` mit dem gespeicherten Stand vergleichen und bei einer Änderung `async_update_entry` aufrufen. Das Passwort bleibt dabei entfernt (siehe S1).

### R6 Config-Flow: unbehandelte Ausnahmen und falsche Step-ID

🟠 Mittel · ✅ verifiziert · [`config_flow.py`](../custom_components/findmy/config_flow.py)

| Stelle | Problem |
|---|---|
| [`:271-281`](../custom_components/findmy/config_flow.py#L271-L281), [`:481-495`](../custom_components/findmy/config_flow.py#L481-L495) | Gefangen werden nur `InvalidCredentialsError` und `UnhandledProtocolError`. Netzwerkfehler (`aiohttp.ClientError`, `TimeoutError`), eine nicht erreichbare Anisette-URL oder ein ungültiges URL-Format führen zu „Unbekannter Fehler“ und einem Stacktrace. |
| [`:301`](../custom_components/findmy/config_flow.py#L301), [`:362`](../custom_components/findmy/config_flow.py#L362) | `get_2fa_methods()` und `request()` sind ungeschützt. FindMy wirft hier `UnhandledProtocolError`, wenn Apple einen Statuscode ungleich 2xx liefert. |
| [`:356-360`](../custom_components/findmy/config_flow.py#L356-L360) | `except KeyError, ValueError` fängt keinen `IndexError` (Listenzugriff `self._2fa_methods[method_id]`). |
| [`:379`](../custom_components/findmy/config_flow.py#L379) | `step_id="2fa_submit"` statt `"acc_2fa_submit"`. Das Absenden führt in den nicht existierenden Schritt `async_step_2fa_submit`. |
| [`:397`](../custom_components/findmy/config_flow.py#L397), [`:405-425`](../custom_components/findmy/config_flow.py#L405-L425) | Nach der 2FA wird nicht geprüft, ob `login_state == LOGGED_IN` ist. Ein halb angemeldetes Konto würde gespeichert. |
| [`:662-690`](../custom_components/findmy/config_flow.py#L662-L690) | Beim Datei-Import wird nur `ValueError` unterdrückt. `FindMyAccessory.from_plist` wirft bei fehlenden Feldern `KeyError` (FindMy `accessory.py:420-440`), und das bricht den Flow ab. |

**Empfehlung:** Ein einheitliches Fehler-Mapping: Fehlerklasse → Fehlerschlüssel (`cannot_connect`, `anisette_unreachable`, `invalid_auth`, `2fa_invalid`, `invalid_file`) mit passenden Übersetzungen. Die Anmeldelogik von `acc_login` und `reauth_confirm`, die heute doppelt existiert, in eine Methode `_async_login()` zusammenführen.

### R7 Re-Authentifizierung akzeptiert ein anderes Konto

🟠 Mittel · ✅ verifiziert · [`config_flow.py:444-501`](../custom_components/findmy/config_flow.py#L444-L501)

**Problem:** Im Re-Auth-Schritt ist die E-Mail nur vorausgefüllt und lässt sich ändern. Es gibt kein `async_set_unique_id` und kein `_abort_if_unique_id_mismatch()`. Meldet sich der Nutzer mit einem anderen Konto an, überschreibt das die Daten des bestehenden Eintrags. `unique_id` und Titel zeigen danach weiter auf das alte Konto.

**Empfehlung:** `await self.async_set_unique_id(email.lower())`, dann `self._abort_if_unique_id_mismatch(reason="wrong_account")`. Statt `self.context["entry_id"]` den modernen Helfer `self._get_reauth_entry()` verwenden.

### R8 Geräte können doppelt angelegt werden

🟠 Mittel · ✅ verifiziert · [`config_flow.py:547-659`](../custom_components/findmy/config_flow.py#L547-L659)

**Problem:** Geräte-Einträge bekommen keine `unique_id`. Wird derselbe Schlüssel zweimal importiert, entstehen zwei Einträge mit identischen Entity-`unique_id`s. Home Assistant lehnt die Entities des zweiten Eintrags ab („ID already exists“). Der Eintrag bleibt aber bestehen, und das Gerät taucht im Coordinator nur einmal auf.

**Empfehlung:** `await self.async_set_unique_id(device_unique_id(device))` und `self._abort_if_unique_id_configured()`. Bestehende Einträge per Migration nachrüsten.

### R9 Kontogeräte kollidieren bei gleichem lokalen Teil der E-Mail

🟠 Mittel · ✅ verifiziert · [`_entity.py:65-66`](../custom_components/findmy/_entity.py#L65-L66), [`sensor.py:322-328`](../custom_components/findmy/sensor.py#L322-L328)

**Problem:** Der Identifier im Geräteregister ist `email.split("@")[0]`. `max@gmail.com` und `max@icloud.com` landen damit **auf demselben Gerät**. Die `unique_id`s der Entities nutzen dagegen die volle E-Mail, sie kollidieren also nicht.

**Empfehlung:** Für den Identifier `account_unique_id` (volle E-Mail, besser noch ein Hash davon) verwenden. Bestehende Geräte per Migration umziehen (`device_registry.async_update_device(new_identifiers=…)`).

### R10 Defekte Eintragsdaten und abgemeldete Konten beim Start

🟠 Mittel · ✅ verifiziert · [`__init__.py:77-100`](../custom_components/findmy/__init__.py#L77-L100), [`storage.py:83-136`](../custom_components/findmy/storage.py#L83-L136)

- Wirft `from_json` (`ValueError` oder `KeyError` bei beschädigten Daten), scheitert das Setup mit einem generischen Stacktrace. Besser ist `ConfigEntryError` mit einer verständlichen Meldung.
- Ein Konto, das mit `login_state != LOGGED_IN` gespeichert ist, wird geladen, als wäre alles in Ordnung. Der Fehler zeigt sich erst beim nächsten Abruf, und dann greift R1. Besser: sofort `ConfigEntryAuthFailed` werfen, dann startet Home Assistant Re-Auth korrekt für **diesen** Eintrag.
- Ein Fehler in `async_forward_entry_setups` hinterlässt einen Eintrag in `RuntimeStorage`, ohne dass es Entities gibt.

### R11 Bluetooth ist harte Abhängigkeit, obwohl optional

🟡 Niedrig · ✅ verifiziert · [`manifest.json`](../custom_components/findmy/manifest.json), [`device_tracker.py:10`](../custom_components/findmy/device_tracker.py#L10), [`presence.py:13`](../custom_components/findmy/presence.py#L13)

**Nachprüfung:** Von 🟠 auf 🟡 herabgestuft. Das Setup der Bluetooth-Integration endet regulär mit `return True`; einen Abbruch wegen fehlender Adapter habe ich dort nicht gefunden. Ein Totalausfall ist deshalb vermutlich selten. Es bleibt, dass Bluetooth samt `usb` jedem Nutzer aufgezwungen wird und Tests unter Windows nicht laufen.

**Problem:** `"dependencies": ["bluetooth"]` und die Imports auf Modulebene führen dazu, dass FindMy **nicht lädt**, wenn die Bluetooth-Integration nicht startet. Das kommt etwa vor bei Containern ohne D-Bus, auf manchen NAS-Systemen oder bei Windows- und Entwicklungsumgebungen (siehe Kapitel 7). Der Abruf über das Find-My-Netzwerk braucht aber kein Bluetooth.

**Empfehlung:** In `after_dependencies` verschieben. Den Bluetooth-Abgleich in ein eigenes Modul auslagern, das nur geladen wird, wenn `"bluetooth" in hass.config.components` gilt. Siehe auch Kapitel 6.

### R12 Falsche Mindestversion in `hacs.json`

🟠 Mittel · ✅ verifiziert · [`hacs.json`](../hacs.json)

`"homeassistant": "2024.7.4"` ist deutlich zu niedrig. Der Code nutzt Python-3.14-Syntax (`except KeyError, ValueError` nach PEP 758), `config_entry`-Generics und `ConfigFlowResult`, und `pyproject.toml` verlangt `homeassistant>=2026.9.3`. HACS bietet die Integration deshalb älteren Installationen an, die dann mit einem `SyntaxError` scheitern.

**Empfehlung:** In `hacs.json` die tatsächlich getestete Mindestversion eintragen, und in CI gegen genau diese Version testen.

### R13 Auf- und Abbau unvollständig

🟡 Niedrig · ✅ verifiziert

- [`__init__.py:116-118`](../custom_components/findmy/__init__.py#L116-L118): Das Ergebnis von `async_unload_platforms` wird verworfen (`_ =`). Wenn das Entladen fehlschlägt, meldet die Integration trotzdem Erfolg. Außerdem wird das Konto **vor** den Plattformen geschlossen.
- `RuntimeStorage.detach()` wird nie aufgerufen. Coordinator und Timer (`_refresh_handle`) bleiben nach dem Entladen des letzten Eintrags bestehen. Ein ausstehender `call_later` kann nach dem Entladen noch einen Abruf auslösen.
- `local_observations`, `local_status` und `local_rssi` werden beim Entladen nicht bereinigt.
- [`__init__.py:70`](../custom_components/findmy/__init__.py#L70): `async_setup` ist mit `ConfigEntry` statt `ConfigType` annotiert, und `CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)` fehlt.

### R14 HTTP-Sessions im Config-Flow werden nie geschlossen

🟡 Niedrig · ✅ verifiziert · [`config_flow.py:268`](../custom_components/findmy/config_flow.py#L268), [`:479`](../custom_components/findmy/config_flow.py#L479)

Das `AsyncAppleAccount` des Flows wird nach `to_json()`, bei einem Abbruch oder bei Fehlern nie mit `close()` geschlossen. Die Folge sind Warnungen „Unclosed client session“ und offene Sockets, auch bei jedem erneuten Versuch nach falschem Passwort, weil jedes Mal ein neues Konto entsteht.

**Empfehlung:** `try/finally` um den Login und `async_remove()` im Flow überschreiben, um aufzuräumen.

### R15 Filter `rolling` im Löschdienst ist wirkungslos

🟡 Niedrig · ✅ verifiziert · [`services.py:50`](../custom_components/findmy/services.py#L50)

Der Filter prüft `entry_type == "device_rolling"`. Seit Migration v2 heißen die Typen aber `device_rolling_derived` und `device_rolling_pre_generated`. Der Docstring erwähnt außerdem den nicht existierenden Typ `openhaystack`.

### R16 Sensor „Nächster Abruf“ falsch berechnet

🟡 Niedrig · ✅ verifiziert · [`coordinator.py:143-150`](../custom_components/findmy/coordinator.py#L143-L150)

Der Code rechnet `last + 15 min × Anzahl Konten`. Richtig ist `last + 15 min`: Das Intervall beträgt `15 min / n`, und bei Round-Robin über `n` Konten kommt jedes Konto alle 15 Minuten dran. Bei drei Konten zeigt der Sensor deshalb 45 statt 15 Minuten.

### R17 Abrufprotokoll wächst unbegrenzt

🟡 Niedrig · ✅ verifiziert · [`coordinator.py:84`](../custom_components/findmy/coordinator.py#L84), [`:194-198`](../custom_components/findmy/coordinator.py#L194-L198)

`self._logs` ist eine Liste ohne Obergrenze: rund 200 Einträge pro Tag und Konto. Jeder Sensorzugriff durchsucht sie linear. Über Monate wächst der Speicherbedarf, und die Zugriffe werden langsamer.

**Empfehlung:** Den Zustand pro Konto in einer kleinen Dataclass halten (`last_start`, `last_end`, `last_result`, `last_error`), statt ein Protokoll zu führen.

### R18 Config-Entry wird bei jedem Abruf neu geschrieben

🟡 Niedrig · ✅ verifiziert · [`device_tracker.py:477-486`](../custom_components/findmy/device_tracker.py#L477-L486)

`_handle_coordinator_update` ruft für **jedes** Gerät `_update_entry()` auf. Bei Rolling-Key-Accessoires ändert sich die Ausrichtung, also wird `core.config_entries` bei jedem Abruf und für jedes Gerät neu geschrieben. Das passt nicht zur Drosselung auf eine Stunde, die der Bluetooth-Pfad (`_LOCAL_ALIGNMENT_SAVE_DELAY`) bewusst einführt. Auf SD-Karten erhöht es den Verschleiß.

**Empfehlung:** Speichern zentral und gedrosselt, und nur, wenn sich die Daten geändert haben.

### R19 Blueprint `tag_stale` prüft nur einmal pro Stunde

🟡 Niedrig · ✅ verifiziert · [`blueprints/automation/tag_stale.yaml:52-62`](../blueprints/automation/tag_stale.yaml#L52-L62)

- `time_pattern` mit `minutes: 30` löst **nur zur Minute 30** jeder Stunde aus, nicht alle 30 Minuten. Richtig wäre `"/30"`, und das lässt sich mit einem Zahlen-Input nicht direkt abbilden.
- Die erste `condition` ist toter Code: Sie liefert immer `true` und verweist auf `trigger.event_data`, das es bei `time_pattern` nicht gibt.
- Ohne Merker wird bei jedem Auslösen erneut benachrichtigt, solange der Tag veraltet ist.
- Die Syntax ist veraltet (`trigger:`, `platform:`, `service:`). Seit Home Assistant 2024.10 heißt es `triggers:`, `trigger:` und `action:`.

---

## 5. Befunde: Wartbarkeit und Dokumentation

| ID | Befund | Ort | Empfehlung |
|---|---|---|---|
| W1 | **Typen der Einträge als Strings** (`"account"`, `"device_static"` …), verteilt über `storage.py`, `services.py`, `device_tracker.py` und `__init__.py`. R15 ist genau daraus entstanden. | mehrere | `StrEnum EntryType` in `const.py`, plus eine Registry `EntryType → (Klasse, Serialisierung)` |
| W2 | **Doppelte Logik:** `unique_id` und `device_info` des Trackers duplizieren `_entity.device_unique_id` und `build_device_info`. | [`device_tracker.py:337-414`](../custom_components/findmy/device_tracker.py#L337-L414) | Gemeinsame Basisklasse `FindMyDeviceEntity` für alle Geräte-Entities |
| W3 | **Drei Basisklassen mit derselben Mechanik** (Coordinator-Listener, Dispatcher, Cache, `available`) in `sensor.py` und `binary_sensor.py`. Das Override `available` mit `functools.cached_property` funktioniert nur, weil Home Assistant den Cache beim Setzen von `_attr_available` leert (per Test geprüft), und ist damit fragil. | [`sensor.py:101-179`](../custom_components/findmy/sensor.py#L101-L179), [`binary_sensor.py:58-136`](../custom_components/findmy/binary_sensor.py#L58-L136) | `CoordinatorEntity` nutzen und `EntityDescription` mit `value_fn` verwenden; das reduziert die Sensorklassen auf Datenbeschreibungen |
| W4 | **Tracker mit zwei Aufgaben:** `FindMyDeviceTracker` enthält rund 250 Zeilen Bluetooth-Logik. Presence- und Batteriesensoren hängen davon ab, dass die Tracker-Entity aktiviert ist. Wird sie deaktiviert, gibt es keine lokalen Daten. | [`device_tracker.py:118-327`](../custom_components/findmy/device_tracker.py#L118-L327) | Eigener `LocalBluetoothManager` pro Accessoire oder zentral für alle, unabhängig von Entities, mit einem gemeinsamen Callback statt drei pro Accessoire |
| W5 | **Toter oder falscher Code:** `RuntimeStorage.static_tags` liefert Konten statt Tags. | [`storage.py:76-78`](../custom_components/findmy/storage.py#L76-L78) | Entfernen |
| W6 | **Inkonsistente Batterie-Doku:** Der Docstring nennt „0/50/70/90“, der Code nutzt 90/65/40/15. Die README nennt für `local_battery` die Werte „Full/Medium/Low/Very Low“, der Sensor `ok/medium/low/critical`. | [`device_tracker.py:392-396`](../custom_components/findmy/device_tracker.py#L392-L396), [README](../README.md) | Eine Skala und eine Doku |
| W7 | **Kommentare fehlen** in `coordinator.py`, `storage.py`, `__init__.py`, `config_flow.py` (keine Modul-Docstrings, keine Begründungen). Ruff ignoriert `D` komplett. | – | Ruff `D` mit Google-Konvention aktivieren, zunächst nur `D1xx` für öffentliche Klassen und Funktionen |
| W8 | **Feste englische Texte** in der UI: Menüoptionen (`"Apple Account"`, `"FindMy Device"`), 2FA-Labels, Titel der Einträge. Es gibt keine deutsche Übersetzung. | [`config_flow.py:218-221`](../custom_components/findmy/config_flow.py#L218-L221), [`:309`](../custom_components/findmy/config_flow.py#L309) | Übersetzungsschlüssel verwenden und `translations/de.json` ergänzen |
| W9 | **Enum-Sensoren ohne `options`** (`State`, `Last fetch result`). Das ist erlaubt, aber dann bietet die Oberfläche keine Auswahl in Automationen, und es werden keine Übersetzungen genutzt. | [`sensor.py:349-399`](../custom_components/findmy/sensor.py#L349-L399) | `options` und `translation_key` setzen |
| W10 | **Manuelle `entity_id`s** über `generate_entity_id` widersprechen `has_entity_name` und erschweren Umbenennungen. | [`device_tracker.py:112-116`](../custom_components/findmy/device_tracker.py#L112-L116), [`sensor.py:296-300`](../custom_components/findmy/sensor.py#L296-L300) | Home Assistant die IDs generieren lassen (Achtung: Für bestehende Installationen ist eine Migration der Registry nötig, sonst ändern sich die IDs) |

---

## 6. Abhängigkeiten: Bestand und Reduktion

> **Nachprüfung:** Dieses Kapitel ist durch [Kapitel 8 der Strategie](REFACTORING_STRATEGY.md#8-abhängigkeiten) ersetzt. Dort stehen die geprüften Fakten, Alternativen und die Empfehlung. Die wichtigste Korrektur: `fs` wird nirgends importiert und ist **kein** Ausfallrisiko. Die betroffenen Stellen unten sind entsprechend berichtigt.

### Bestand

**Laufzeit** (installiert Home Assistant über `manifest.json`):

| Paket | Herkunft | Bereits in Home Assistant? | Zweck | Größe |
|---|---|---|---|---|
| `FindMy==0.10.2` | direkt | nein | Apple-Protokoll, Schlüssel, Reports | 0,2 MB |
| `srp` (+ `six`) | FindMy | nein | SRP-Login bei Apple | < 0,1 MB |
| `beautifulsoup4` (+ `soupsieve`) | FindMy | nein | Telefonnummern für SMS-2FA aus HTML extrahieren | 0,8 MB |
| `anisette` | FindMy | nein | Lokale Anisette-Erzeugung | 0,1 MB |
| ↳ `unicorn` | anisette | nein | CPU-Emulator für Apples ARM-Bibliotheken | **58 MB**, nativ |
| ↳ `pyelftools` | anisette | nein | ELF-Parser | 0,7 MB |
| ↳ `fs` (+ `appdirs`, `setuptools`, `six`) | anisette | nein | Virtuelles Dateisystem | 0,6 MB |
| `cryptography`, `aiohttp`, `bleak`, `certifi`, `urllib3`, `typing-extensions` | FindMy/anisette | ja | – | – |
| Integration `bluetooth` | `manifest.json` → `dependencies` | ja (Komponente) | lokale Erkennung | – |

**Entwicklung** (`pyproject.toml`): `homeassistant` (≈ 130 Pakete), `basedpyright`, `ruff`, `pre-commit`.

### Risiken im Bestand

- **`fs` 2.4.16 ist eine Altlast, aber kein Ausfallrisiko (korrigiert).** Das Paket ist seit 2022 ohne Release und nutzt `pkg_resources`. `anisette` 1.2.4 führt es aber nur noch in den Metadaten und importiert es nicht mehr. Per Test geprüft: `anisette` und `findmy` laden auch, wenn `fs`, `appdirs` und `pkg_resources` blockiert sind. Die erste Fassung dieses Reviews warnte hier fälschlich vor einem Totalausfall.
- **`bleak` ist das eigentliche Kopplungsrisiko.** `FindMy` verlangt `bleak<3.1.0`, Home Assistant legt `bleak==3.0.2` fest. Hebt Home Assistant auf 3.1 an, lässt sich `FindMy==0.10.2` nicht mehr installieren.
- **`unicorn`** ist mit 58 MB das größte Paket. Es braucht native Wheels für jede Architektur und ist der Grund für lange Installationszeiten auf ARM-Geräten wie dem Raspberry Pi.
- **Doppelte Versionsangaben:** `manifest.json` pinnt `FindMy==0.10.2`, `pyproject.toml` erlaubt `findmy>=0.10.2`. Die Entwicklungsumgebung kann damit von dem abweichen, was Nutzer bekommen.
- **`homeassistant` als Laufzeitabhängigkeit** in `[project].dependencies`. Für eine Custom-Integration stellt Home Assistant die Umgebung bereit, das Paket gehört in die Dev-Gruppe.

### Reduktionsmöglichkeiten

| # | Maßnahme | Einsparung | Aufwand | Abhängig von |
|---|---|---|---|---|
| A1 | **Bluetooth optional machen** (R11): `after_dependencies`, Bluetooth-Code verzögert laden | Harte Abhängigkeit auf die Bluetooth-Komponente samt `habluetooth`, `dbus-fast`, `bluetooth-adapters` und USB-Erkennung entfällt | mittel | nur Integration |
| A2 | **`anisette` in FindMy.py optional machen** (Upstream-PR): `findmy[local-anisette]` als Extra; `LocalAnisetteProvider` importiert `anisette` erst bei Bedarf. FindMy importiert es heute auf Modulebene (`reports/anisette.py:17`). | ≈ 60 MB (`unicorn`, `pyelftools`, `fs`, `appdirs`) und das `pkg_resources`-Risiko, für alle Nutzer mit Anisette-Server | klein upstream, dann trivial | Upstream FindMy.py |
| A3 | **`fs` aus den Abhängigkeiten von `anisette` streichen** (Upstream-PR, korrigiert): Das Paket wird nicht mehr importiert | `fs`, `appdirs` | eine Zeile | Upstream anisette |
| A4 | **`beautifulsoup4` in FindMy.py ersetzen** (Upstream): Die Telefonnummern stehen als JSON in einem `<script>`-Tag, ein gezielter Regex oder `html.parser` aus der Standardbibliothek reicht | 0,9 MB, 2 Pakete | klein | Upstream FindMy.py |
| A5 | **`homeassistant` in die Dev-Gruppe verschieben**, besser durch `pytest-homeassistant-custom-component` ersetzen; das pinnt HA passend und bringt die Test-Fixtures mit | sauberere Semantik, reproduzierbare Tests | klein | nur Integration |
| A6 | **Versionsangaben vereinheitlichen:** `findmy==0.10.2` in `pyproject.toml`, in CI per Skript gegen `manifest.json` prüfen | verhindert Abweichungen | klein | nur Integration |
| A7 | `pre-commit` beibehalten. Es ist nur ein Dev-Werkzeug, und der Gewinn beim Entfernen wäre gering. Die GitHub-Action `pre-commit-ci/lite-action` setzt aber die App pre-commit.ci voraus; ohne sie ist der Schritt nutzlos und kann entfallen. | – | – | – |

**Empfehlung:** A1, A5 und A6 sofort im Fork umsetzen. A2 bis A4 als Issues bzw. PRs bei `malmeloo/FindMy.py` und `malmeloo/anisette` einbringen. Das kommt allen Nutzern zugute, und der Fork muss keine Bibliothek mitpflegen. Ein eigener Fork von FindMy.py ist **nicht** empfohlen: Das Apple-Protokoll ändert sich laufend, und ein Fork würde schnell veralten.

---

## 7. Entwicklungsumgebung, Tests und CI

| Thema | Stand | Empfehlung |
|---|---|---|
| **Tests** | Es gibt keine. | Mit `pytest-homeassistant-custom-component` beginnen. Zuerst `local_bluetooth.py` (reine Funktionen), dann Config-Flow (gemockte FindMy-Klassen), dann Coordinator (Fehlerpfade R1–R5). Ziel: Jeder Befund aus Kapitel 3 und 4 bekommt einen Regressionstest. |
| **Import von HA** | Mit `mashumaro` 3.16 schlug **jeder** Import aus dem Kern von Home Assistant unter Python 3.14 fehl (`webrtc_models`). **Behoben** in `a46c8a9` (Update auf 3.23). | Nach `uv lock`-Updates in CI einen Import-Smoke-Test ausführen. |
| **Bluetooth unter Windows** | `homeassistant.components.bluetooth` lässt sich lokal nicht importieren (`aiousbwatcher` fehlt, Linux-spezifisch). | Tests, die Bluetooth brauchen, in CI unter Linux oder lokal in WSL bzw. im Devcontainer ausführen. Mit A1 lassen sich Tests ohne Bluetooth unter Windows ausführen. |
| **Docker-Compose** | Bindet `/run/dbus` und `/etc/localtime` ein, auf Docker Desktop für Windows nicht vorhanden. | Ein Override `docker-compose.windows.yml` ohne diese Mounts, oder einen Devcontainer. |
| **CI** | Läuft nur `pre-commit`. | `hassfest` (`home-assistant/actions/hassfest`) und `hacs/action` (Validierung) ergänzen, dazu `pytest`. Gegen die Mindestversion aus `hacs.json` testen. |
| **Release** | `manifest.json` steht dauerhaft auf `v0.0.1`. | Versionieren und Releases über GitHub-Releases (HACS zeigt diese an). |

---

## 8. Refactoring-Plan

Die Reihenfolge ist so gewählt, dass jede Phase einzeln lieferbar ist und die gefährlichsten Punkte zuerst geschlossen werden. Jede Phase endet mit grünen Prüfungen und einem Commit.

### Phase 0: Testbasis (Voraussetzung)

- `pytest-homeassistant-custom-component` als Dev-Abhängigkeit einführen (A5), `tests/` mit `conftest.py` und Fixtures für gemockte `AsyncAppleAccount`, `KeyPair` und `FindMyAccessory`.
- Erste Tests für `local_bluetooth.py` und `_entity.py` (reine Funktionen, keine Mocks nötig).
- CI um `pytest`, `hassfest` und die HACS-Validierung erweitern.

### Phase 1: Sofortmaßnahmen (kleine Änderungen, großer Effekt)

| Befund | Änderung |
|---|---|
| S2 | Zentrale Redaktion aller Flow-Logs |
| S3 | Admin-Dienst, kein Default `all`, Bestätigungsfeld |
| R2 | `success`-Flag korrigieren, Fehler-Mapping auf `UpdateFailed` |
| R3 | `coordinator.reload()` nach dem Unload |
| R6 | `step_id` korrigieren, `IndexError` fangen, Netzwerkfehler abfangen, `LOGGED_IN` prüfen |
| R7 | Prüfung, ob die `unique_id` bei Re-Auth übereinstimmt |
| R12 | `hacs.json` korrigieren |
| R15, R16 | Filter und Berechnung korrigieren |
| R19 | Blueprint reparieren |
| S6 | `state_class` für Lat/Lon entfernen |

### Phase 2: Sitzungen und Rate-Limit (Kern der Stabilität)

Zielarchitektur: **`entry.runtime_data` statt globalem `hass.data`**, und **ein Abruf-Scheduler pro Konto**.

```
ConfigEntry (Konto)  ── runtime_data ──►  AccountRuntime
                                            ├─ account: AsyncAppleAccount
                                            ├─ state: AccountFetchState (last_start/end/result/error)  ← persistiert (Store)
                                            └─ entry (für Re-Auth & Token-Persistenz)

FindMyHub (einmalig, hass.data[DOMAIN], mit sauberem Auf- und Abbau)
  ├─ accounts: dict[entry_id, AccountRuntime]
  ├─ devices:  dict[entry_id, FindMyDevice]
  ├─ scheduler: wählt das Konto mit dem ältesten Abruf, sofern ≥ 15 min her; sonst warten
  └─ coordinator: DataUpdateCoordinator (hält nur Location-Daten, config_entry=None ist dann korrekt,
                  Auth-Fehler werden pro Konto über AccountRuntime.entry.async_start_reauth gemeldet)
```

- R1: Re-Auth gezielt für das betroffene Konto, andere Konten laufen weiter.
- R4: Rate-Limit pro Konto, persistiert über Neustarts. Manuelle Refreshes werden zusammengefasst.
- R5 und S1: Sitzung nach jeder Änderung speichern, ohne Passwort. Eine Migration entfernt gespeicherte Passwörter.
- R10: Beim Setup die Daten validieren; `ConfigEntryAuthFailed` werfen, wenn das Konto nicht angemeldet ist.
- R13, R14, R17: sauberer Lebenszyklus, Sessions schließen, Protokoll ersetzt durch einen Zustand.
- S4: `anisette_libs_path` mit Hash-Prüfung.

### Phase 3: Struktur und Wartbarkeit

- W1: `EntryType`-Enum und Registry für die Serialisierung.
- W2 und W3: Basisklasse `FindMyDeviceEntity(CoordinatorEntity)` und Sensoren über `EntityDescription` mit `value_fn`.
- W4 und R11: Bluetooth als eigener, optionaler Dienst (`local_bluetooth_manager.py`) mit **einem** Callback für alle Accessoires, entkoppelt von der Tracker-Entity.
- R8 und R9: `unique_id`s für Geräte-Einträge und Kontogeräte, mit Migration (Config-Entry-Version 2.1).
- R18: zentrales, gedrosseltes Speichern.
- W7 bis W10: Docstrings (Ruff `D1`), Übersetzungen inklusive Deutsch, Enum-Optionen.

### Phase 4: Härtung und Datenschutz

- S5: Plausibilitätsprüfung der Ausrichtung, Warnhinweis in der Doku.
- S7: Maskierung personenbezogener Daten.
- `diagnostics.py` mit Redaktion (siehe Kapitel 9).
- Dependency-PRs A2 bis A4 upstream einreichen.

---

## 9. Ausbau-Perspektiven

Diese Erweiterungen ergeben sich direkt aus den Befunden und bauen auf der Architektur aus Phase 2 und 3 auf:

| Idee | Anknüpfung | Nutzen |
|---|---|---|
| **Diagnostics** (`diagnostics.py`) | S1, S2, R2 | Nutzer können einen Diagnose-Download an Issues hängen, mit Geheimnissen redigiert statt Debug-Logs zu kopieren. |
| **Repairs-Hinweise** (`issue_registry`) | R1, S4, R11 | Sichtbare Hinweise in Home Assistant, etwa „Anisette-Server nicht erreichbar“, „Konto X benötigt erneute Anmeldung“, „Bluetooth nicht verfügbar, lokale Erkennung deaktiviert“. |
| **Sensor für Kontostatus** (angemeldet / Re-Auth nötig / gesperrt / Anisette-Fehler) | R1, R2, W9 | Echte Fehlerursache statt nur `idle`/`fetching`; damit lassen sich Automationen bauen. |
| **Standortverlauf** über `fetch_location_history` (FindMy unterstützt bis zu 7 Tage) | R4 | Lücken nach Ausfällen oder Neustarts schließen; Pfade auf der Karte. Nur im Rahmen des Rate-Limits pro Konto. |
| **Einstellbares Abfrageintervall** pro Konto (≥ 15 min) | R3, R4 | Wer selten Updates braucht, schont das Konto. |
| **Re-Import / Schlüssel-Export** für Geräte | S3 | Ersatz für den Löschdienst als Werkzeug für Massenänderungen, mit Sicherung. |
| **Zentraler Bluetooth-Scanner** mit Statistik (gesehene Accessoires, Trefferquote) | W4, S5 | Bessere Diagnose der lokalen Erkennung; eine Grundlage für ein Ereignis wie „unbekannter Tracker folgt mir“ (DULT-Anti-Stalking). |
| **Ereignisse** (`findmy_location_updated`, `findmy_accessory_seen`) | W4 | Automationen ohne Polling von Attributen; macht den Blueprint `tag_stale` einfacher. |
| **Mehrsprachigkeit** (Deutsch zuerst) | W8 | – |

---

## 10. Anhang: Geprüfte Fakten zu Home Assistant und FindMy.py

Diese Fakten stützen die Befunde. Sie wurden gegen die installierten Versionen (Home Assistant 2026.9.3, FindMy 0.10.2, anisette 1.2.4) geprüft und sollten bei Versionssprüngen erneut geprüft werden.

| Fakt | Quelle |
|---|---|
| `DataUpdateCoordinator` ohne `config_entry` nutzt `config_entries.current_entry.get()`. Re-Auth startet nur, wenn `self.config_entry` gesetzt ist. | `homeassistant/helpers/update_coordinator.py:100-110`, `:538-554` |
| Nach `ConfigEntryAuthFailed` plant der Coordinator keinen weiteren Abruf (`if not auth_failed and self._listeners …: self._schedule_refresh()`). Korrigiert: Die erste Fassung sprach hier von nicht benachrichtigten Listenern. | `helpers/update_coordinator.py:585`, Test |
| Der Coordinator entprellt `async_request_refresh` standardmäßig nur mit 10 s Cooldown. | `helpers/update_coordinator.py:35-36` |
| `process_uploaded_file` liefert einen `Path` und löscht die Datei nach dem Kontext. | `components/file_upload/__init__.py:36-57` |
| Enum-Sensoren ohne `options` sind zulässig; mit `options` wird der Wert validiert. | `components/sensor/__init__.py:697-712` |
| Das Setzen von `_attr_available` leert auch ein in der Unterklasse definiertes `functools.cached_property` namens `available`. | Test im Rahmen des Reviews |
| `AsyncAppleAccount.to_json()` enthält `account.password` im Klartext. | `findmy/reports/account.py:439-455` |
| `_gsa_authenticate` speichert Benutzername und Passwort für spätere stille Neuanmeldungen. | `findmy/reports/account.py:799-802` |
| Nach einem HTTP 401 beim Abruf meldet sich FindMy automatisch neu an und erneuert die Tokens im Objekt. | `findmy/reports/account.py:690-700` |
| Der Abruf wiederholt leere Antworten bis zu fünfmal (2–10 s Backoff) und wirft dann `EmptyResponseError`. | `findmy/reports/account.py:650-688` |
| Die HTTP-Session von FindMy hat einen Gesamttimeout von 5 s. | `findmy/util/http.py:102` |
| `__hash__` und `__eq__` von `FindMyAccessory`, `FixedRollingKeyPairAccessory` und `KeyPair` hängen nur an unveränderlichen Schlüsseln, nicht an der Ausrichtung. Die Geräte sind deshalb als Dict-Schlüssel sicher. | `findmy/accessory.py:214-222`, `:529-542`, `findmy/keys.py:63-67` |
| `FindMyAccessory.from_plist` wirft bei fehlenden Feldern `KeyError`. | `findmy/accessory.py:412-440` |
| `LocalAnisetteProvider` lädt die Bibliotheken ohne `libs_path` bei jeder Initialisierung herunter, von `anisette.dl.mikealmel.ooo`, ohne Timeout, Status- oder Hashprüfung. | `findmy/reports/anisette.py:318-370`, `anisette/anisette.py:26`, `anisette/_util.py:44-50` |
| `fs` 2.4.16 ruft `pkg_resources.declare_namespace` auf, wird aber von `anisette` 1.2.4 und `findmy` 0.10.2 nicht importiert. | `fs/__init__.py:4`, Import-Test |
| `FindMyAccessory.update_alignment` verwirft Aufrufe mit älterem Datum oder kleinerem Index. | `findmy/accessory.py:368-381`, Test |
| Pro Accessoire und Abruf leitet FindMy 2.016 Schlüsselpaare synchron ab (3 je Index, 672 Indizes). | Profil, siehe Strategie Kapitel 3 |
| Home Assistant legt `bleak==3.0.2`, `mashumaro>=3.17.0` und `setuptools<82.0.0` fest. | `homeassistant/package_constraints.txt` |
