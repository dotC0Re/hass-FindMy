# Refactoring strategy hass-FindMy

| | |
|---|---|
| **As of** | 2026-11-18, commit `5284e8a` |
| **Reference** | Builds on the [code review](CODE_REVIEW.md). The finding IDs (S1, R1 …) come from there. |
| **Contents** | Verification of all findings, new findings (event loop blocking, Apple change), strategy for fixing each finding, thorough analysis of the dependencies |
| **Measurement environment** | Windows 11, x86-64 desktop, Python 3.14.10, Home Assistant 2026.9.3, FindMy 0.10.2, anisette 1.2.4. On ARM devices (Raspberry Pi, Home Assistant Green) the measured computation times are, by experience, several times higher. |

## Contents

1. [Short summary](#1-short-summary)
2. [Result of the verification](#2-result-of-the-verification)
3. [New findings](#3-new-findings)
4. [Guidelines for the refactoring](#4-guidelines-for-the-refactoring)
5. [Target architecture](#5-target-architecture)
6. [Work packages](#6-work-packages)
7. [Strategy per finding](#7-strategy-per-finding)
8. [Dependencies](#8-dependencies)
9. [Order and milestones](#9-order-and-milestones)
10. [Open decisions](#10-open-decisions)

---

## 1. Short summary

- **Verification:** All 26 findings were checked again, 9 of them with test scripts against the real code, the rest again against the source text. 23 are confirmed, three of them with additions. **R1 is more serious** than described: A single expired account stops the polling for **all** accounts. **S5 and R11 are weaker** than described, and one statement about the dependencies was **wrong**: The package `fs` is not imported anywhere, the feared failure risk does not exist.
- **Blocking of Home Assistant (new, R20):** Confirmed and measured. Every fetch computes around 2,000 elliptic-curve keys per AirTag **directly in the event loop**. On the measurement PC that is 0.9 s per AirTag and fetch, 3.9 s for a tag that has not been located for a long time. On ARM and with several tags this becomes the 30 to 90 seconds reported in the original repo. The solution has two stages: cache derived keys (saves over 99 % of the computation time) and move all FindMy work into a thread of its own.
- **Apple change (new, R22):** According to the original repo of the library, since about 2026-10-12 Apple no longer delivers a fetch token for the emulated Anisette. The maintainer confirmed on 2026-10-28 that a fix is still pending. New sign-ins are therefore expected to fail. I did not verify this myself, because that would require an Apple account. For the refactoring this means: Tests must work with mocks of the library, and the integration must show this state clearly instead of concealing it.
- **Dependencies:** In the fork itself only little can be removed, because almost everything hangs on `FindMy`. Feasible immediately: make Bluetooth optional, remove `homeassistant` from the runtime dependencies, unify the version specifications, enforce the constraints of Home Assistant in the lockfile. The big lever lies in three small proposals to the original repo: remove `fs` from `anisette` (unused), make `anisette` and `bleak` optional in `FindMy`, replace `beautifulsoup4` with the standard library.
- **Existing prior work:** The original repo has three open pull requests that fix parts of the findings (#64 for R16, #66 for R1/R2, #69 for R20). After a review they should be adopted instead of rewriting the same thing.

---

## 2. Result of the verification

**Methods:** *Test* = script against the real code of the integration or the library · *Source* = retraced again in the source text (integration, Home Assistant, FindMy) · *Measurement* = runtime measured

| ID | Result | Method | Note |
|---|---|---|---|
| S1 | ✅ confirmed | Test | `to_json()` contains the password in plain text. Addition: Without a stored password the silent re-sign-in raises `ValueError("No username or password specified")`, not `UnauthorizedError`. The strategy has to catch this. |
| S2 | ✅ confirmed | Source | – |
| S3 | ✅ confirmed | Source | The WebSocket command `call_service` does not require admin rights. Only `async_register_admin_service` checks them. |
| S4 | ✅ confirmed | Source | The download happens on first use after every start (`fs_fallback`), not already on loading. |
| S5 | ⚠️ **weakened** | Test | The library ignores alignments that go backwards. A replay only has an effect if the stored alignment is **older** than the replayed signal. The consequence is then a permanent lag of the location tracking by the age of the signal (in the test: 2 h) until a genuine signal corrects it. Faking presence remains; with Bluetooth presence it is fundamentally unavoidable. New severity: 🟡 for the alignment, 🟠 for the presence (duty to document). |
| S6 | ✅ confirmed | Source | – |
| S7 | ✅ confirmed | Source | – |
| R1 | 🔺 **aggravated** | Test | `config_entry` is `None`, no re-auth flow starts. **In addition** the coordinator schedules **no further fetch** after `ConfigEntryAuthFailed` (`update_coordinator.py:585`). One expired account thus ends the polling of all accounts until the next restart or manual refresh. |
| R2 | ✅ confirmed, refined | Test | `UnhandledProtocolError` and `TimeoutError` are logged as `SUCCESS`. `EmptyResponseError` does not reach the coordinator (the library catches it). Newly added: `KeyError: 'searchPartyToken'` after the Apple change (R22) also falls into this gap. |
| R3 | ✅ confirmed | Test | The interval stays at 7:30 min after unloading one of two accounts. |
| R4 | ✅ confirmed, extended | Source, measurement | A "fetch" is in truth many HTTP requests, see R21. |
| R5 | ✅ confirmed | Source | – |
| R6 | ✅ confirmed, extended | Test | In addition: A valid JSON file without a `type` field raises `KeyError` and aborts the flow. Network errors arrive as `aiohttp.ClientError` or `TimeoutError`. The path with the wrong `step_id` is only reachable with an empty code. |
| R7 | ✅ confirmed | Source | – |
| R8 | ✅ confirmed | Source | – |
| R9 | ✅ confirmed | Source | – |
| R10 | ✅ confirmed | Test | Corrupt data leads to `ValueError` for all three types. |
| R11 | ⚠️ **weakened** | Source | `bluetooth.async_setup` ends regularly with `return True`; I did not find an abort there because of missing adapters. A total failure is therefore presumably rare. What remains: Bluetooth (together with `usb`) is forced on every user, and tests do not run on Windows. New severity: 🟡. |
| R12 | ✅ confirmed | Source | Home Assistant 2026.9.3 requires Python ≥ 3.14.2; the syntax `except A, B:` requires 3.14. |
| R13 | ✅ confirmed | Source | – |
| R14 | ✅ confirmed | Source | – |
| R15 | ✅ confirmed | Source | – |
| R16 | ✅ confirmed | Test | With 3 accounts the sensor shows 45 min instead of 15 min. In the original repo PR #64 exists for this. |
| R17 | ✅ confirmed | Test | 2 entries per fetch, never trimmed. |
| R18 | ✅ confirmed | Source | `async_update_entry` only writes if the data changed. The alignment, however, changes on every fetch with a match (`reports.py:457`). |
| R19 | ✅ confirmed | Source | `time_pattern` evaluates only strings with `/` as "every n minutes". |
| W3 | ✅ confirmed (not a bug) | Test | The `cached_property` override works because Home Assistant clears the cache. |
| Dep. | ❌ **statement corrected** | Test | `fs` is imported neither by `anisette` nor by `FindMy`. Both also load when `fs`, `appdirs` and `pkg_resources` are blocked. The warning in the review that the integration would fail because of `pkg_resources` was wrong. See chapter 8. |

The scripts of the verification are not in the repository. They go into work package AP0 as regression tests in `tests/`.

---

## 3. New findings

### R20 The fetch blocks the event loop of Home Assistant

🔴 High · ✅ verified (measurement, profile) · [`coordinator.py:216`](../custom_components/findmy/coordinator.py#L216), FindMy `reports/reports.py:419-507`, `accessory.py:403`, `:674`

**Problem:** `account.fetch_location()` is a coroutine, but it computes **synchronously** between the network accesses. For every accessory with rolling keys the library walks through all key indices of the last 7 days (673 of them) and regenerates three key pairs per index. Control is released only about every 290 indices, before an HTTP request.

**Measurement** (one accessory, network replaced by a mock that answers immediately, i.e. pure computation time):

| Scenario | Duration | longest single block |
|---|---|---|
| Tag, last located 1 h ago | 0.9 s | 0.5–0.9 s |
| same tag, second fetch (caches warm) | 0.9 s | 0.5 s |
| Tag, not located for 30 days | 3.9 s | 1.2 s |
| 5-year-old tag, first fetch after restart | 1.15 s | 0.6 s |

- Practically the whole duration is blocking: The sum of the stalls over 100 ms equals the total duration in each case.
- **Profile:** 89 % of the time is spent in `ec.derive_private_key`, 2,016 calls per accessory and fetch. Two thirds of these are **repetitions**: The two secondary keys are the same for 96 consecutive indices, but are derived anew every time (1,344 derivations for about 9 distinct keys).
- Warm caches do not help, because the library only caches the chain of secrets, not the finished key pairs. Between two fetches, however, only **one** index out of 673 changes.
- The time grows linearly with the number of accessories and with the age of the alignment. A tag that has never been located searches **all** indices since pairing.

**Extrapolation:** 9 accessories result in at least 8 s of blocking per fetch on the measurement PC. On an ARM device or a VM this matches the 34 to 94 seconds reported in the original repo (hass-FindMy #70, 9 accessories).

**Second, weaker source:** The candidates for the local Bluetooth matching are rebuilt from a JSON copy every 15 minutes per accessory ([`device_tracker.py:187-199`](../custom_components/findmy/device_tracker.py#L187-L199), [`local_bluetooth.py:90-95`](../custom_components/findmy/local_bluetooth.py#L90-L95)). This runs in the executor and does not block the loop directly. On the measurement PC it took 0.3 to 0.5 s with stalls of at most 0.04 s. A fresh copy, however, always starts the key chain at index 0. On slow devices this noticeably competes with the loop for computation time (hass-FindMy #67 reports several seconds).

**Fix:** see [AP2](#ap2-relieving-the-event-loop).

### R21 One fetch is many HTTP requests

🟠 Medium · ✅ verified (measurement, source) · FindMy `reports/reports.py:400-413`, `:476-487`

**Problem:** The README speaks of one query per 15 minutes per account. In fact the library makes per cycle:
- **one** request for all static keys together,
- **per accessory with rolling keys** one to three requests: one if there is a report in the most recent roughly 72 h, three if there is none within 7 days (measured),
- correspondingly more with an old alignment, about one request per 290 indices. For a never-located, 2.7-year-old tag PR #69 cites around 330 requests per fetch; this matches the formula (95,000 indices / 290).

The requests follow each other directly. Ten AirTags therefore mean 10 to 30 requests within a few seconds, and that every 15 minutes per account.

**Impact:** The actual risk of a lock depends on the number of requests, not on the number of cycles. R3 and R4 therefore weigh more heavily.

**Fix:** see [AP3](#ap3-accounts-sessions-and-request-budget) (budget per account) and [AP2](#ap2-relieving-the-event-loop) (upper limit for the index search).

### R22 Apple change in October 2026: sign-in and fetch disrupted

🔴 High · 🌐 reported externally, **not verified myself** · FindMy.py #278, PR #279; hass-FindMy #65

**Situation according to the original repos:**
- Since about 2026-10-12 Apple requires the client identifier to match the platform from which the Anisette identity originates. The library `anisette` emulates an Android library.
- Android and Windows identities no longer receive a `searchPartyToken`. Without this token the fetch answers with HTTP 401.
- The maintainer wrote on 2026-10-28 that it still does not work and that a fix in `Anisette.py` and `FindMy.py` is necessary. PR #279 ("anisette mac backend") is open, last changed on 2026-11-05. The latest published version is still 0.10.2 from 2026-09-14.
- Users of the integration report "Invalid username or password" at sign-in.

**Meaning for the refactoring:**
1. The core function (fetching via the Find My network) is expected to be disrupted for new sign-ins. Whether existing sessions keep working is open.
2. A test against Apple is currently not reliably possible. All tests must work with mocks (AP0).
3. The failure patterns of this disruption hit exactly the gaps R1 and R2: `KeyError: 'searchPartyToken'` is logged as a success, and a refused re-sign-in leads to no message. **AP1 fixes this first.**
4. Automatic re-sign-ins with a stored password are dangerous in this state: Every cycle triggers another failing login. This supports the recommendation on S1 not to store the password.
5. The dependencies around `anisette` are expected to change with the fix. Proposals to the original repo (chapter 8) should be coordinated accordingly.

---

## 4. Guidelines for the refactoring

1. **Tests first, then restructuring.** Every finding gets a regression test that fails before the fix. The scripts of the verification are the template.
2. **Small, individually deliverable steps.** Every commit leaves the checks green and the integration runnable. Bug fixes and structural restructuring are not mixed in the same commit.
3. **Adopt prior work.** Open pull requests of the original repo are reviewed and, where suitable, adopted via `git cherry-pick`. This preserves authorship, and the fork can later be reconciled with the original more easily.
4. **Solve problems of the library there.** What belongs in `FindMy.py` or `Anisette.py` is submitted as an issue or PR. In the fork there is at most a slim, clearly marked interim solution with a reference to the issue.
5. **No silent change of behaviour.** Changes to entity IDs, stored data or services go through a migration and are listed in the release notes.
6. **Secrets never in the log, never unnecessarily on disk.**
7. **The event loop of Home Assistant stays free.** Computation and file accesses of the library run outside the loop. A test measures this.
8. **Comments explain the why.** Every module gets a docstring with its purpose and scope. Ruff rule `D1` is enabled for public names.

---

## 5. Target architecture

```
custom_components/findmy/
├── __init__.py            Setup, unload, migration (thin)
├── const.py               Constants, EntryType (StrEnum), signal names
├── hub.py                 FindMyHub: lifecycle, registry of accounts and devices
├── worker.py              FindMyWorker: separate thread with its own asyncio loop for all FindMy calls
├── account.py             AccountRuntime: state, save session, re-auth, request budget
├── scheduler.py           picks account and devices for the next fetch, keeps to the budget
├── key_cache.py           CachedFindMyAccessory: finished key pairs per index
├── coordinator.py         now only holds the location data and notifies entities
├── bluetooth/             optional, loaded only if Bluetooth is present
│   ├── manager.py         one callback for all accessories, candidate maintenance
│   └── matching.py        pure functions (today local_bluetooth.py)
├── entity.py              FindMyDeviceEntity / FindMyAccountEntity (base classes)
├── device_tracker.py · sensor.py · binary_sensor.py   now only descriptions and presentation
├── config_flow.py         Flows; login runs via the worker; central error mapping and redaction
├── diagnostics.py         Diagnostics download with redacted secrets
└── repairs.py             Notices: account signed out, Anisette unreachable, Apple disruption
```

**Key decisions:**

- **One worker thread for the library.** `FindMyWorker` starts a thread with its own asyncio loop. All `AsyncAppleAccount` objects are created there and used only there (their aiohttp sessions are bound to this loop). Home Assistant calls in via `asyncio.run_coroutine_threadsafe` and waits with `asyncio.wrap_future`. This way no computation of the library can block the loop of Home Assistant: neither key derivation nor decryption, Anisette emulation or the parsing of plists.
- **No shared mutable objects.** The worker operates on device objects of its own (`CachedFindMyAccessory`). Only data comes back: reports and the new alignment as `(date, index)`. The Home Assistant side adopts them in the loop.
- **State per account instead of a log.** `AccountRuntime` holds `last_start`, `last_end`, `last_result`, `last_error`, `requests_used` and knows its config entry. The account sensors, re-auth and the budget draw on it.
- **`hass.data[DOMAIN]` remains for the hub**, but is set up and torn down cleanly. Every config entry gets `entry.runtime_data` with its account or device.
- **Bluetooth is an optional service**, decoupled from the tracker entity.

---

## 6. Work packages

### AP0 Test base and CI

| | |
|---|---|
| **Goal** | Every further change can be checked automatically. |
| **Approach** | `pytest-homeassistant-custom-component` as a dev dependency (replaces the direct dependency on `homeassistant`, see A5). `tests/conftest.py` with mocks for `AsyncAppleAccount` and randomly generated `FindMyAccessory`/`KeyPair`. Adopt the scripts of the verification as tests: coordinator (R1, R2, R3, R16, R17), library behaviour (S1, R6, R10), alignment (S5), blocking measurement (R20, as a test with an upper limit for the longest stall). |
| **CI** | `pytest` on Linux, `hassfest`, HACS validation, import smoke test. Remove the step `pre-commit-ci/lite-action`. |
| **Windows** | Mark tests that import Bluetooth with a marker and skip them locally. After AP4 (Bluetooth optional) this group shrinks. |
| **Risk** | Low. `pytest-homeassistant-custom-component` fixes the HA version; align the minimum version in `hacs.json` (R12) with it afterwards. |

### AP1 Immediate fixes

Small changes without structural restructuring, each with a test. Order by risk:

| Step | Findings | Approach |
|---|---|---|
| 1 | R2, R1 (part), R16 | Review and adopt PR #66 and #64 of the original repo. Then add: set `success` only after the `await`; translate `UnhandledProtocolError`, `aiohttp.ClientError`, `TimeoutError`, `KeyError` and `ValueError` from the library into `UpdateFailed` with a short message. Auth errors start re-auth for the entry **of the account** and then raise `UpdateFailed`, **not** `ConfigEntryAuthFailed`. This way the polling of the other accounts keeps running. |
| 2 | R3 | `coordinator.reload()` at the end of `async_unload_entry`. |
| 3 | S2 | `_redact()` for all flow logs; a test checks the log output for secrets. |
| 4 | S3, R15 | `async_register_admin_service`; `filter` becomes a required field; `rolling` matches both rolling types; new required field `confirm`. |
| 5 | R6, R7 | Error mapping in the flow (`cannot_connect`, `invalid_file` …); catch `IndexError` and `KeyError`; fix `step_id`; check `LOGGED_IN` after 2FA; re-auth checks the `unique_id`. |
| 6 | S6 | Remove `state_class` from latitude and longitude; note in the release notes. |
| 7 | R12, R19 | Fix `hacs.json`; repair the blueprint (interval as a selection `/5`, `/15`, `/30`, remove the dead condition, new syntax). |

### AP2 Relieving the event loop

Fixes R20, mitigates R21. Three stages, each effective on its own:

**Stage 1: Cache keys** (biggest effect, smallest intervention)

- `key_cache.py` with `CachedFindMyAccessory(FindMyAccessory)`. The class overrides `keys_at`, `_primary_key_at` and `_secondary_keys_at` and holds finished `KeyPair` objects per index, limited to the fetch window plus a reserve. Secondary keys are derived only once per secondary index (9 instead of 1,344).
- `warm_keys(start, end)` derives missing keys **outside the loop**. In the normal case this is one new index per 15 minutes.
- The Bluetooth matching uses the same cache instead of rebuilding a JSON copy every 15 minutes. This also eliminates the restart of the chain at index 0.
- **Prior work:** PR #69 of the original repo implements exactly this (`key_cache.py`, 179 lines). Review it, measure it with the blocking test from AP0, adopt it.
- **Expectation:** Computation time per fetch drops from around 2,000 to a few derivations, i.e. by over 99 %. The rest (set operations, hashes) is at about a tenth of today's time on the first fetch after startup and close to zero afterwards.
- **The clean place is the library** (FindMy.py #285). Once a cache exists there, the own class is no longer needed.

**Stage 2: Worker thread** (structural safeguard)

- `worker.py` as in chapter 5. Fetch, login, 2FA and file import run in the worker.
- Accesses are serial (one loop). A timeout per fetch, about 120 s, prevents hangs.
- On unloading: close accounts in the worker, stop the loop, end the thread.
- **Why in addition to stage 1:** The first fetch after a restart, the decryption of reports, the local Anisette emulation and future changes of the library would otherwise remain in the loop.
- **Measured value for the expectation:** Computation of the same kind in a thread caused at most 0.04 s of stalls in the loop on the measurement PC.

**Stage 3: Limit the search**

- Upper limit for the number of indices per accessory and fetch, for example 7 days (672). Older ranges are caught up over several cycles or given up after a configurable time. This limits both computation time and HTTP requests for never-located tags.
- Implementation in `CachedFindMyAccessory.get_min_index` (not deeper than `max_index − limit`).
- Save the alignment more often so that it is not outdated after a restart (throttled together with R18).

### AP3 Accounts, sessions and request budget

| Findings | Approach |
|---|---|
| R1, R10 | `AccountRuntime` with a state machine: `ok` → `auth_required` → `ok`. On `UnauthorizedError`, `InvalidStateError`, `InvalidCredentialsError` or `ValueError("No username or password …")`: set the state, `entry.async_start_reauth(hass)`, repairs notice, **no further fetches with this account**. At setup: `login_state != LOGGED_IN` → `ConfigEntryAuthFailed`; corrupt data → `ConfigEntryError`. |
| S1, R5 | After every fetch compare `to_json()` with the stored state and save on change, setting `account.password = None`. Migration (entry version 2.1): remove the password from existing entries. The password stays in the memory of the worker only during the login. |
| R4, R21 | `scheduler.py`: save the time and number of requests per account in `helpers.storage.Store`. A fetch starts only if the last one is at least 15 minutes ago. In addition a budget of requests per cycle; accessories that no longer fit come first in the next cycle. Refreshes caused by setup, `update_entity` or restart are moved to the next permitted time. Remove `async_update` from the entities. |
| R22 | Recognise the failure patterns of the Apple change specifically (`KeyError: 'searchPartyToken'`, error code `-80045`) and display them as a separate state `apple_unsupported` with a repairs notice and a link to the issue. In this state **no** automatic login attempt. |
| R13, R14, R17 | Hub with clean setup and teardown; the flow closes its accounts in `async_remove`; state replaces the log. |
| S4 | `anisette_libs_path` in the configuration directory, remember the SHA-256 of the file at the first download and check it on every start. Because of R22, fix it only after the library is fixed, because the library bundle may change. |

### AP4 Structure and maintainability

| Findings | Approach |
|---|---|
| W1 | `EntryType(StrEnum)` and a table type → class; replaces all strings and the four `if` chains. |
| W2, W3, W9 | `FindMyDeviceEntity(CoordinatorEntity)`; sensors as `SensorEntityDescription` with `value_fn`; `options` and `translation_key` for enum sensors. |
| W4, R11 | `bluetooth/manager.py` with **one** callback and one shared lookup for all accessories. In the manifest from `dependencies` to `after_dependencies`; import only once `"bluetooth" in hass.config.components`. |
| R8, R9 | `unique_id` for device entries; account device via a hash of the email. Migration of the device registry. |
| R18 | One central, throttled save point for alignments. |
| W5–W8 | Remove dead code, align documentation, docstrings, translations (German). |
| W10 | Let Home Assistant generate entity IDs. **Only with a migration**, otherwise the IDs of existing installations change (see chapter 10). |

### AP5 Hardening and privacy

| Findings | Approach |
|---|---|
| S5 | Debounce realignment jobs: at most one per accessory and minute, even if the library rejects the alignment. Adopt the alignment from Bluetooth only if the index is not behind the expectation from the last confirmed location. Note in the README and entity description: not a security feature. |
| S7 | Mask the email; neutral device names. |
| new | `diagnostics.py` with `async_redact_data`; `repairs.py`. |

### AP6 Dependencies

See [chapter 8](#8-dependencies).

---

## 7. Strategy per finding

| ID | Package | Core of the solution | Migration needed | Prior work |
|---|---|---|---|---|
| S1 | AP3 | Do not store the password; save the session continuously | yes (remove password) | – |
| S2 | AP1 | Central redaction of the logs | no | – |
| S3 | AP1 | Admin service, required fields | no (service calls without `filter` break; release note) | – |
| S4 | AP3 | Local path and hash check | no | upstream proposal |
| S5 | AP5 | Debounce, plausibility check, documentation | no | – |
| S6 | AP1 | Remove `state_class` | no (delete old statistics manually) | – |
| S7 | AP5 | Masking | optional (device names) | – |
| R1 | AP1 → AP3 | Re-auth per account; polling keeps running | no | PR #66 |
| R2 | AP1 | Correct `success`; errors → `UpdateFailed` | no | PR #66 |
| R3 | AP1 | `reload()` after unload | no | – |
| R4 | AP3 | Scheduler with stored time | no | – |
| R5 | AP3 | Save the session after a change | no | – |
| R6 | AP1 | Error mapping in the flow | no | – |
| R7 | AP1 | `_abort_if_unique_id_mismatch` | no | – |
| R8 | AP4 | `unique_id` for devices | yes | – |
| R9 | AP4 | Identifier from the full email (hash) | yes (device registry) | – |
| R10 | AP3 | Validation at setup | no | – |
| R11 | AP4 | Bluetooth optional | no | – |
| R12 | AP1 | Correct the minimum version | no | – |
| R13 | AP3 | Hub with lifecycle | no | – |
| R14 | AP3 | Flow cleans up | no | – |
| R15 | AP1 | Correct the filter | no | – |
| R16 | AP1 | Correct the formula | no | PR #64 |
| R17 | AP3 | State instead of log | no | – |
| R18 | AP4 | Throttled saving | no | – |
| R19 | AP1 | Repair the blueprint | no (users re-import) | – |
| R20 | AP2 | Key cache, worker thread, search limit | no | PR #69, FindMy.py #285 |
| R21 | AP2, AP3 | Search limit, budget per account | no | PR #69 |
| R22 | AP1, AP3 | Make the error visible; no auto-login; wait for the library fix | no | FindMy.py PR #279 |
| W1–W10 | AP4 | see AP4 | only W10 | – |

---

## 8. Dependencies

### 8.1 What the integration really needs

Verified by test: `findmy` was imported while individual packages were blocked.

| Package | Comes from | Needed at import? | What for in practice | Already in Home Assistant? | Latest version |
|---|---|---|---|---|---|
| `FindMy` 0.10.2 | manifest | yes | everything | no | 2026-09-14 (current) |
| `anisette` 1.2.4 | FindMy | **yes**, also with an Anisette server | local Anisette | no | 2025-10-09 |
| `unicorn` 2.1.4 (58 MB) | anisette | yes | CPU emulator for the local Anisette | no | 2025-09-09 |
| `pyelftools` | anisette | yes | read ELF files | no | 2026-05-29 |
| `fs` 2.4.16 | anisette | **no, never imported** | nothing (legacy burden in the metadata) | no | 2022-05-02 |
| `appdirs` 1.4.4 | fs | no | nothing | no | 2020-05-11 |
| `srp` 1.0.22 | FindMy | yes | SRP login at Apple (uses the private module `srp._pysrp`) | no | 2024-11-01 |
| `six` | srp, fs | yes (because of `srp`) | Python 2 compatibility in `srp` | no | 2024-12-04 |
| `beautifulsoup4`, `soupsieve` | FindMy | yes | **one** place: read `<script class="boot_args">` from the 2FA page | required by the core integration `scrape`, hence usually present in the official images | 2026-06-07 |
| `bleak` | FindMy | yes | only for the library's own scanner, which the integration does not use | yes (`bluetooth`), pinned there to 3.0.2 | – |
| `cryptography`, `aiohttp`, `certifi`, `urllib3`, `typing-extensions` | FindMy, anisette | yes | – | yes (core) | – |

### 8.2 Correction and actual risks

- **`fs` is not a failure risk.** `anisette` 1.2.4 has a virtual file system of its own based on `tarfile`. `fs` and `appdirs` are only installed, but not loaded. Home Assistant does restrict `setuptools<82`, because newer versions no longer contain `pkg_resources`. This does not affect the integration, however, as long as nobody imports `fs`.
- **Real risk: `bleak`.** `FindMy` requires `bleak>=3.0.2,<3.1.0`. Home Assistant pins `bleak==3.0.2`. If Home Assistant raises this to 3.1, `FindMy==0.10.2` can no longer be installed, and the integration does not load, although it does not use the scanner of the library at all.
- **Real risk: binding to `anisette`.** The library is also imported with an Anisette server. Any disruption there affects all users, and the 58 MB of `unicorn` have to be installed on every device. With R22 a rework is due here anyway.
- **Lockfile against the constraints of Home Assistant.** `uv.lock` contained `mashumaro` 3.16, although Home Assistant requires at least 3.17 in `package_constraints.txt`. These constraints are not in the package metadata, so `uv` does not respect them on its own.

### 8.3 What can be done in the fork immediately

| # | Measure | Effect | Effort |
|---|---|---|---|
| A1 | Move Bluetooth from `dependencies` to `after_dependencies`, load code lazily (AP4) | Bluetooth and `usb` are no longer forced; tests without Bluetooth possible | medium |
| A5 | Remove `homeassistant` from `[project].dependencies`; `pytest-homeassistant-custom-component` into the dev group | The test library fixes Home Assistant together with matching dependencies and brings fixtures along | small |
| A6 | `findmy==0.10.2` in `pyproject.toml`, matching the manifest; CI check for equality | no deviation between development and users | small |
| A8 | Enforce the constraints of Home Assistant in the lockfile: generate `[tool.uv] constraint-dependencies` from `homeassistant/package_constraints.txt` (script), or rely on A5 | Errors like `mashumaro` 3.16 no longer occur | small |
| A9 | Add `"loggers": ["findmy", "anisette"]` and `"codeowners"` to the manifest | Log levels of the library can be set in Home Assistant | trivial |

**Not** possible in the fork: leaving out individual packages of `FindMy`. Home Assistant installs the requirement from the manifest with all its dependencies, and there is no way to exclude any.

### 8.4 Proposals to the original repos

Ordered by the ratio of benefit to effort:

| # | Where | Proposal | Savings for users | Effort | Evidence |
|---|---|---|---|---|---|
| U1 | `Anisette.py` | Remove `fs` from `dependencies` | `fs`, `appdirs` (and `six` from this side) | one line | Import test: runs without `fs` |
| U2 | `FindMy.py` | Make `bleak` optional (extra `scanner`), delay the import in `findmy/__init__.py`. Put the classes `OfflineFindingDevice` & co. in a module without a `bleak` import | resolves the coupling to the `bleak` version of Home Assistant | small | `bleak` only in `scanner/scanner.py` |
| U3 | `FindMy.py` | Make `anisette` optional (extra `local-anisette`), import only in `LocalAnisetteProvider` | 58 MB `unicorn`, `pyelftools`, `anisette` for everyone with an Anisette server | small, but coordinate with R22/PR #279 | Import at module level (`reports/anisette.py:17`) |
| U4 | `FindMy.py` | Replace `beautifulsoup4` with `html.parser` from the standard library (about 15 lines) | `beautifulsoup4`, `soupsieve` | small | one use (`account.py:134`) |
| U5 | `FindMy.py` | Cache finished key pairs and do not derive secondary keys multiple times (#285) | fixes R20 at the root | medium | Profile in chapter 3 |
| U6 | `Anisette.py` | Download with timeout, status check and optional hash | S4 | small | `_util.py:44-50` |
| U7 | `FindMy.py` | Replace `srp`, see 8.5 | `srp`, `six` | medium | Feasibility test |

### 8.5 More modern or more widespread packages

| Today | Candidate | Assessment |
|---|---|---|
| `srp` (pysrp; needs `six`, a private module is used) | **`srptools` 2.0.0** (2026-10-26, pure Python, no dependencies, ≥ 3.10). Already present in Home Assistant, because the integration `apple_tv` uses it via `pyatv`. | **Feasible, but not a direct replacement.** In the test the public value `A` and the session key matched `pysrp` if the password hash function is overridden for Apple's variant (without username). The proof `M1` still differed; it is composed differently and would have to be overridden as well. Effort: about 30 lines plus a test against Apple. Because of R22 currently not sensibly testable. Benefit: two packages fewer. Low priority. |
| `beautifulsoup4` | `html.parser` (standard library) | Clear gain (U4). |
| `fs` | – | Remove without replacement (U1). |
| `appdirs` | `platformdirs` would be the maintained successor | Resolved by U1. |
| `unicorn` | No alternative for the emulation. The way out is an Anisette server (then U3) or the "mac backend" from PR #279. | Wait and see what the fix for R22 brings. |
| `bleak` | – | Is the standard and pinned in Home Assistant; only make it optional (U2). |
| `basedpyright` | `mypy` (used by the core of Home Assistant) or `ty` (Astral) | Keep. `basedpyright` is stricter, runs fast and fits the existing configuration. A switch would bring work without gain. |
| `pre-commit` | `prek` (faster reimplementation) | Keep. The three local hooks are done in seconds. |
| `ruff` | – | Is already the standard. |

### 8.6 Recommendation

1. **Now in the fork:** A5, A6, A8, A9 in AP0; A1 in AP4.
2. **Now at the original repo:** Submit U1 (one line) and U5 (with the measured values from chapter 3).
3. **After the fix for R22:** Submit U2, U3, U4, U6, coordinated with the then-current structure of `anisette`.
4. **Later, if needed:** U7.
5. **No own fork of the library.** The Apple protocol keeps changing, as R22 shows. A fork would be outdated after a few weeks.

---

## 9. Order and milestones

```
AP0 Test base ──► AP1 Immediate fixes ──► AP2 Stage 1 (key cache) ──► AP2 Stage 2 (worker) ─┐
                        │                                                                   ├─► AP4 Structure ──► AP5 Hardening
                        └───────────────────────────────────────► AP3 Accounts/budget ◄─────┘
AP6: A5/A6/A8/A9 in AP0 · submit U1/U5 in parallel · A1 in AP4 · U2–U4/U6 after the fix for R22
```

| Milestone | Contents | Result for users |
|---|---|---|
| **M1** | AP0, AP1 | Errors are displayed correctly; re-auth starts; a defective account does not slow down the others; delete service secured; no secrets in the log |
| **M2** | AP2 stages 1 and 3 | Home Assistant stays usable during fetches; fewer requests for old tags |
| **M3** | AP2 stage 2, AP3 | Password no longer stored; query limit is kept and survives restarts; sessions are preserved |
| **M4** | AP4 | Bluetooth optional; no duplicate devices; maintainable structure |
| **M5** | AP5 | Diagnostics download, repairs notices, privacy |

**Dependencies between the packages:**
- AP2 stage 2 and AP3 share `worker.py` and `account.py`; they are designed together and delivered one after the other.
- AP4 (structural restructuring) comes after AP1 to AP3, so that bug fixes do not get lost in large restructuring commits.
- The adoption of PR #66 and #69 belongs at the beginning of AP1 and AP2 respectively, **before** any restructuring of the affected files. Afterwards they can no longer be adopted cleanly.

**A package is done when:** Tests for all covered findings exist and are green, `pre-commit` is green, the migration (if needed) is tested, README and review document show the new state.

---

## 10. Open decisions

| # | Question | Recommendation | Consequence of the alternative |
|---|---|---|---|
| E1 | Keep storing the password of the Apple ID? | **No**, at most as an explicitly chosen option. | With password: silent re-sign-in remains possible, but the password remains in plain text in every backup. |
| E2 | Adopt open PRs of the original repo or write them ourselves? | **Adopt** after review (#64, #66, #69). | Writing them ourselves: more effort, no reconciliation with the original possible any more. |
| E3 | Switch entity IDs to the standard scheme of Home Assistant (W10)? | **Not for now.** Only once a migration preserves the old IDs. | A switch without migration breaks the users' automations and dashboards. |
| E4 | Wait for the fix for the Apple change? | **Do not wait.** M1 and M2 do not depend on it and improve the behaviour precisely in this failure case. Only S4 and the proposals U2–U4/U6 afterwards. | Waiting delays fixing the blocking and the security holes. |
| E5 | Keep the delete service `delete_devices`? | Keep, but secured (AP1). | Removing it would be simpler, but takes away an existing function. |
| E6 | Should the fork give changes back to the original repo? | Yes, for bug fixes as individual PRs. This keeps the fork small. | Without giving back, the distance to the original grows with every change. |
