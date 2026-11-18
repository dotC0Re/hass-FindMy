# Refactoring-Strategie hass-FindMy

| | |
|---|---|
| **Stand** | 18.11.2026, Commit `5284e8a` |
| **Bezug** | Baut auf dem [Code-Review](CODE_REVIEW.md) auf. Die IDs der Befunde (S1, R1 …) stammen von dort. |
| **Inhalt** | Nachprüfung aller Befunde, neue Befunde (Blockade der Event-Loop, Apple-Umstellung), Strategie zur Behebung je Befund, gründliche Analyse der Abhängigkeiten |
| **Messumgebung** | Windows 11, x86-64-Desktop, Python 3.14.10, Home Assistant 2026.9.3, FindMy 0.10.2, anisette 1.2.4. Auf ARM-Geräten (Raspberry Pi, Home Assistant Green) sind die gemessenen Rechenzeiten erfahrungsgemäß um ein Mehrfaches höher. |

## Inhalt

1. [Kurzfassung](#1-kurzfassung)
2. [Ergebnis der Nachprüfung](#2-ergebnis-der-nachprüfung)
3. [Neue Befunde](#3-neue-befunde)
4. [Leitlinien für das Refactoring](#4-leitlinien-für-das-refactoring)
5. [Zielarchitektur](#5-zielarchitektur)
6. [Arbeitspakete](#6-arbeitspakete)
7. [Strategie je Befund](#7-strategie-je-befund)
8. [Abhängigkeiten](#8-abhängigkeiten)
9. [Reihenfolge und Meilensteine](#9-reihenfolge-und-meilensteine)
10. [Offene Entscheidungen](#10-offene-entscheidungen)

---

## 1. Kurzfassung

- **Nachprüfung:** Alle 26 Befunde wurden erneut geprüft, 9 davon mit Testskripten gegen den echten Code, die übrigen erneut am Quelltext. 23 sind bestätigt, drei davon mit Ergänzungen. **R1 ist ernster** als beschrieben: Ein einziges abgelaufenes Konto stoppt die Abfragen für **alle** Konten. **S5 und R11 sind schwächer** als beschrieben, und eine Aussage zu den Abhängigkeiten war **falsch**: Das Paket `fs` wird nirgends importiert, das befürchtete Ausfallrisiko besteht nicht.
- **Blockade von Home Assistant (neu, R20):** Bestätigt und gemessen. Jeder Abruf berechnet pro AirTag rund 2.000 elliptische Schlüssel **direkt in der Event-Loop**. Auf dem Mess-PC sind das 0,9 s pro AirTag und Abruf, bei einem lange nicht georteten Tag 3,9 s. Auf ARM und mit mehreren Tags werden daraus die im Original-Repo gemeldeten 30 bis 90 Sekunden. Die Lösung hat zwei Stufen: abgeleitete Schlüssel zwischenspeichern (spart über 99 % der Rechenzeit) und alle FindMy-Arbeit in einen eigenen Thread verlagern.
- **Apple-Umstellung (neu, R22):** Laut Original-Repo der Bibliothek liefert Apple seit etwa 12.10.2026 für die emulierte Anisette kein Abruf-Token mehr. Der Maintainer hat am 28.10.2026 bestätigt, dass ein Fix noch aussteht. Neue Anmeldungen schlagen damit voraussichtlich fehl. Das habe ich nicht selbst geprüft, weil dafür ein Apple-Konto nötig wäre. Für das Refactoring heißt das: Tests müssen mit Attrappen der Bibliothek arbeiten, und die Integration muss diesen Zustand klar anzeigen statt ihn zu verschleiern.
- **Abhängigkeiten:** Im Fork selbst lässt sich nur wenig entfernen, denn fast alles hängt an `FindMy`. Sofort machbar sind: Bluetooth optional, `homeassistant` raus aus den Laufzeitabhängigkeiten, Versionsangaben vereinheitlichen, die Vorgaben von Home Assistant im Lockfile erzwingen. Der große Hebel liegt in drei kleinen Vorschlägen an das Original-Repo: `fs` aus `anisette` streichen (unbenutzt), `anisette` und `bleak` in `FindMy` optional machen, `beautifulsoup4` durch die Standardbibliothek ersetzen.
- **Vorhandene Vorarbeit:** Im Original-Repo liegen drei offene Pull Requests, die Teile der Befunde beheben (#64 für R16, #66 für R1/R2, #69 für R20). Sie sollten nach einer Durchsicht übernommen werden, statt dasselbe neu zu schreiben.

---

## 2. Ergebnis der Nachprüfung

**Methoden:** *Test* = Skript gegen den echten Code der Integration bzw. der Bibliothek · *Quelle* = erneut im Quelltext nachvollzogen (Integration, Home Assistant, FindMy) · *Messung* = Laufzeit gemessen

| ID | Ergebnis | Methode | Anmerkung |
|---|---|---|---|
| S1 | ✅ bestätigt | Test | `to_json()` enthält das Passwort im Klartext. Zusatz: Ohne gespeichertes Passwort wirft die stille Neuanmeldung `ValueError("No username or password specified")`, nicht `UnauthorizedError`. Die Strategie muss das abfangen. |
| S2 | ✅ bestätigt | Quelle | – |
| S3 | ✅ bestätigt | Quelle | Der WebSocket-Befehl `call_service` verlangt keine Admin-Rechte. Nur `async_register_admin_service` prüft sie. |
| S4 | ✅ bestätigt | Quelle | Der Download passiert beim ersten Gebrauch nach jedem Start (`fs_fallback`), nicht schon beim Laden. |
| S5 | ⚠️ **abgeschwächt** | Test | Die Bibliothek ignoriert Ausrichtungen, die rückwärts gehen. Ein Replay wirkt nur, wenn die gespeicherte Ausrichtung **älter** ist als das abgespielte Signal. Folge ist dann ein dauerhafter Rückstand der Ortung um das Alter des Signals (im Test: 2 h), bis ein echtes Signal ihn korrigiert. Das Vortäuschen von Präsenz bleibt bestehen; es ist bei Bluetooth-Präsenz grundsätzlich nicht vermeidbar. Neuer Grad: 🟡 für die Ausrichtung, 🟠 für die Präsenz (Dokumentationspflicht). |
| S6 | ✅ bestätigt | Quelle | – |
| S7 | ✅ bestätigt | Quelle | – |
| R1 | 🔺 **verschärft** | Test | `config_entry` ist `None`, es startet kein Re-Auth-Flow. **Zusätzlich** plant der Coordinator nach `ConfigEntryAuthFailed` **keinen weiteren Abruf** (`update_coordinator.py:585`). Ein abgelaufenes Konto beendet damit die Abfragen aller Konten bis zum nächsten Neustart oder manuellen Refresh. |
| R2 | ✅ bestätigt, präzisiert | Test | `UnhandledProtocolError` und `TimeoutError` werden als `SUCCESS` protokolliert. `EmptyResponseError` erreicht den Coordinator nicht (die Bibliothek fängt ihn). Neu dazu: `KeyError: 'searchPartyToken'` nach der Apple-Umstellung (R22) fällt ebenfalls in diese Lücke. |
| R3 | ✅ bestätigt | Test | Intervall bleibt nach dem Entladen eines von zwei Konten bei 7:30 min. |
| R4 | ✅ bestätigt, erweitert | Quelle, Messung | Ein „Abruf“ sind in Wahrheit viele HTTP-Anfragen, siehe R21. |
| R5 | ✅ bestätigt | Quelle | – |
| R6 | ✅ bestätigt, erweitert | Test | Zusätzlich: Eine gültige JSON-Datei ohne Feld `type` wirft `KeyError` und bricht den Flow ab. Netzwerkfehler kommen als `aiohttp.ClientError` oder `TimeoutError`. Der Pfad mit der falschen `step_id` ist nur mit leerem Code erreichbar. |
| R7 | ✅ bestätigt | Quelle | – |
| R8 | ✅ bestätigt | Quelle | – |
| R9 | ✅ bestätigt | Quelle | – |
| R10 | ✅ bestätigt | Test | Defekte Daten führen bei allen drei Typen zu `ValueError`. |
| R11 | ⚠️ **abgeschwächt** | Quelle | `bluetooth.async_setup` endet regulär mit `return True`; einen Abbruch wegen fehlender Adapter habe ich dort nicht gefunden. Ein Totalausfall ist deshalb vermutlich selten. Es bleibt: Bluetooth (samt `usb`) wird jedem Nutzer aufgezwungen, und Tests laufen unter Windows nicht. Neuer Grad: 🟡. |
| R12 | ✅ bestätigt | Quelle | Home Assistant 2026.9.3 verlangt Python ≥ 3.14.2; die Syntax `except A, B:` setzt 3.14 voraus. |
| R13 | ✅ bestätigt | Quelle | – |
| R14 | ✅ bestätigt | Quelle | – |
| R15 | ✅ bestätigt | Quelle | – |
| R16 | ✅ bestätigt | Test | Bei 3 Konten zeigt der Sensor 45 min statt 15 min. Im Original-Repo liegt dafür PR #64 vor. |
| R17 | ✅ bestätigt | Test | 2 Einträge pro Abruf, nie gekürzt. |
| R18 | ✅ bestätigt | Quelle | `async_update_entry` schreibt nur bei geänderten Daten. Die Ausrichtung ändert sich aber bei jedem Abruf mit Treffer (`reports.py:457`). |
| R19 | ✅ bestätigt | Quelle | `time_pattern` wertet nur Zeichenketten mit `/` als „alle n Minuten“. |
| W3 | ✅ bestätigt (kein Fehler) | Test | Der `cached_property`-Override funktioniert, weil Home Assistant den Cache leert. |
| Abh. | ❌ **Aussage korrigiert** | Test | `fs` wird weder von `anisette` noch von `FindMy` importiert. Beide laden auch, wenn `fs`, `appdirs` und `pkg_resources` blockiert sind. Die Warnung im Review, die Integration falle mit `pkg_resources` aus, war falsch. Siehe Kapitel 8. |

Die Skripte der Nachprüfung liegen nicht im Repository. Sie gehen in Arbeitspaket AP0 als Regressionstests in `tests/` auf.

---

## 3. Neue Befunde

### R20 Der Abruf blockiert die Event-Loop von Home Assistant

🔴 Hoch · ✅ verifiziert (Messung, Profil) · [`coordinator.py:216`](../custom_components/findmy/coordinator.py#L216), FindMy `reports/reports.py:419-507`, `accessory.py:403`, `:674`

**Problem:** `account.fetch_location()` ist zwar eine Coroutine, rechnet aber zwischen den Netzwerkzugriffen **synchron**. Für jedes Accessoire mit Rolling Keys läuft die Bibliothek alle Schlüsselindizes der letzten 7 Tage ab (673 Stück) und erzeugt je Index drei Schlüsselpaare neu. Abgegeben wird die Kontrolle nur etwa alle 290 Indizes, vor einer HTTP-Anfrage.

**Messung** (ein Accessoire, Netzwerk durch eine sofort antwortende Attrappe ersetzt, also reine Rechenzeit):

| Szenario | Dauer | längste einzelne Blockade |
|---|---|---|
| Tag, vor 1 h zuletzt geortet | 0,9 s | 0,5–0,9 s |
| derselbe Tag, zweiter Abruf (Caches warm) | 0,9 s | 0,5 s |
| Tag, seit 30 Tagen ohne Ortung | 3,9 s | 1,2 s |
| 5 Jahre alter Tag, erster Abruf nach Neustart | 1,15 s | 0,6 s |

- Praktisch die gesamte Dauer ist Blockade: Die Summe der Aussetzer über 100 ms entspricht jeweils der Gesamtdauer.
- **Profil:** 89 % der Zeit stecken in `ec.derive_private_key`, 2.016 Aufrufe pro Accessoire und Abruf. Zwei Drittel davon sind **Wiederholungen**: Die beiden Sekundärschlüssel sind für 96 aufeinanderfolgende Indizes dieselben, werden aber jedes Mal neu abgeleitet (1.344 Ableitungen für etwa 9 verschiedene Schlüssel).
- Warme Caches helfen nicht, weil die Bibliothek nur die Kette der Geheimnisse zwischenspeichert, nicht die fertigen Schlüsselpaare. Zwischen zwei Abrufen ändert sich aber nur **ein** Index von 673.
- Die Zeit wächst linear mit der Zahl der Accessoires und mit dem Alter der Ausrichtung. Ein nie georteter Tag durchsucht **alle** Indizes seit der Kopplung.

**Hochrechnung:** 9 Accessoires ergeben auf dem Mess-PC mindestens 8 s Blockade pro Abruf. Auf einem ARM-Gerät oder einer VM passt das zu den 34 bis 94 Sekunden, die im Original-Repo gemeldet sind (hass-FindMy #70, 9 Accessoires).

**Zweite, schwächere Quelle:** Die Kandidaten für den lokalen Bluetooth-Abgleich werden alle 15 Minuten pro Accessoire aus einer JSON-Kopie neu aufgebaut ([`device_tracker.py:187-199`](../custom_components/findmy/device_tracker.py#L187-L199), [`local_bluetooth.py:90-95`](../custom_components/findmy/local_bluetooth.py#L90-L95)). Das läuft im Executor und blockiert die Loop nicht direkt. Auf dem Mess-PC dauerte es 0,3 bis 0,5 s bei höchstens 0,04 s Aussetzer. Eine frische Kopie beginnt die Schlüsselkette aber immer bei Index 0. Auf langsamen Geräten konkurriert das spürbar mit der Loop um Rechenzeit (hass-FindMy #67 berichtet mehrere Sekunden).

**Behebung:** siehe [AP2](#ap2-event-loop-entlasten).

### R21 Ein Abruf sind viele HTTP-Anfragen

🟠 Mittel · ✅ verifiziert (Messung, Quelle) · FindMy `reports/reports.py:400-413`, `:476-487`

**Problem:** Die README spricht von einer Abfrage pro 15 Minuten und Konto. Tatsächlich stellt die Bibliothek pro Zyklus:
- **eine** Anfrage für alle statischen Schlüssel zusammen,
- **pro Accessoire mit Rolling Keys** eine bis drei Anfragen: eine, wenn in den jüngsten rund 72 h ein Bericht liegt, drei, wenn in 7 Tagen keiner liegt (gemessen),
- bei alter Ausrichtung entsprechend mehr, etwa eine Anfrage je 290 Indizes. Für einen nie georteten, 2,7 Jahre alten Tag nennt PR #69 rund 330 Anfragen pro Abruf; das deckt sich mit der Formel (95.000 Indizes / 290).

Die Anfragen folgen direkt aufeinander. Zehn AirTags bedeuten also 10 bis 30 Anfragen in wenigen Sekunden, und das alle 15 Minuten pro Konto.

**Auswirkung:** Das eigentliche Sperrrisiko hängt an der Zahl der Anfragen, nicht an der Zahl der Zyklen. R3 und R4 wiegen dadurch schwerer.

**Behebung:** siehe [AP3](#ap3-konten-sitzungen-und-abfragebudget) (Budget pro Konto) und [AP2](#ap2-event-loop-entlasten) (Obergrenze für die Indexsuche).

### R22 Apple-Umstellung im Oktober 2026: Anmeldung und Abruf gestört

🔴 Hoch · 🌐 extern berichtet, **nicht selbst geprüft** · FindMy.py #278, PR #279; hass-FindMy #65

**Sachstand laut Original-Repos:**
- Seit etwa 12.10.2026 verlangt Apple, dass die Client-Kennung zur Plattform passt, von der die Anisette-Identität stammt. Die Bibliothek `anisette` emuliert eine Android-Bibliothek.
- Android- und Windows-Identitäten erhalten kein `searchPartyToken` mehr. Ohne dieses Token antwortet der Abruf mit HTTP 401.
- Der Maintainer schrieb am 28.10.2026, dass es weiterhin nicht funktioniert und ein Fix in `Anisette.py` und `FindMy.py` nötig ist. PR #279 („anisette mac backend“) ist offen, zuletzt geändert am 05.11.2026. Die letzte veröffentlichte Version ist weiterhin 0.10.2 vom 14.09.2026.
- Nutzer der Integration melden „Invalid username or password“ bei der Anmeldung.

**Bedeutung für das Refactoring:**
1. Die Kernfunktion (Abruf über das Find-My-Netzwerk) ist für neue Anmeldungen voraussichtlich gestört. Ob bestehende Sitzungen weiterlaufen, ist offen.
2. Ein Test gegen Apple ist derzeit nicht verlässlich möglich. Alle Tests müssen mit Attrappen arbeiten (AP0).
3. Die Fehlerbilder dieser Störung treffen genau die Lücken R1 und R2: `KeyError: 'searchPartyToken'` wird als Erfolg protokolliert, und eine verweigerte Neuanmeldung führt zu keiner Meldung. **AP1 behebt das zuerst.**
4. Automatische Neuanmeldungen mit gespeichertem Passwort sind in diesem Zustand gefährlich: Jeder Zyklus löst einen weiteren fehlschlagenden Login aus. Das stützt die Empfehlung zu S1, das Passwort nicht zu speichern.
5. Die Abhängigkeiten rund um `anisette` werden sich mit dem Fix voraussichtlich ändern. Vorschläge an das Original-Repo (Kapitel 8) sollten darauf abgestimmt werden.

---

## 4. Leitlinien für das Refactoring

1. **Erst Tests, dann Umbau.** Jeder Befund bekommt einen Regressionstest, der vor dem Fix fehlschlägt. Die Skripte der Nachprüfung sind die Vorlage.
2. **Kleine, einzeln lieferbare Schritte.** Jeder Commit lässt die Prüfungen grün und die Integration lauffähig. Fehlerbehebungen und Strukturumbau werden nicht im selben Commit gemischt.
3. **Vorarbeit übernehmen.** Offene Pull Requests des Original-Repos werden durchgesehen und bei Eignung per `git cherry-pick` übernommen. So bleibt die Urheberschaft erhalten, und der Fork lässt sich später leichter mit dem Original abgleichen.
4. **Probleme der Bibliothek dort lösen.** Was in `FindMy.py` oder `Anisette.py` gehört, wird als Issue oder PR eingebracht. Im Fork gibt es dafür höchstens eine schmale, klar markierte Übergangslösung mit Verweis auf das Issue.
5. **Keine stille Verhaltensänderung.** Änderungen an Entity-IDs, gespeicherten Daten oder Diensten laufen über eine Migration und stehen in den Release-Notes.
6. **Geheimnisse nie im Log, nie unnötig auf der Platte.**
7. **Home Assistants Event-Loop bleibt frei.** Rechenarbeit und Dateizugriffe der Bibliothek laufen außerhalb der Loop. Ein Test misst das.
8. **Kommentare erklären das Warum.** Jedes Modul bekommt einen Docstring mit Zweck und Abgrenzung. Ruff-Regel `D1` wird für öffentliche Namen aktiviert.

---

## 5. Zielarchitektur

```
custom_components/findmy/
├── __init__.py            Setup, Unload, Migration (dünn)
├── const.py               Konstanten, EntryType (StrEnum), Signalnamen
├── hub.py                 FindMyHub: Lebenszyklus, Register der Konten und Geräte
├── worker.py              FindMyWorker: eigener Thread mit eigener asyncio-Loop für alle FindMy-Aufrufe
├── account.py             AccountRuntime: Zustand, Sitzung speichern, Re-Auth, Abfragebudget
├── scheduler.py           wählt Konto und Geräte für den nächsten Abruf, hält das Budget ein
├── key_cache.py           CachedFindMyAccessory: fertige Schlüsselpaare je Index
├── coordinator.py         hält nur noch die Ortungsdaten und benachrichtigt Entities
├── bluetooth/             optional, nur geladen, wenn Bluetooth vorhanden ist
│   ├── manager.py         ein Callback für alle Accessoires, Kandidatenpflege
│   └── matching.py        reine Funktionen (heute local_bluetooth.py)
├── entity.py              FindMyDeviceEntity / FindMyAccountEntity (Basisklassen)
├── device_tracker.py · sensor.py · binary_sensor.py   nur noch Beschreibungen und Darstellung
├── config_flow.py         Flows; Login läuft über den Worker; zentrale Fehlerzuordnung und Redaktion
├── diagnostics.py         Diagnose-Download mit geschwärzten Geheimnissen
└── repairs.py             Hinweise: Konto abgemeldet, Anisette nicht erreichbar, Apple-Störung
```

**Kernentscheidungen:**

- **Ein Worker-Thread für die Bibliothek.** `FindMyWorker` startet einen Thread mit eigener asyncio-Loop. Alle `AsyncAppleAccount`-Objekte werden dort erzeugt und nur dort benutzt (ihre aiohttp-Sitzungen sind an diese Loop gebunden). Home Assistant ruft über `asyncio.run_coroutine_threadsafe` hinein und wartet mit `asyncio.wrap_future`. Damit kann keine Rechenarbeit der Bibliothek die Loop von Home Assistant blockieren: weder Schlüsselableitung noch Entschlüsselung, Anisette-Emulation oder das Parsen von Plists.
- **Keine geteilten veränderlichen Objekte.** Der Worker arbeitet auf eigenen Geräteobjekten (`CachedFindMyAccessory`). Zurück kommen nur Daten: Berichte und die neue Ausrichtung als `(Datum, Index)`. Die Seite von Home Assistant übernimmt sie in der Loop.
- **Zustand pro Konto statt Protokoll.** `AccountRuntime` hält `last_start`, `last_end`, `last_result`, `last_error`, `requests_used` und kennt seinen Config-Entry. Daraus speisen sich die Kontosensoren, Re-Auth und das Budget.
- **`hass.data[DOMAIN]` bleibt für den Hub**, wird aber sauber auf- und abgebaut. Jeder Config-Entry bekommt `entry.runtime_data` mit seinem Konto bzw. Gerät.
- **Bluetooth ist ein optionaler Dienst**, entkoppelt von der Tracker-Entity.

---

## 6. Arbeitspakete

### AP0 Testbasis und CI

| | |
|---|---|
| **Ziel** | Jede weitere Änderung ist automatisch prüfbar. |
| **Vorgehen** | `pytest-homeassistant-custom-component` als Dev-Abhängigkeit (ersetzt die direkte Abhängigkeit auf `homeassistant`, siehe A5). `tests/conftest.py` mit Attrappen für `AsyncAppleAccount` und zufällig erzeugten `FindMyAccessory`/`KeyPair`. Die Skripte der Nachprüfung als Tests übernehmen: Coordinator (R1, R2, R3, R16, R17), Bibliotheksverhalten (S1, R6, R10), Ausrichtung (S5), Blockade-Messung (R20, als Test mit Obergrenze für den längsten Aussetzer). |
| **CI** | `pytest` unter Linux, `hassfest`, HACS-Validierung, Import-Smoke-Test. Den Schritt `pre-commit-ci/lite-action` entfernen. |
| **Windows** | Tests, die Bluetooth importieren, mit Marker versehen und lokal überspringen. Nach AP4 (Bluetooth optional) schrumpft diese Gruppe. |
| **Risiko** | Gering. `pytest-homeassistant-custom-component` legt die HA-Version fest; die Mindestversion in `hacs.json` (R12) danach ausrichten. |

### AP1 Sofort-Fixes

Kleine Änderungen ohne Strukturumbau, jeweils mit Test. Reihenfolge nach Risiko:

| Schritt | Befunde | Vorgehen |
|---|---|---|
| 1 | R2, R1 (Teil), R16 | PR #66 und #64 des Original-Repos durchsehen und übernehmen. Danach ergänzen: `success` erst nach dem `await` setzen; `UnhandledProtocolError`, `aiohttp.ClientError`, `TimeoutError`, `KeyError` und `ValueError` aus der Bibliothek in `UpdateFailed` mit kurzer Meldung übersetzen. Auth-Fehler starten Re-Auth für den Entry **des Kontos** und werfen danach `UpdateFailed`, **nicht** `ConfigEntryAuthFailed`. So laufen die Abfragen der übrigen Konten weiter. |
| 2 | R3 | `coordinator.reload()` am Ende von `async_unload_entry`. |
| 3 | S2 | `_redact()` für alle Flow-Logs; Test prüft die Log-Ausgabe auf Geheimnisse. |
| 4 | S3, R15 | `async_register_admin_service`; `filter` wird Pflichtfeld; `rolling` trifft beide Rolling-Typen; neues Pflichtfeld `confirm`. |
| 5 | R6, R7 | Fehlerzuordnung im Flow (`cannot_connect`, `invalid_file` …); `IndexError` und `KeyError` fangen; `step_id` korrigieren; nach 2FA `LOGGED_IN` prüfen; Re-Auth prüft die `unique_id`. |
| 6 | S6 | `state_class` bei Latitude und Longitude entfernen; Hinweis in den Release-Notes. |
| 7 | R12, R19 | `hacs.json` korrigieren; Blueprint reparieren (Intervall als Auswahl `/5`, `/15`, `/30`, tote Bedingung entfernen, neue Syntax). |

### AP2 Event-Loop entlasten

Behebt R20, mildert R21. Drei Stufen, jede für sich wirksam:

**Stufe 1: Schlüssel zwischenspeichern** (größter Effekt, kleinster Eingriff)

- `key_cache.py` mit `CachedFindMyAccessory(FindMyAccessory)`. Die Klasse überschreibt `keys_at`, `_primary_key_at` und `_secondary_keys_at` und hält fertige `KeyPair`-Objekte je Index, begrenzt auf das Abruffenster plus Reserve. Sekundärschlüssel werden je Sekundärindex nur einmal abgeleitet (9 statt 1.344).
- `warm_keys(start, end)` leitet fehlende Schlüssel **außerhalb der Loop** ab. Im Normalfall ist das ein neuer Index pro 15 Minuten.
- Der Bluetooth-Abgleich nutzt denselben Cache, statt alle 15 Minuten eine JSON-Kopie neu aufzubauen. Damit entfällt auch der Neustart der Kette bei Index 0.
- **Vorarbeit:** PR #69 des Original-Repos setzt genau das um (`key_cache.py`, 179 Zeilen). Durchsehen, mit dem Blockade-Test aus AP0 messen, übernehmen.
- **Erwartung:** Rechenzeit pro Abruf sinkt von rund 2.000 auf wenige Ableitungen, also um über 99 %. Der Rest (Mengenoperationen, Hashes) liegt bei etwa einem Zehntel der heutigen Zeit beim ersten Abruf nach dem Start und nahe null danach.
- **Sauberer Ort ist die Bibliothek** (FindMy.py #285). Sobald dort ein Cache vorhanden ist, entfällt die eigene Klasse.

**Stufe 2: Worker-Thread** (strukturelle Absicherung)

- `worker.py` wie in Kapitel 5. Abruf, Login, 2FA und Datei-Import laufen im Worker.
- Zugriffe sind seriell (eine Loop). Ein Timeout pro Abruf, etwa 120 s, verhindert Hänger.
- Beim Entladen: Konten im Worker schließen, Loop stoppen, Thread beenden.
- **Warum zusätzlich zu Stufe 1:** Der erste Abruf nach einem Neustart, die Entschlüsselung von Berichten, die lokale Anisette-Emulation und künftige Änderungen der Bibliothek bleiben sonst in der Loop.
- **Messwert zur Erwartung:** Rechenarbeit derselben Art in einem Thread verursachte auf dem Mess-PC höchstens 0,04 s Aussetzer in der Loop.

**Stufe 3: Suche begrenzen**

- Obergrenze für die Zahl der Indizes pro Accessoire und Abruf, zum Beispiel 7 Tage (672). Ältere Bereiche werden über mehrere Zyklen nachgeholt oder nach einer einstellbaren Zeit aufgegeben. Das begrenzt sowohl Rechenzeit als auch HTTP-Anfragen für nie geortete Tags.
- Umsetzung in `CachedFindMyAccessory.get_min_index` (nicht tiefer als `max_index − Grenze`).
- Die Ausrichtung häufiger sichern, damit sie nach einem Neustart nicht veraltet ist (zusammen mit R18 gedrosselt).

### AP3 Konten, Sitzungen und Abfragebudget

| Befunde | Vorgehen |
|---|---|
| R1, R10 | `AccountRuntime` mit Zustandsautomat: `ok` → `auth_required` → `ok`. Bei `UnauthorizedError`, `InvalidStateError`, `InvalidCredentialsError` oder `ValueError("No username or password …")`: Zustand setzen, `entry.async_start_reauth(hass)`, Repairs-Hinweis, **keine weiteren Abrufe mit diesem Konto**. Beim Setup: `login_state != LOGGED_IN` → `ConfigEntryAuthFailed`; defekte Daten → `ConfigEntryError`. |
| S1, R5 | Nach jedem Abruf `to_json()` mit dem gespeicherten Stand vergleichen und bei Änderung speichern, dabei `account.password = None`. Migration (Entry-Version 2.1): Passwort aus bestehenden Einträgen entfernen. Das Passwort bleibt nur während des Logins im Speicher des Workers. |
| R4, R21 | `scheduler.py`: Zeitpunkt und Anzahl der Anfragen je Konto in `helpers.storage.Store` sichern. Ein Abruf startet nur, wenn der letzte mindestens 15 Minuten zurückliegt. Zusätzlich ein Budget an Anfragen pro Zyklus; Accessoires, die nicht mehr hineinpassen, kommen im nächsten Zyklus zuerst dran. Refreshes durch Setup, `update_entity` oder Neustart werden auf den nächsten erlaubten Zeitpunkt gelegt. `async_update` aus den Entities entfernen. |
| R22 | Fehlerbilder der Apple-Umstellung gezielt erkennen (`KeyError: 'searchPartyToken'`, Fehlercode `-80045`) und als eigenen Zustand `apple_unsupported` mit Repairs-Hinweis und Link zum Issue anzeigen. In diesem Zustand **kein** automatischer Login-Versuch. |
| R13, R14, R17 | Hub mit sauberem Auf- und Abbau; Flow schließt seine Konten in `async_remove`; Zustand ersetzt das Protokoll. |
| S4 | `anisette_libs_path` im Konfigurationsverzeichnis, SHA-256 der Datei beim ersten Download merken und bei jedem Start prüfen. Wegen R22 erst nach dem Fix der Bibliothek festschreiben, weil sich das Bibliotheksbündel ändern kann. |

### AP4 Struktur und Wartbarkeit

| Befunde | Vorgehen |
|---|---|
| W1 | `EntryType(StrEnum)` und eine Tabelle Typ → Klasse; ersetzt alle Zeichenketten und die vier `if`-Ketten. |
| W2, W3, W9 | `FindMyDeviceEntity(CoordinatorEntity)`; Sensoren als `SensorEntityDescription` mit `value_fn`; `options` und `translation_key` für Enum-Sensoren. |
| W4, R11 | `bluetooth/manager.py` mit **einem** Callback und einem gemeinsamen Lookup für alle Accessoires. Im Manifest von `dependencies` nach `after_dependencies`; Import erst, wenn `"bluetooth" in hass.config.components`. |
| R8, R9 | `unique_id` für Geräte-Einträge; Kontogerät über Hash der E-Mail. Migration der Geräte-Registry. |
| R18 | Ein zentraler, gedrosselter Speicherpunkt für Ausrichtungen. |
| W5–W8 | Toten Code entfernen, Doku angleichen, Docstrings, Übersetzungen (Deutsch). |
| W10 | Entity-IDs von Home Assistant erzeugen lassen. **Nur mit Migration**, sonst ändern sich die IDs bestehender Installationen (siehe Kapitel 10). |

### AP5 Härtung und Datenschutz

| Befunde | Vorgehen |
|---|---|
| S5 | Realignment-Aufträge entprellen: höchstens einer pro Accessoire und Minute, auch wenn die Bibliothek die Ausrichtung ablehnt. Ausrichtung aus Bluetooth nur übernehmen, wenn der Index nicht hinter der Erwartung aus der letzten bestätigten Ortung zurückliegt. Hinweis in README und Entity-Beschreibung: keine Sicherheitsfunktion. |
| S7 | E-Mail maskieren; neutrale Gerätenamen. |
| neu | `diagnostics.py` mit `async_redact_data`; `repairs.py`. |

### AP6 Abhängigkeiten

Siehe [Kapitel 8](#8-abhängigkeiten).

---

## 7. Strategie je Befund

| ID | Paket | Kern der Lösung | Migration nötig | Vorarbeit |
|---|---|---|---|---|
| S1 | AP3 | Passwort nicht speichern; Sitzung laufend sichern | ja (Passwort entfernen) | – |
| S2 | AP1 | Zentrale Redaktion der Logs | nein | – |
| S3 | AP1 | Admin-Dienst, Pflichtfelder | nein (Dienstaufrufe ohne `filter` brechen; Release-Note) | – |
| S4 | AP3 | Lokaler Pfad und Hashprüfung | nein | Upstream-Vorschlag |
| S5 | AP5 | Entprellen, Plausibilitätsprüfung, Doku | nein | – |
| S6 | AP1 | `state_class` entfernen | nein (alte Statistiken manuell löschen) | – |
| S7 | AP5 | Maskierung | optional (Gerätenamen) | – |
| R1 | AP1 → AP3 | Re-Auth pro Konto; Abfragen laufen weiter | nein | PR #66 |
| R2 | AP1 | `success` korrekt; Fehler → `UpdateFailed` | nein | PR #66 |
| R3 | AP1 | `reload()` nach Unload | nein | – |
| R4 | AP3 | Scheduler mit gespeichertem Zeitpunkt | nein | – |
| R5 | AP3 | Sitzung nach Änderung speichern | nein | – |
| R6 | AP1 | Fehlerzuordnung im Flow | nein | – |
| R7 | AP1 | `_abort_if_unique_id_mismatch` | nein | – |
| R8 | AP4 | `unique_id` für Geräte | ja | – |
| R9 | AP4 | Identifier aus voller E-Mail (Hash) | ja (Geräte-Registry) | – |
| R10 | AP3 | Validierung beim Setup | nein | – |
| R11 | AP4 | Bluetooth optional | nein | – |
| R12 | AP1 | Mindestversion korrigieren | nein | – |
| R13 | AP3 | Hub mit Lebenszyklus | nein | – |
| R14 | AP3 | Flow räumt auf | nein | – |
| R15 | AP1 | Filter korrigieren | nein | – |
| R16 | AP1 | Formel korrigieren | nein | PR #64 |
| R17 | AP3 | Zustand statt Protokoll | nein | – |
| R18 | AP4 | Gedrosseltes Speichern | nein | – |
| R19 | AP1 | Blueprint reparieren | nein (Nutzer importieren neu) | – |
| R20 | AP2 | Schlüssel-Cache, Worker-Thread, Suchgrenze | nein | PR #69, FindMy.py #285 |
| R21 | AP2, AP3 | Suchgrenze, Budget pro Konto | nein | PR #69 |
| R22 | AP1, AP3 | Fehler sichtbar machen; kein Auto-Login; Fix der Bibliothek abwarten | nein | FindMy.py PR #279 |
| W1–W10 | AP4 | siehe AP4 | nur W10 | – |

---

## 8. Abhängigkeiten

### 8.1 Was die Integration wirklich braucht

Geprüft per Test: `findmy` wurde importiert, während einzelne Pakete blockiert waren.

| Paket | Kommt von | Beim Import nötig? | Wofür tatsächlich | Schon in Home Assistant? | Letzte Version |
|---|---|---|---|---|---|
| `FindMy` 0.10.2 | Manifest | ja | alles | nein | 14.09.2026 (aktuell) |
| `anisette` 1.2.4 | FindMy | **ja**, auch mit Anisette-Server | lokale Anisette | nein | 09.10.2025 |
| `unicorn` 2.1.4 (58 MB) | anisette | ja | CPU-Emulator für die lokale Anisette | nein | 09.09.2025 |
| `pyelftools` | anisette | ja | ELF-Dateien lesen | nein | 29.05.2026 |
| `fs` 2.4.16 | anisette | **nein, nie importiert** | nichts (Altlast in den Metadaten) | nein | 02.05.2022 |
| `appdirs` 1.4.4 | fs | nein | nichts | nein | 11.05.2020 |
| `srp` 1.0.22 | FindMy | ja | SRP-Login bei Apple (nutzt das private Modul `srp._pysrp`) | nein | 01.11.2024 |
| `six` | srp, fs | ja (wegen `srp`) | Python-2-Kompatibilität in `srp` | nein | 04.12.2024 |
| `beautifulsoup4`, `soupsieve` | FindMy | ja | **eine** Stelle: `<script class="boot_args">` aus der 2FA-Seite lesen | von der Kern-Integration `scrape` verlangt, in den offiziellen Images daher in der Regel vorhanden | 07.06.2026 |
| `bleak` | FindMy | ja | nur für den eigenen Scanner der Bibliothek, den die Integration nicht nutzt | ja (`bluetooth`), dort fest auf 3.0.2 | – |
| `cryptography`, `aiohttp`, `certifi`, `urllib3`, `typing-extensions` | FindMy, anisette | ja | – | ja (Kern) | – |

### 8.2 Korrektur und tatsächliche Risiken

- **`fs` ist kein Ausfallrisiko.** `anisette` 1.2.4 hat ein eigenes virtuelles Dateisystem auf Basis von `tarfile`. `fs` und `appdirs` werden nur noch installiert, aber nicht geladen. Home Assistant begrenzt zwar `setuptools<82`, weil neuere Versionen `pkg_resources` nicht mehr enthalten. Das betrifft die Integration aber nicht, solange niemand `fs` importiert.
- **Echtes Risiko: `bleak`.** `FindMy` verlangt `bleak>=3.0.2,<3.1.0`. Home Assistant legt `bleak==3.0.2` fest. Hebt Home Assistant auf 3.1 an, lässt sich `FindMy==0.10.2` nicht mehr installieren, und die Integration lädt nicht, obwohl sie den Scanner der Bibliothek gar nicht benutzt.
- **Echtes Risiko: Bindung an `anisette`.** Die Bibliothek wird auch mit Anisette-Server importiert. Jede Störung dort trifft alle Nutzer, und die 58 MB von `unicorn` müssen auf jedem Gerät installiert werden. Mit R22 steht hier ohnehin ein Umbau an.
- **Lockfile gegen die Vorgaben von Home Assistant.** `uv.lock` enthielt `mashumaro` 3.16, obwohl Home Assistant in `package_constraints.txt` mindestens 3.17 verlangt. Diese Vorgaben stehen nicht in den Paket-Metadaten, deshalb beachtet `uv` sie nicht von selbst.

### 8.3 Was im Fork sofort geht

| # | Maßnahme | Wirkung | Aufwand |
|---|---|---|---|
| A1 | Bluetooth von `dependencies` nach `after_dependencies`, Code verzögert laden (AP4) | Bluetooth und `usb` werden nicht mehr erzwungen; Tests ohne Bluetooth möglich | mittel |
| A5 | `homeassistant` aus `[project].dependencies` entfernen; `pytest-homeassistant-custom-component` in die Dev-Gruppe | Die Testbibliothek legt Home Assistant samt passender Abhängigkeiten fest und bringt Fixtures mit | klein |
| A6 | `findmy==0.10.2` in `pyproject.toml`, passend zum Manifest; CI-Prüfung auf Gleichheit | keine Abweichung zwischen Entwicklung und Nutzern | klein |
| A8 | Vorgaben von Home Assistant im Lockfile erzwingen: `[tool.uv] constraint-dependencies` aus `homeassistant/package_constraints.txt` erzeugen (Skript), oder sich auf A5 stützen | Fehler wie `mashumaro` 3.16 treten nicht mehr auf | klein |
| A9 | `"loggers": ["findmy", "anisette"]` und `"codeowners"` im Manifest ergänzen | Log-Stufen der Bibliothek lassen sich in Home Assistant einstellen | trivial |

Im Fork **nicht** möglich: einzelne Pakete von `FindMy` weglassen. Home Assistant installiert die Anforderung aus dem Manifest mit allen Abhängigkeiten, und einen Ausschluss gibt es nicht.

### 8.4 Vorschläge an die Original-Repos

Geordnet nach Verhältnis von Nutzen zu Aufwand:

| # | Wo | Vorschlag | Einsparung für Nutzer | Aufwand | Beleg |
|---|---|---|---|---|---|
| U1 | `Anisette.py` | `fs` aus `dependencies` streichen | `fs`, `appdirs` (und `six` von dieser Seite) | eine Zeile | Import-Test: läuft ohne `fs` |
| U2 | `FindMy.py` | `bleak` optional machen (Extra `scanner`), Import in `findmy/__init__.py` verzögern. Die Klassen `OfflineFindingDevice` & Co. in ein Modul ohne `bleak`-Import legen | löst die Kopplung an Home Assistants `bleak`-Version | klein | `bleak` nur in `scanner/scanner.py` |
| U3 | `FindMy.py` | `anisette` optional machen (Extra `local-anisette`), Import erst in `LocalAnisetteProvider` | 58 MB `unicorn`, `pyelftools`, `anisette` für alle mit Anisette-Server | klein, aber mit R22/PR #279 abstimmen | Import auf Modulebene (`reports/anisette.py:17`) |
| U4 | `FindMy.py` | `beautifulsoup4` durch `html.parser` aus der Standardbibliothek ersetzen (etwa 15 Zeilen) | `beautifulsoup4`, `soupsieve` | klein | eine Verwendung (`account.py:134`) |
| U5 | `FindMy.py` | Fertige Schlüsselpaare cachen und Sekundärschlüssel nicht mehrfach ableiten (#285) | behebt R20 an der Wurzel | mittel | Profil in Kapitel 3 |
| U6 | `Anisette.py` | Download mit Timeout, Statusprüfung und optionalem Hash | S4 | klein | `_util.py:44-50` |
| U7 | `FindMy.py` | `srp` ersetzen, siehe 8.5 | `srp`, `six` | mittel | Machbarkeitstest |

### 8.5 Modernere oder verbreitetere Pakete

| Heute | Kandidat | Bewertung |
|---|---|---|
| `srp` (pysrp; braucht `six`, benutzt wird ein privates Modul) | **`srptools` 2.0.0** (26.10.2026, reines Python, keine Abhängigkeiten, ≥ 3.10). Bereits in Home Assistant vorhanden, weil die Integration `apple_tv` es über `pyatv` nutzt. | **Machbar, aber kein direkter Austausch.** Im Test stimmten der öffentliche Wert `A` und der Sitzungsschlüssel mit `pysrp` überein, wenn die Passwort-Hash-Funktion für Apples Variante (ohne Benutzername) überschrieben wird. Der Nachweis `M1` wich noch ab; er wird anders zusammengesetzt und müsste ebenfalls überschrieben werden. Aufwand: etwa 30 Zeilen plus Test gegen Apple. Wegen R22 derzeit nicht sinnvoll testbar. Nutzen: zwei Pakete weniger. Niedrige Priorität. |
| `beautifulsoup4` | `html.parser` (Standardbibliothek) | Klarer Gewinn (U4). |
| `fs` | – | Ersatzlos streichen (U1). |
| `appdirs` | `platformdirs` wäre der gepflegte Nachfolger | Erledigt sich mit U1. |
| `unicorn` | Keine Alternative für die Emulation. Ausweg ist ein Anisette-Server (dann U3) oder das „mac backend“ aus PR #279. | Abwarten, was der Fix für R22 bringt. |
| `bleak` | – | Ist der Standard und in Home Assistant gesetzt; nur optional machen (U2). |
| `basedpyright` | `mypy` (nutzt der Kern von Home Assistant) oder `ty` (Astral) | Beibehalten. `basedpyright` ist strenger, läuft schnell und passt zur bestehenden Konfiguration. Ein Wechsel brächte Arbeit ohne Gewinn. |
| `pre-commit` | `prek` (schnellere Neuimplementierung) | Beibehalten. Die drei lokalen Hooks sind in Sekunden durch. |
| `ruff` | – | Ist bereits der Standard. |

### 8.6 Empfehlung

1. **Jetzt im Fork:** A5, A6, A8, A9 in AP0; A1 in AP4.
2. **Jetzt beim Original-Repo:** U1 (eine Zeile) und U5 (mit den Messwerten aus Kapitel 3) einreichen.
3. **Nach dem Fix für R22:** U2, U3, U4, U6 einreichen, abgestimmt auf die dann gültige Struktur von `anisette`.
4. **Später, bei Bedarf:** U7.
5. **Kein eigener Fork der Bibliothek.** Das Protokoll von Apple ändert sich laufend, wie R22 zeigt. Ein Fork wäre nach wenigen Wochen veraltet.

---

## 9. Reihenfolge und Meilensteine

```
AP0 Testbasis ──► AP1 Sofort-Fixes ──► AP2 Stufe 1 (Schlüssel-Cache) ──► AP2 Stufe 2 (Worker) ─┐
                        │                                                                      ├─► AP4 Struktur ──► AP5 Härtung
                        └────────────────────────────────────────────► AP3 Konten/Budget ◄─────┘
AP6: A5/A6/A8/A9 in AP0 · U1/U5 parallel einreichen · A1 in AP4 · U2–U4/U6 nach dem Fix für R22
```

| Meilenstein | Inhalt | Ergebnis für Nutzer |
|---|---|---|
| **M1** | AP0, AP1 | Fehler werden korrekt angezeigt; Re-Auth startet; ein defektes Konto bremst die anderen nicht; Löschdienst abgesichert; keine Geheimnisse im Log |
| **M2** | AP2 Stufe 1 und 3 | Home Assistant bleibt während der Abrufe bedienbar; weniger Anfragen bei alten Tags |
| **M3** | AP2 Stufe 2, AP3 | Passwort nicht mehr gespeichert; Abfragegrenze wird eingehalten und überlebt Neustarts; Sitzungen bleiben erhalten |
| **M4** | AP4 | Bluetooth optional; keine doppelten Geräte; wartbarer Aufbau |
| **M5** | AP5 | Diagnose-Download, Repairs-Hinweise, Datenschutz |

**Abhängigkeiten zwischen den Paketen:**
- AP2 Stufe 2 und AP3 teilen sich `worker.py` und `account.py`; sie werden zusammen entworfen und nacheinander geliefert.
- AP4 (Strukturumbau) kommt nach AP1 bis AP3, damit Fehlerbehebungen nicht in großen Umbau-Commits untergehen.
- Die Übernahme von PR #66 und #69 gehört an den Anfang von AP1 bzw. AP2, **vor** jede Umstrukturierung der betroffenen Dateien. Danach lassen sie sich nicht mehr sauber übernehmen.

**Fertig ist ein Paket, wenn:** Tests für alle abgedeckten Befunde vorhanden und grün sind, `pre-commit` grün ist, die Migration (falls nötig) getestet ist, README und Review-Dokument den neuen Stand zeigen.

---

## 10. Offene Entscheidungen

| # | Frage | Empfehlung | Folge der Alternative |
|---|---|---|---|
| E1 | Passwort der Apple-ID weiter speichern? | **Nein**, höchstens als ausdrücklich gewählte Option. | Mit Passwort: stille Neuanmeldung bleibt möglich, das Passwort bleibt aber im Klartext in jedem Backup. |
| E2 | Offene PRs des Original-Repos übernehmen oder selbst schreiben? | **Übernehmen** nach Durchsicht (#64, #66, #69). | Selbst schreiben: mehr Aufwand, kein Abgleich mit dem Original mehr möglich. |
| E3 | Entity-IDs auf das Standardschema von Home Assistant umstellen (W10)? | **Vorerst nein.** Erst wenn eine Migration die alten IDs erhält. | Umstellung ohne Migration bricht Automationen und Dashboards der Nutzer. |
| E4 | Auf den Fix für die Apple-Umstellung warten? | **Nicht warten.** M1 und M2 hängen nicht davon ab und verbessern gerade das Verhalten in diesem Störfall. Nur S4 und die Vorschläge U2–U4/U6 danach. | Warten verzögert die Behebung der Blockade und der Sicherheitslücken. |
| E5 | Löschdienst `delete_devices` behalten? | Behalten, aber abgesichert (AP1). | Entfernen wäre einfacher, nimmt aber eine vorhandene Funktion weg. |
| E6 | Soll der Fork Änderungen an das Original-Repo zurückgeben? | Ja, für Fehlerbehebungen als einzelne PRs. Das hält den Fork klein. | Ohne Rückgabe wächst der Abstand zum Original mit jeder Änderung. |
