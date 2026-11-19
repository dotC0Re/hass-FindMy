# Code review hass-FindMy

| | |
|---|---|
| **As of** | 2026-11-18, commit `a46c8a9` (fork `dotC0Re/hass-FindMy`, identical to `malmeloo/hass-FindMy` up to `b2e6785`) |
| **Scope** | The whole integration `custom_components/findmy/` (≈ 3,300 lines), blueprints, project and CI configuration, relevant parts of the libraries `FindMy` 0.10.2 and `anisette` |
| **Goal** | Basis for a refactoring focused on **security** and **stability**, with good maintainability, as a foundation for further development |
| **Method** | Complete manual reading of all modules; every assumption checked against the source code of Home Assistant 2026.9.3 and FindMy 0.10.2 in `.venv`; individual behavioural assumptions verified by script |
| **Verification** | All findings were checked a second time, many by test. The result, three new findings (R20–R22) and the strategy for fixing them are in [REFACTORING_STRATEGY.md](REFACTORING_STRATEGY.md). Corrections are incorporated here and marked with "Verification:". |

## Contents

1. [Summary](#1-summary)
2. [Architecture overview](#2-architecture-overview)
3. [Findings: security and privacy](#3-findings-security-and-privacy)
4. [Findings: stability and correctness](#4-findings-stability-and-correctness)
5. [Findings: maintainability and documentation](#5-findings-maintainability-and-documentation)
6. [Dependencies: current state and reduction](#6-dependencies-current-state-and-reduction)
7. [Development environment, tests and CI](#7-development-environment-tests-and-ci)
8. [Refactoring plan](#8-refactoring-plan)
9. [Outlook for further development](#9-outlook-for-further-development)
10. [Appendix: Verified facts about Home Assistant and FindMy.py](#10-appendix-verified-facts-about-home-assistant-and-findmypy)

**Severity:** 🔴 High = security hole, data loss, risk of the Apple account being locked, or silent loss of function · 🟠 Medium = misbehaviour in realistic situations · 🟡 Low = edge case, cosmetic flaw, technical debt

**Status:** ✅ verified (proven in the source code or by test) · 🔍 plausible (sequence is coherent but not reproduced live) · 🌐 reported externally (from the original repositories, not verified myself)

---

## 1. Summary

The integration is rich in functionality: fetching via the Find My network, local Bluetooth detection including the DULT format of the second AirTag generation, presence and battery sensors. The code is typed, is checked with Ruff (`select = ["ALL"]`) and basedpyright, and the newer modules (`local_bluetooth.py`, `presence.py`) are well commented.

The weaknesses lie mainly in four areas:

1. **Secrets:** The Apple ID **password** is stored in plain text in `core.config_entries` and therefore in every backup (S1). Private keys of tags end up in the debug log (S2). A service that any user can call deletes **all** devices when called without parameters (S3).
2. **Session expiry:** When Apple ends a session, re-authentication **never** starts (R1). Failures are logged as successes (R2). Renewed tokens are lost on restart (R5).
3. **Rate limit:** The promised limit of one query per 15 minutes per account is not enforced. After an account is removed it is even permanently exceeded (R3, R4). This raises the risk of Apple locking the account.
4. **Structure:** A global, process-wide state (`hass.data[DOMAIN]`) is never torn down. A single coordinator for all accounts prevents a clean attribution of errors per account. There are no tests.

Most findings can be fixed with manageable effort. Chapter 8 proposes an order that closes the highest-risk items first and builds up a test base at the same time. The elaborated version with work packages is in [REFACTORING_STRATEGY.md](REFACTORING_STRATEGY.md).

The verification produced three further findings, which are described in the strategy: The fetch **blocks the event loop** of Home Assistant for seconds up to over a minute (R20), a fetch consists of **many HTTP requests** (R21), and in October 2026 Apple changed the sign-in, so **new sign-ins are expected to fail** (R22).

### Findings overview

| ID | Title | Severity | Status |
|---|---|---|---|
| [S1](#s1-apple-id-password-stored-in-plain-text) | Apple ID password stored in plain text | 🔴 | ✅ |
| [S2](#s2-private-keys-and-2fa-codes-in-the-log) | Private keys and 2FA codes in the log | 🔴 | ✅ |
| [S3](#s3-delete-service-without-admin-check-with-a-dangerous-default) | Delete service without admin check, default "delete everything" | 🔴 | ✅ |
| [S4](#s4-local-anisette-downloads-unverified-binaries) | Local Anisette downloads unverified binaries | 🟠 | ✅ |
| [S5](#s5-bluetooth-replay-can-fake-presence-and-shift-the-key-alignment) | Bluetooth replay fakes presence (🟠) and can delay location tracking (🟡) | 🟠 | ✅ |
| [S6](#s6-position-history-in-unlimited-long-term-statistics) | Position history in unlimited long-term statistics | 🟠 | ✅ |
| [S7](#s7-personal-data-in-logs-and-ids) | Personal data in logs and IDs | 🟡 | ✅ |
| [R1](#r1-re-authentication-is-never-started) | Re-authentication is never started; one expired account stops all polling | 🔴 | ✅ |
| [R2](#r2-failed-fetches-are-logged-as-successes) | Failed fetches are logged as successes | 🔴 | ✅ |
| [R3](#r3-polling-interval-is-not-adjusted-after-an-account-is-removed) | Polling interval not adjusted after an account is removed | 🔴 | ✅ |
| [R4](#r4-rate-limit-is-not-enforced) | Rate limit is not enforced | 🟠 | ✅ |
| [R5](#r5-renewed-session-data-is-not-saved) | Renewed session data is not saved | 🟠 | ✅ |
| [R6](#r6-config-flow-unhandled-exceptions-and-wrong-step-id) | Config flow: unhandled exceptions, wrong step ID | 🟠 | ✅ |
| [R7](#r7-re-authentication-accepts-a-different-account) | Re-authentication accepts a different account | 🟠 | ✅ |
| [R8](#r8-devices-can-be-created-twice) | Devices can be created twice | 🟠 | ✅ |
| [R9](#r9-account-devices-collide-when-the-local-part-of-the-email-address-is-the-same) | Account devices collide when the local part of the email address is the same | 🟠 | ✅ |
| [R10](#r10-corrupt-entry-data-and-logged-out-accounts-at-startup) | Corrupt entry data and logged-out accounts at startup | 🟠 | ✅ |
| [R11](#r11-bluetooth-is-a-hard-dependency-although-optional) | Bluetooth is a hard dependency although optional | 🟡 | ✅ |
| [R12](#r12-wrong-minimum-version-in-hacsjson) | Wrong minimum version in `hacs.json` | 🟠 | ✅ |
| [R13](#r13-setup-and-teardown-incomplete) | Setup and teardown incomplete | 🟡 | ✅ |
| [R14](#r14-http-sessions-in-the-config-flow-are-never-closed) | HTTP sessions in the config flow are never closed | 🟡 | ✅ |
| [R15](#r15-filter-rolling-in-the-delete-service-has-no-effect) | Filter `rolling` in the delete service has no effect | 🟡 | ✅ |
| [R16](#r16-next-fetch-sensor-calculated-incorrectly) | "Next fetch" sensor calculated incorrectly | 🟡 | ✅ |
| [R17](#r17-fetch-log-grows-without-bound) | Fetch log grows without bound | 🟡 | ✅ |
| [R18](#r18-config-entry-is-rewritten-on-every-fetch) | Config entry is rewritten on every fetch | 🟡 | ✅ |
| [R19](#r19-blueprint-tag_stale-checks-only-once-per-hour) | Blueprint `tag_stale` checks only once per hour | 🟡 | ✅ |
| [R20](REFACTORING_STRATEGY.md#r20-the-fetch-blocks-the-event-loop-of-home-assistant) | The fetch blocks the event loop of Home Assistant (new) | 🔴 | ✅ |
| [R21](REFACTORING_STRATEGY.md#r21-one-fetch-is-many-http-requests) | One fetch is many HTTP requests (new) | 🟠 | ✅ |
| [R22](REFACTORING_STRATEGY.md#r22-apple-change-in-october-2026-sign-in-and-fetch-disrupted) | Apple change in October 2026: sign-in and fetch disrupted (new) | 🔴 | 🌐 |
| [W1–W10](#5-findings-maintainability-and-documentation) | Maintainability and documentation | 🟡 | ✅ |

---

## 2. Architecture overview

### Modules

| Module | Purpose |
|---|---|
| [`__init__.py`](../custom_components/findmy/__init__.py) | Setup, unload and migration of the config entries; dispatches to the platforms |
| [`storage.py`](../custom_components/findmy/storage.py) | `RuntimeStorage`: global object in `hass.data[DOMAIN]` holding all accounts and devices, Bluetooth intermediate states and the coordinator |
| [`coordinator.py`](../custom_components/findmy/coordinator.py) | **One** `DataUpdateCoordinator` for all accounts; picks accounts in turn (round robin) and keeps an in-memory fetch log |
| [`config_flow.py`](../custom_components/findmy/config_flow.py) | Sign-in to the account including 2FA, re-authentication, import of devices (keys, `.json`, `.plist`), options flow for the absence time |
| [`device_tracker.py`](../custom_components/findmy/device_tracker.py) | Tracker entity; **additionally** the whole local Bluetooth matching for accessories with rolling keys |
| [`local_bluetooth.py`](../custom_components/findmy/local_bluetooth.py) | Pure functions: generate candidate keys, parse and match Offline Finding and DULT advertisements |
| [`presence.py`](../custom_components/findmy/presence.py) | Presence sensor and signal strength, fed via dispatcher signals from the tracker |
| [`sensor.py`](../custom_components/findmy/sensor.py), [`binary_sensor.py`](../custom_components/findmy/binary_sensor.py) | Position, battery and status counters per device; fetch status per account |
| [`_entity.py`](../custom_components/findmy/_entity.py) | Shared helpers: IDs, device info, decoding of the battery bits |
| [`services.py`](../custom_components/findmy/services.py) | Service `findmy.delete_devices` |

### Data flow

```
                      ┌──────────────── hass.data["findmy"] = RuntimeStorage (global) ────────────────┐
config entries ──────►│ _entries: {entry_id: AsyncAppleAccount | KeyPair | FindMyAccessory | Fixed…}  │
 (account / device)   │ local_observations / local_status / local_rssi  (never cleaned up)           │
                      │ coordinator: FindMyCoordinator (a single one, without config_entry)          │
                      └───────────────┬──────────────────────────────────────────────┬───────────────┘
                                      │ every 15 min / number of accounts,            │
                                      │ account in turn                              │
                                      ▼                                              │
                      account.fetch_location(all devices) ──► Apple                  │
                                      │                                              │
                                      ▼                                              │
     device_tracker / sensor / binary_sensor (per device, via coordinator context)   │
                                                                                     │
HA Bluetooth ──► FindMyDeviceTracker._async_track_service_info ──► local_bluetooth.match_*()
                      │  (3 callbacks per accessory)                                 │
                      └─ dispatcher signal ──► presence, signal and battery sensors ◄┘
```

### Strengths that should be kept

- **Pure functions in `local_bluetooth.py`:** easy to test, and the prefix lookup `O(1)` is efficient.
- **Key generation in the executor** on a snapshot (`build_candidate_key_lookup_from_json`), so that the generators in the live object are not modified.
- **Write throttling:** The recorder and config entries are deliberately throttled (`_ATTRIBUTE_UPDATE_DELAY`, `_LOCAL_ALIGNMENT_SAVE_DELAY`, `_unrecorded_attributes` for the MAC address).
- **Strict static analysis** with Ruff `ALL` and basedpyright.
- **Migration** of the config entries exists and is versioned.

---

## 3. Findings: security and privacy

### S1 Apple ID password stored in plain text

🔴 High · ✅ verified · [`config_flow.py:409-412`](../custom_components/findmy/config_flow.py#L409-L412), FindMy `reports/account.py:439-455`

**Problem:** `AsyncAppleAccount.to_json()` serialises not only the session tokens but also `account.username` **and `account.password`**. The library sets the password at login (`_gsa_authenticate`, lines 801-802) so that it can silently sign in again after an HTTP 401. The integration stores this mapping unchanged as `entry.data["account_data"]`.

**Impact:** The password of the Apple ID is stored in plain text in `/config/.storage/core.config_entries` and in **every Home Assistant backup**, including cloud backups. Anyone with read access to the file system or a backup takes over the Apple account. For an Apple ID this typically also affects iCloud data, photos and device locks.

**Recommendation:**
1. **Do not** persist the password: set `account_data["account"]["password"] = None` before saving. A migration removes the password from existing entries.
2. Consequence: The silent re-login after a 401 no longer happens. Instead a re-auth flow starts (see R1), and the session is saved as soon as it has been renewed (see R5). In practice the tokens last weeks to months.
3. Optional: A setting "Store password for automatic re-sign-in", off by default, with a clear warning.
4. Independently of this, introduce `diagnostics.py` with `async_redact_data` for `password`, `login.data`, `anisette` and private keys (see chapter 9).
5. Document in the README which secrets are stored where.

### S2 Private keys and 2FA codes in the log

🔴 High · ✅ verified · [`config_flow.py:551`](../custom_components/findmy/config_flow.py#L551), [`:373`](../custom_components/findmy/config_flow.py#L373), [`:349`](../custom_components/findmy/config_flow.py#L349)

**Problem:** Every flow step logs the complete user input at debug level. The password is masked in `acc_login` and `reauth_confirm`, but other secrets are not:
- `dev_static` logs `private_key` in plain text. With this key anyone can fetch the position of the tag from Apple.
- `acc_2fa_submit` logs the 2FA code.

**Impact:** Debug logs are routinely shared in GitHub issues and forums. A shared private key enables permanent tracking, because static keys never rotate.

**Recommendation:** A central function `_redact(user_input)` with a list of secret fields (`password`, `private_key`, `code`, `file`) for **all** debug output of the flows. A unit test ensures that no field in the list is ever logged unfiltered.

### S3 Delete service without admin check, with a dangerous default

🔴 High · ✅ verified · [`services.py:20-26`](../custom_components/findmy/services.py#L20-L26), [`:75-80`](../custom_components/findmy/services.py#L75-L80)

**Problem:** `findmy.delete_devices` is registered with `hass.services.async_register`, not with `async_register_admin_service`. As a result **any** Home Assistant user can call the service, including non-admins, automations, scripts and voice assistants. The default for `filter` is `all`. A call without parameters therefore removes **all** device entries irrevocably, together with the imported key files.

**Impact:** Data loss. An AirTag key from a `.plist` cannot easily be obtained again, and the alignment of the keys is lost.

**Recommendation:**
1. Register with `homeassistant.helpers.service.async_register_admin_service`.
2. Make `filter` a required field and do not give it a default `all`. Additionally require a required field `confirm: true`.
3. Disable or export entries before deleting them. Alternatively drop the service altogether, since Home Assistant can already delete entries through the UI.

### S4 Local Anisette downloads unverified binaries

🟠 Medium · ✅ verified · [`config_flow.py:264-267`](../custom_components/findmy/config_flow.py#L264-L267), [`storage.py:88-92`](../custom_components/findmy/storage.py#L88-L92), `anisette/anisette.py:26`, `anisette/_util.py:44-50`

**Problem:** Without an Anisette URL the integration uses `LocalAnisetteProvider()` **without** `libs_path`. The `anisette` library then downloads an ARM64 library bundle from `https://anisette.dl.mikealmel.ooo/libs` **on every start and for every account** and runs it in the CPU emulator `unicorn`. In this:
- There is **no integrity check**, neither hash nor signature. Only TLS and the operator of the domain are trusted.
- There is **no timeout** (`urllib3.PoolManager().request()` without `timeout`). A hanging server blocks an executor thread indefinitely.
- The HTTP status is not checked. An error page is interpreted as a library.

**Impact:** Supply chain risk: A compromised download server delivers code that is parsed (`pyelftools`) and emulated inside Home Assistant. In addition there is a stability risk: Without internet or during a server outage no account starts.

**Recommendation:**
1. Set `libs_path` to a file in the configuration directory, for example `hass.config.path(".storage/findmy_anisette_libs.bin")`. Both `LocalAnisetteProvider(libs_path=…)` and `AsyncAppleAccount.from_json(…, anisette_libs_path=…)` support this. The download then happens only once.
2. Store the SHA-256 of the known library version in the integration and check it before use.
3. Propose an upstream fix in `anisette`: timeout, status check, optional hash.
4. Explain in the README the choice between local and remote Anisette with their advantages and disadvantages.

### S5 Bluetooth replay can fake presence and shift the key alignment

🟠 Medium (presence) / 🟡 Low (alignment) · ✅ verified · [`device_tracker.py:298-312`](../custom_components/findmy/device_tracker.py#L298-L312), [`local_bluetooth.py:171-205`](../custom_components/findmy/local_bluetooth.py#L171-L205)

**Verification:** Point 2 was worded too strongly in the first version. The library discards alignments whose index or time lies in the past (`accessory.py:368-381`). A replay therefore only has an effect if the stored alignment is **older** than the replayed signal. In the test (alignment 3 h old, signal 2 h old) the next fetch requested keys that were 2 h behind the true state. The lag remained until a genuine signal corrected it. If the alignment was fresh, the replay had no effect. Newly added: Every such signal triggers a recalculation of the candidates in the integration, even if the library rejects the alignment.

**Problem:**
1. Advertisements are **not authenticated**. Anyone who records an advertisement of the tag (range ≈ 10-50 m) and re-transmits it near the Home Assistant scanner produces a match. With the DULT format the MAC address alone is enough (`match_dult_advertisement` only compares `_lookup_prefix(mac)`).
2. A match with `can_align=True` calls `update_alignment(detected_at, key_index)` and **stores** the result hourly in the config entry. The window of candidates reaches back 36 h. A replay of an advertisement up to 36 h old is therefore counted as a match. The check `observation.detected_at < previous.detected_at` does not apply because the replay is newly received. Whether the alignment is actually shifted by this depends on the stored state (see the verification above).

**Impact:**
- Presence sensors report "home" although the tag is missing. This becomes critical if automations use it to control doors, alarm systems or similar.
- If the stored alignment is older than the replayed signal, the network-based location tracking lags behind by the age of the signal until a genuine advertisement corrects it (confirmed by simulation). The damage is limited and requires the attacker to be within radio range of the scanner.

**Recommendation:**
1. Only adopt the alignment from Bluetooth if the index is not behind the expectation from the last confirmed location. Debounce recalculations of the candidates.
2. State clearly in the README and in the description of the presence sensor: **Not a security feature.** Do not use for access control.
3. For DULT matches, check whether the same MAC address was previously confirmed with the full key via the Offline Finding format.

### S6 Position history in unlimited long-term statistics

🟠 Medium · ✅ verified · [`sensor.py:182-205`](../custom_components/findmy/sensor.py#L182-L205)

**Problem:** The `Latitude` and `Longitude` sensors have `state_class: measurement` and the unit `°`. Home Assistant derives **long-term statistics** from them (hourly mean, minimum and maximum). These are **not** deleted by `recorder.purge_keep_days`.

**Impact:** A permanent, hourly-resolution movement history of every tracker, including of carried objects and therefore of people, without an expiry date. Technically it is also meaningless, because the mean of two coordinates is not a location.

**Recommendation:** Remove `state_class`. Possibly disable the lat/lon sensors by default (`entity_registry_enabled_default = False`), since the tracker already carries the position as an attribute. Users should clean up existing statistics via Developer tools → Statistics; a note about this belongs in the release notes.

### S7 Personal data in logs and IDs

🟡 Low · ✅ verified · [`config_flow.py:259`](../custom_components/findmy/config_flow.py#L259), [`:472`](../custom_components/findmy/config_flow.py#L472), [`_entity.py:46-66`](../custom_components/findmy/_entity.py#L46-L66)

- The email address is logged at **info level**, so it is visible by default.
- Name and email of the account appear in the device name (`Apple: First Last <mail>`), and the local part of the email is embedded in the entity IDs. This is unfavourable for screenshots and shared dashboards.

**Recommendation:** Log the email only at debug level and mask it (`j***@example.com`). Use "Apple account 1" or just the first name as the device name, the email at most as an attribute.

---

## 4. Findings: stability and correctness

### R1 Re-authentication is never started

🔴 High · ✅ verified · [`coordinator.py:72-79`](../custom_components/findmy/coordinator.py#L72-L79), [`:217-235`](../custom_components/findmy/coordinator.py#L217-L235), [`storage.py:40`](../custom_components/findmy/storage.py#L40)

**Problem:** The coordinator is created in `async_setup` without `config_entry=`. Home Assistant then takes `config_entries.current_entry.get()`, and in `async_setup` that is `None`. The comment in lines 226-229 promises that `ConfigEntryAuthFailed` prompts the user to sign in again. However, Home Assistant calls `async_start_reauth_if_available` **only** if `self.config_entry` is set (`helpers/update_coordinator.py:553-554`). Even if an entry were set, it would be the wrong one: The coordinator is responsible for all accounts, but re-auth belongs to the account that has just failed.

**Impact:** When a session expires, **all** device entities become `unavailable` (`last_update_success = False`). The user gets no prompt, the error is silent.

**Verification (confirmed and aggravated by test):** After `ConfigEntryAuthFailed` the coordinator schedules **no further fetch** (`helpers/update_coordinator.py:585`). A single expired account therefore ends the polling of **all** accounts until Home Assistant restarts or someone refreshes manually.

**Recommendation:** On an auth error, explicitly send the entry **of the affected account** to re-auth: `entry.async_start_reauth(hass)`. This requires the mapping account → entry to be known (see the target architecture in chapter 8). The error is handled only for that account. An error on account A must not block account B and must not set all devices to `unavailable`.

### R2 Failed fetches are logged as successes

🔴 High · ✅ verified · [`coordinator.py:212-240`](../custom_components/findmy/coordinator.py#L212-L240)

**Problem:**
```python
success = False
try:
    success = True                                   # ← before the call!
    device_reports = await account.fetch_location(devices)
except UnauthorizedError: success = False; ...
except InvalidStateError: success = False; ...
finally:
    self._log(account, FETCH_SUCCESS if success else FETCH_ERROR)
```
All other exceptions are left with `success = True` and are logged as a **success**. This affects `UnhandledProtocolError`, `aiohttp.ClientError`, `TimeoutError`, Anisette errors and `KeyError` (for example `'searchPartyToken'` after the Apple change, see R22), and these are in practice the most frequent errors. Confirmed by test for `UnhandledProtocolError` and `TimeoutError`. `EmptyResponseError` is caught by the library itself. Because they are additionally not translated into `UpdateFailed`, Home Assistant logs them as "Unexpected error" with a stack trace.

**Impact:** The `Last fetch result` sensors show `success` although nothing was fetched. Troubleshooting becomes misleading.

**Recommendation:** Set `success = True` **after** the `await`. Catch known errors (`UnhandledProtocolError`, `EmptyResponseError`, `aiohttp.ClientError`, `TimeoutError`) specifically and translate them into `UpdateFailed`, with a short message and without a stack trace. Only genuinely unexpected errors may appear as a stack trace.

### R3 Polling interval is not adjusted after an account is removed

🔴 High · ✅ verified · [`__init__.py:103-126`](../custom_components/findmy/__init__.py#L103-L126)

**Problem:** `async_setup_entry` calls `coordinator.reload()`, but `async_unload_entry` does not. The interval is `900 s / number of accounts`.

**Example:** With two accounts the interval is 450 s. If one account is removed or disabled, it stays at 450 s, and the remaining account is polled **every 7.5 minutes**, i.e. twice as often as promised. This also applies when `async_update_reload_and_abort` reloads during a re-authentication, until the next setup.

**Impact:** Higher risk of the Apple account being locked. This is exactly what the README warns about.

**Recommendation:** Call `reload()` after every unload. Better in principle: Enforce the rate limit per account instead of via a global interval (see R4).

### R4 Rate limit is not enforced

🟠 Medium · ✅ verified · [`__init__.py:95-98`](../custom_components/findmy/__init__.py#L95-L98), [`sensor.py:146-147`](../custom_components/findmy/sensor.py#L146-L147), [`:314-315`](../custom_components/findmy/sensor.py#L314-L315), [`binary_sensor.py:104-105`](../custom_components/findmy/binary_sensor.py#L104-L105)

**Problem:** The limit "1 fetch per 15 minutes per account" results only from the interval, it is not checked per account. Additional fetches are caused by:
- **Every setup of an entry**, also when a new device is added. `schedule_refresh()` fetches data after 10 s and uses the **next** account in turn for this.
- **`homeassistant.update_entity`** on any FindMy entity, including from automations. `async_update` calls `async_request_refresh()` and is debounced by only 10 s.
- **Restarts of Home Assistant**: The fetch log exists only in memory, after 10 s a new fetch is made regardless of the last fetch.

**Impact:** With frequent restarts, when importing many devices or with automations using `update_entity`, the number of requests rises without limit.

**Recommendation:** One scheduler per account with a **persisted** time of the last fetch (`helpers.storage.Store`). A fetch happens only if `now - last_fetch >= 15 min`. Manual refreshes are deferred until then and do not trigger another fetch. Remove `async_update` from the entities, since they do not poll.

### R5 Renewed session data is not saved

🟠 Medium · ✅ verified · [`coordinator.py:216`](../custom_components/findmy/coordinator.py#L216), FindMy `reports/account.py:690-700`

**Problem:** When FindMy receives an HTTP 401, it signs in again with the stored password and renews `login_state_data` with new tokens. However, the integration **never** writes `account.to_json()` back to the config entry, except in the re-auth flow.

**Impact:** After every restart it starts with the outdated tokens. This leads to a 401, a re-sign-in with the password, and that again on every restart. Frequent password logins from changing Anisette identities are a typical trigger for security challenges and locks at Apple. Without a stored password (recommendation for S1) every session renewal would be lost after the restart.

**Recommendation:** After every fetch compare `to_json()` with the stored state and call `async_update_entry` if it changed. The password stays removed (see S1).

### R6 Config flow: unhandled exceptions and wrong step ID

🟠 Medium · ✅ verified · [`config_flow.py`](../custom_components/findmy/config_flow.py)

| Location | Problem |
|---|---|
| [`:271-281`](../custom_components/findmy/config_flow.py#L271-L281), [`:481-495`](../custom_components/findmy/config_flow.py#L481-L495) | Only `InvalidCredentialsError` and `UnhandledProtocolError` are caught. Network errors (`aiohttp.ClientError`, `TimeoutError`), an unreachable Anisette URL or an invalid URL format lead to "Unknown error" and a stack trace. |
| [`:301`](../custom_components/findmy/config_flow.py#L301), [`:362`](../custom_components/findmy/config_flow.py#L362) | `get_2fa_methods()` and `request()` are unprotected. FindMy raises `UnhandledProtocolError` here if Apple returns a status code other than 2xx. |
| [`:356-360`](../custom_components/findmy/config_flow.py#L356-L360) | `except KeyError, ValueError` does not catch an `IndexError` (list access `self._2fa_methods[method_id]`). |
| [`:379`](../custom_components/findmy/config_flow.py#L379) | `step_id="2fa_submit"` instead of `"acc_2fa_submit"`. Submitting leads to the non-existent step `async_step_2fa_submit`. |
| [`:397`](../custom_components/findmy/config_flow.py#L397), [`:405-425`](../custom_components/findmy/config_flow.py#L405-L425) | After the 2FA it is not checked whether `login_state == LOGGED_IN`. A half-signed-in account would be saved. |
| [`:662-690`](../custom_components/findmy/config_flow.py#L662-L690) | During file import only `ValueError` is suppressed. `FindMyAccessory.from_plist` raises `KeyError` on missing fields (FindMy `accessory.py:420-440`), and that aborts the flow. |

**Recommendation:** A uniform error mapping: error class → error key (`cannot_connect`, `anisette_unreachable`, `invalid_auth`, `2fa_invalid`, `invalid_file`) with matching translations. Merge the sign-in logic of `acc_login` and `reauth_confirm`, which exists twice today, into one method `_async_login()`.

### R7 Re-authentication accepts a different account

🟠 Medium · ✅ verified · [`config_flow.py:444-501`](../custom_components/findmy/config_flow.py#L444-L501)

**Problem:** In the re-auth step the email is only prefilled and can be changed. There is no `async_set_unique_id` and no `_abort_if_unique_id_mismatch()`. If the user signs in with a different account, this overwrites the data of the existing entry. `unique_id` and title then continue to point to the old account.

**Recommendation:** `await self.async_set_unique_id(email.lower())`, then `self._abort_if_unique_id_mismatch(reason="wrong_account")`. Use the modern helper `self._get_reauth_entry()` instead of `self.context["entry_id"]`.

### R8 Devices can be created twice

🟠 Medium · ✅ verified · [`config_flow.py:547-659`](../custom_components/findmy/config_flow.py#L547-L659)

**Problem:** Device entries get no `unique_id`. If the same key is imported twice, two entries with identical entity `unique_id`s are created. Home Assistant rejects the entities of the second entry ("ID already exists"). The entry remains, however, and the device appears only once in the coordinator.

**Recommendation:** `await self.async_set_unique_id(device_unique_id(device))` and `self._abort_if_unique_id_configured()`. Retrofit existing entries by migration.

### R9 Account devices collide when the local part of the email address is the same

🟠 Medium · ✅ verified · [`_entity.py:65-66`](../custom_components/findmy/_entity.py#L65-L66), [`sensor.py:322-328`](../custom_components/findmy/sensor.py#L322-L328)

**Problem:** The identifier in the device registry is `email.split("@")[0]`. `max@gmail.com` and `max@icloud.com` therefore end up **on the same device**. The `unique_id`s of the entities, in contrast, use the full email, so they do not collide.

**Recommendation:** Use `account_unique_id` (full email, better still a hash of it) for the identifier. Move existing devices by migration (`device_registry.async_update_device(new_identifiers=…)`).

### R10 Corrupt entry data and logged-out accounts at startup

🟠 Medium · ✅ verified · [`__init__.py:77-100`](../custom_components/findmy/__init__.py#L77-L100), [`storage.py:83-136`](../custom_components/findmy/storage.py#L83-L136)

- If `from_json` raises (`ValueError` or `KeyError` on damaged data), the setup fails with a generic stack trace. `ConfigEntryError` with an understandable message is better.
- An account that was stored with `login_state != LOGGED_IN` is loaded as if everything were fine. The error only shows at the next fetch, and then R1 applies. Better: raise `ConfigEntryAuthFailed` immediately, then Home Assistant starts re-auth correctly for **this** entry.
- A failure in `async_forward_entry_setups` leaves an entry in `RuntimeStorage` without there being any entities.

### R11 Bluetooth is a hard dependency although optional

🟡 Low · ✅ verified · [`manifest.json`](../custom_components/findmy/manifest.json), [`device_tracker.py:10`](../custom_components/findmy/device_tracker.py#L10), [`presence.py:13`](../custom_components/findmy/presence.py#L13)

**Verification:** Downgraded from 🟠 to 🟡. The setup of the Bluetooth integration ends regularly with `return True`; I did not find an abort there because of missing adapters. A total failure is therefore presumably rare. What remains is that Bluetooth together with `usb` is forced on every user and that tests do not run on Windows.

**Problem:** `"dependencies": ["bluetooth"]` and the module-level imports mean that FindMy **does not load** if the Bluetooth integration does not start. This happens for example with containers without D-Bus, on some NAS systems or in Windows and development environments (see chapter 7). Fetching via the Find My network does not need Bluetooth, however.

**Recommendation:** Move it to `after_dependencies`. Move the Bluetooth matching into a separate module that is loaded only if `"bluetooth" in hass.config.components` holds. See also chapter 6.

### R12 Wrong minimum version in `hacs.json`

🟠 Medium · ✅ verified · [`hacs.json`](../hacs.json)

`"homeassistant": "2024.7.4"` is much too low. The code uses Python 3.14 syntax (`except KeyError, ValueError` per PEP 758), `config_entry` generics and `ConfigFlowResult`, and `pyproject.toml` requires `homeassistant>=2026.9.3`. HACS therefore offers the integration to older installations, which then fail with a `SyntaxError`.

**Recommendation:** Enter the minimum version that was actually tested in `hacs.json`, and test against exactly this version in CI.

### R13 Setup and teardown incomplete

🟡 Low · ✅ verified

- [`__init__.py:116-118`](../custom_components/findmy/__init__.py#L116-L118): The result of `async_unload_platforms` is discarded (`_ =`). If unloading fails, the integration reports success anyway. In addition the account is closed **before** the platforms.
- `RuntimeStorage.detach()` is never called. The coordinator and timer (`_refresh_handle`) remain after the last entry is unloaded. A pending `call_later` can still trigger a fetch after unloading.
- `local_observations`, `local_status` and `local_rssi` are not cleaned up on unload.
- [`__init__.py:70`](../custom_components/findmy/__init__.py#L70): `async_setup` is annotated with `ConfigEntry` instead of `ConfigType`, and `CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)` is missing.

### R14 HTTP sessions in the config flow are never closed

🟡 Low · ✅ verified · [`config_flow.py:268`](../custom_components/findmy/config_flow.py#L268), [`:479`](../custom_components/findmy/config_flow.py#L479)

The `AsyncAppleAccount` of the flow is never closed with `close()` after `to_json()`, on an abort or on errors. The consequences are "Unclosed client session" warnings and open sockets, also on every retry after a wrong password, because a new account is created each time.

**Recommendation:** `try/finally` around the login and override `async_remove()` in the flow to clean up.

### R15 Filter `rolling` in the delete service has no effect

🟡 Low · ✅ verified · [`services.py:50`](../custom_components/findmy/services.py#L50)

The filter checks `entry_type == "device_rolling"`. Since migration v2, however, the types are called `device_rolling_derived` and `device_rolling_pre_generated`. The docstring also mentions the non-existent type `openhaystack`.

### R16 "Next fetch" sensor calculated incorrectly

🟡 Low · ✅ verified · [`coordinator.py:143-150`](../custom_components/findmy/coordinator.py#L143-L150)

The code calculates `last + 15 min × number of accounts`. The correct value is `last + 15 min`: The interval is `15 min / n`, and with round robin over `n` accounts every account is due every 15 minutes. With three accounts the sensor therefore shows 45 instead of 15 minutes.

### R17 Fetch log grows without bound

🟡 Low · ✅ verified · [`coordinator.py:84`](../custom_components/findmy/coordinator.py#L84), [`:194-198`](../custom_components/findmy/coordinator.py#L194-L198)

`self._logs` is a list without an upper limit: around 200 entries per day and account. Every sensor access searches it linearly. Over months the memory requirement grows and the accesses become slower.

**Recommendation:** Keep the state per account in a small dataclass (`last_start`, `last_end`, `last_result`, `last_error`) instead of keeping a log.

### R18 Config entry is rewritten on every fetch

🟡 Low · ✅ verified · [`device_tracker.py:477-486`](../custom_components/findmy/device_tracker.py#L477-L486)

`_handle_coordinator_update` calls `_update_entry()` for **every** device. For rolling-key accessories the alignment changes, so `core.config_entries` is rewritten on every fetch and for every device. This does not fit the throttling to one hour that the Bluetooth path (`_LOCAL_ALIGNMENT_SAVE_DELAY`) deliberately introduces. On SD cards it increases wear.

**Recommendation:** Save centrally and throttled, and only if the data has changed.

### R19 Blueprint `tag_stale` checks only once per hour

🟡 Low · ✅ verified · [`blueprints/automation/tag_stale.yaml:52-62`](../blueprints/automation/tag_stale.yaml#L52-L62)

- `time_pattern` with `minutes: 30` fires **only at minute 30** of every hour, not every 30 minutes. The correct value would be `"/30"`, and that cannot be mapped directly with a number input.
- The first `condition` is dead code: It always returns `true` and refers to `trigger.event_data`, which does not exist for `time_pattern`.
- Without a marker, a notification is sent again on every trigger as long as the tag is stale.
- The syntax is outdated (`trigger:`, `platform:`, `service:`). Since Home Assistant 2024.10 it is `triggers:`, `trigger:` and `action:`.

---

## 5. Findings: maintainability and documentation

| ID | Finding | Location | Recommendation |
|---|---|---|---|
| W1 | **Entry types as strings** (`"account"`, `"device_static"` …), spread over `storage.py`, `services.py`, `device_tracker.py` and `__init__.py`. R15 arose from exactly this. | several | `StrEnum EntryType` in `const.py`, plus a registry `EntryType → (class, serialisation)` |
| W2 | **Duplicated logic:** `unique_id` and `device_info` of the tracker duplicate `_entity.device_unique_id` and `build_device_info`. | [`device_tracker.py:337-414`](../custom_components/findmy/device_tracker.py#L337-L414) | Common base class `FindMyDeviceEntity` for all device entities |
| W3 | **Three base classes with the same mechanics** (coordinator listener, dispatcher, cache, `available`) in `sensor.py` and `binary_sensor.py`. The override `available` with `functools.cached_property` only works because Home Assistant clears the cache when `_attr_available` is set (verified by test), and is therefore fragile. | [`sensor.py:101-179`](../custom_components/findmy/sensor.py#L101-L179), [`binary_sensor.py:58-136`](../custom_components/findmy/binary_sensor.py#L58-L136) | Use `CoordinatorEntity` and use `EntityDescription` with `value_fn`; this reduces the sensor classes to data descriptions |
| W4 | **Tracker with two responsibilities:** `FindMyDeviceTracker` contains around 250 lines of Bluetooth logic. Presence and battery sensors depend on the tracker entity being enabled. If it is disabled, there is no local data. | [`device_tracker.py:118-327`](../custom_components/findmy/device_tracker.py#L118-L327) | A separate `LocalBluetoothManager` per accessory or centrally for all, independent of entities, with one shared callback instead of three per accessory |
| W5 | **Dead or wrong code:** `RuntimeStorage.static_tags` returns accounts instead of tags. | [`storage.py:76-78`](../custom_components/findmy/storage.py#L76-L78) | Remove |
| W6 | **Inconsistent battery documentation:** The docstring says "0/50/70/90", the code uses 90/65/40/15. For `local_battery` the README gives the values "Full/Medium/Low/Very Low", the sensor `ok/medium/low/critical`. | [`device_tracker.py:392-396`](../custom_components/findmy/device_tracker.py#L392-L396), [README](../README.md) | One scale and one documentation |
| W7 | **Comments missing** in `coordinator.py`, `storage.py`, `__init__.py`, `config_flow.py` (no module docstrings, no rationale). Ruff ignores `D` entirely. | – | Enable Ruff `D` with the Google convention, initially only `D1xx` for public classes and functions |
| W8 | **Hard-coded English texts** in the UI: menu options (`"Apple Account"`, `"FindMy Device"`), 2FA labels, entry titles. There is no German translation. | [`config_flow.py:218-221`](../custom_components/findmy/config_flow.py#L218-L221), [`:309`](../custom_components/findmy/config_flow.py#L309) | Use translation keys and add `translations/de.json` |
| W9 | **Enum sensors without `options`** (`State`, `Last fetch result`). This is allowed, but then the UI offers no selection in automations, and no translations are used. | [`sensor.py:349-399`](../custom_components/findmy/sensor.py#L349-L399) | Set `options` and `translation_key` |
| W10 | **Manual `entity_id`s** via `generate_entity_id` contradict `has_entity_name` and make renaming harder. | [`device_tracker.py:112-116`](../custom_components/findmy/device_tracker.py#L112-L116), [`sensor.py:296-300`](../custom_components/findmy/sensor.py#L296-L300) | Let Home Assistant generate the IDs (note: for existing installations a registry migration is needed, otherwise the IDs change) |

---

## 6. Dependencies: current state and reduction

> **Verification:** This chapter is replaced by [chapter 8 of the strategy](REFACTORING_STRATEGY.md#8-dependencies). It contains the verified facts, alternatives and the recommendation. The most important correction: `fs` is not imported anywhere and is **not** a failure risk. The affected places below have been corrected accordingly.

### Current state

**Runtime** (installed by Home Assistant via `manifest.json`):

| Package | Origin | Already in Home Assistant? | Purpose | Size |
|---|---|---|---|---|
| `FindMy==0.10.2` | direct | no | Apple protocol, keys, reports | 0.2 MB |
| `srp` (+ `six`) | FindMy | no | SRP login at Apple | < 0.1 MB |
| `beautifulsoup4` (+ `soupsieve`) | FindMy | no | Extract phone numbers for SMS 2FA from HTML | 0.8 MB |
| `anisette` | FindMy | no | Local Anisette generation | 0.1 MB |
| ↳ `unicorn` | anisette | no | CPU emulator for Apple's ARM libraries | **58 MB**, native |
| ↳ `pyelftools` | anisette | no | ELF parser | 0.7 MB |
| ↳ `fs` (+ `appdirs`, `setuptools`, `six`) | anisette | no | Virtual file system | 0.6 MB |
| `cryptography`, `aiohttp`, `bleak`, `certifi`, `urllib3`, `typing-extensions` | FindMy/anisette | yes | – | – |
| Integration `bluetooth` | `manifest.json` → `dependencies` | yes (component) | local detection | – |

**Development** (`pyproject.toml`): `homeassistant` (≈ 130 packages), `basedpyright`, `ruff`, `pre-commit`.

### Risks in the current state

- **`fs` 2.4.16 is a legacy burden, but not a failure risk (corrected).** The package has had no release since 2022 and uses `pkg_resources`. However, `anisette` 1.2.4 only lists it in the metadata and no longer imports it. Verified by test: `anisette` and `findmy` also load when `fs`, `appdirs` and `pkg_resources` are blocked. The first version of this review wrongly warned of a total failure here.
- **`bleak` is the actual coupling risk.** `FindMy` requires `bleak<3.1.0`, Home Assistant pins `bleak==3.0.2`. If Home Assistant raises this to 3.1, `FindMy==0.10.2` can no longer be installed.
- **`unicorn`** is the largest package at 58 MB. It needs native wheels for every architecture and is the reason for long installation times on ARM devices such as the Raspberry Pi.
- **Duplicate version specifications:** `manifest.json` pins `FindMy==0.10.2`, `pyproject.toml` allows `findmy>=0.10.2`. The development environment can thus differ from what users get.
- **`homeassistant` as a runtime dependency** in `[project].dependencies`. For a custom integration Home Assistant provides the environment, so the package belongs in the dev group.

### Reduction options

| # | Measure | Savings | Effort | Depends on |
|---|---|---|---|---|
| A1 | **Make Bluetooth optional** (R11): `after_dependencies`, load Bluetooth code lazily | The hard dependency on the Bluetooth component together with `habluetooth`, `dbus-fast`, `bluetooth-adapters` and USB detection goes away | medium | integration only |
| A2 | **Make `anisette` optional in FindMy.py** (upstream PR): `findmy[local-anisette]` as an extra; `LocalAnisetteProvider` imports `anisette` only when needed. Today FindMy imports it at module level (`reports/anisette.py:17`). | ≈ 60 MB (`unicorn`, `pyelftools`, `fs`, `appdirs`) and the `pkg_resources` risk, for all users with an Anisette server | small upstream, then trivial | upstream FindMy.py |
| A3 | **Remove `fs` from the dependencies of `anisette`** (upstream PR, corrected): The package is no longer imported | `fs`, `appdirs` | one line | upstream anisette |
| A4 | **Replace `beautifulsoup4` in FindMy.py** (upstream): The phone numbers are in a `<script>` tag as JSON, a targeted regex or `html.parser` from the standard library is enough | 0.9 MB, 2 packages | small | upstream FindMy.py |
| A5 | **Move `homeassistant` to the dev group**, better still replace it with `pytest-homeassistant-custom-component`; that pins HA appropriately and brings the test fixtures along | cleaner semantics, reproducible tests | small | integration only |
| A6 | **Unify version specifications:** `findmy==0.10.2` in `pyproject.toml`, check against `manifest.json` by script in CI | prevents deviations | small | integration only |
| A7 | Keep `pre-commit`. It is only a dev tool, and the gain from removing it would be small. The GitHub action `pre-commit-ci/lite-action`, however, requires the pre-commit.ci app; without it the step is useless and can be dropped. | – | – | – |

**Recommendation:** Implement A1, A5 and A6 immediately in the fork. Submit A2 to A4 as issues or PRs to `malmeloo/FindMy.py` and `malmeloo/anisette`. This benefits all users, and the fork does not have to maintain a library. A separate fork of FindMy.py is **not** recommended: The Apple protocol keeps changing, and a fork would quickly become outdated.

---

## 7. Development environment, tests and CI

| Topic | Current state | Recommendation |
|---|---|---|
| **Tests** | There are none. | Start with `pytest-homeassistant-custom-component`. First `local_bluetooth.py` (pure functions), then the config flow (mocked FindMy classes), then the coordinator (error paths R1–R5). Goal: Every finding from chapters 3 and 4 gets a regression test. |
| **Importing HA** | With `mashumaro` 3.16, **every** import from the core of Home Assistant failed under Python 3.14 (`webrtc_models`). **Fixed** in `a46c8a9` (update to 3.23). | Run an import smoke test in CI after `uv lock` updates. |
| **Bluetooth on Windows** | `homeassistant.components.bluetooth` cannot be imported locally (`aiousbwatcher` is missing, Linux-specific). | Run tests that need Bluetooth in CI on Linux or locally in WSL or in the devcontainer. With A1, tests without Bluetooth can be run on Windows. |
| **Docker Compose** | Mounts `/run/dbus` and `/etc/localtime`, which do not exist on Docker Desktop for Windows. | An override `docker-compose.windows.yml` without these mounts, or a devcontainer. |
| **CI** | Runs only `pre-commit`. | Add `hassfest` (`home-assistant/actions/hassfest`) and `hacs/action` (validation), plus `pytest`. Test against the minimum version from `hacs.json`. |
| **Release** | `manifest.json` permanently says `v0.0.1`. | Version it and publish releases via GitHub releases (HACS displays these). |

---

## 8. Refactoring plan

The order is chosen so that every phase can be delivered on its own and the most dangerous items are closed first. Every phase ends with green checks and a commit.

### Phase 0: Test base (prerequisite)

- Introduce `pytest-homeassistant-custom-component` as a dev dependency (A5), `tests/` with `conftest.py` and fixtures for mocked `AsyncAppleAccount`, `KeyPair` and `FindMyAccessory`.
- First tests for `local_bluetooth.py` and `_entity.py` (pure functions, no mocks needed).
- Extend CI with `pytest`, `hassfest` and the HACS validation.

### Phase 1: Immediate measures (small changes, big effect)

| Finding | Change |
|---|---|
| S2 | Central redaction of all flow logs |
| S3 | Admin service, no default `all`, confirmation field |
| R2 | Fix the `success` flag, error mapping to `UpdateFailed` |
| R3 | `coordinator.reload()` after unload |
| R6 | Fix `step_id`, catch `IndexError`, catch network errors, check `LOGGED_IN` |
| R7 | Check that the `unique_id` matches on re-auth |
| R12 | Fix `hacs.json` |
| R15, R16 | Fix the filter and the calculation |
| R19 | Repair the blueprint |
| S6 | Remove `state_class` for lat/lon |

### Phase 2: Sessions and rate limit (core of stability)

Target architecture: **`entry.runtime_data` instead of global `hass.data`**, and **one fetch scheduler per account**.

```
ConfigEntry (account)  ── runtime_data ──►  AccountRuntime
                                            ├─ account: AsyncAppleAccount
                                            ├─ state: AccountFetchState (last_start/end/result/error)  ← persisted (Store)
                                            └─ entry (for re-auth & token persistence)

FindMyHub (single instance, hass.data[DOMAIN], with clean setup and teardown)
  ├─ accounts: dict[entry_id, AccountRuntime]
  ├─ devices:  dict[entry_id, FindMyDevice]
  ├─ scheduler: picks the account with the oldest fetch, provided it is ≥ 15 min ago; otherwise waits
  └─ coordinator: DataUpdateCoordinator (holds only location data, config_entry=None is then correct,
                  auth errors are reported per account via AccountRuntime.entry.async_start_reauth)
```

- R1: Re-auth targeted at the affected account, other accounts keep running.
- R4: Rate limit per account, persisted across restarts. Manual refreshes are combined.
- R5 and S1: Save the session after every change, without the password. A migration removes stored passwords.
- R10: Validate the data at setup; raise `ConfigEntryAuthFailed` if the account is not signed in.
- R13, R14, R17: clean lifecycle, close sessions, log replaced by a state.
- S4: `anisette_libs_path` with hash check.

### Phase 3: Structure and maintainability

- W1: `EntryType` enum and registry for serialisation.
- W2 and W3: Base class `FindMyDeviceEntity(CoordinatorEntity)` and sensors via `EntityDescription` with `value_fn`.
- W4 and R11: Bluetooth as a separate, optional service (`local_bluetooth_manager.py`) with **one** callback for all accessories, decoupled from the tracker entity.
- R8 and R9: `unique_id`s for device entries and account devices, with migration (config entry version 2.1).
- R18: central, throttled saving.
- W7 to W10: docstrings (Ruff `D1`), translations including German, enum options.

### Phase 4: Hardening and privacy

- S5: Plausibility check of the alignment, warning in the documentation.
- S7: Masking of personal data.
- `diagnostics.py` with redaction (see chapter 9).
- Submit the dependency PRs A2 to A4 upstream.

---

## 9. Outlook for further development

These extensions follow directly from the findings and build on the architecture from phases 2 and 3:

| Idea | Link | Benefit |
|---|---|---|
| **Diagnostics** (`diagnostics.py`) | S1, S2, R2 | Users can attach a diagnostics download to issues, with secrets redacted, instead of copying debug logs. |
| **Repairs notices** (`issue_registry`) | R1, S4, R11 | Visible notices in Home Assistant, such as "Anisette server unreachable", "Account X needs to sign in again", "Bluetooth unavailable, local detection disabled". |
| **Sensor for account status** (signed in / re-auth needed / locked / Anisette error) | R1, R2, W9 | The real cause of the error instead of just `idle`/`fetching`; this allows building automations. |
| **Location history** via `fetch_location_history` (FindMy supports up to 7 days) | R4 | Close gaps after outages or restarts; paths on the map. Only within the rate limit per account. |
| **Configurable polling interval** per account (≥ 15 min) | R3, R4 | Whoever needs updates only rarely spares the account. |
| **Re-import / key export** for devices | S3 | Replacement for the delete service as a tool for bulk changes, with a backup. |
| **Central Bluetooth scanner** with statistics (accessories seen, hit rate) | W4, S5 | Better diagnosis of local detection; a basis for an event such as "unknown tracker is following me" (DULT anti-stalking). |
| **Events** (`findmy_location_updated`, `findmy_accessory_seen`) | W4 | Automations without polling attributes; makes the blueprint `tag_stale` simpler. |
| **Multilingual support** (German first) | W8 | – |

---

## 10. Appendix: Verified facts about Home Assistant and FindMy.py

These facts support the findings. They were checked against the installed versions (Home Assistant 2026.9.3, FindMy 0.10.2, anisette 1.2.4) and should be checked again after version jumps.

| Fact | Source |
|---|---|
| `DataUpdateCoordinator` without `config_entry` uses `config_entries.current_entry.get()`. Re-auth only starts if `self.config_entry` is set. | `homeassistant/helpers/update_coordinator.py:100-110`, `:538-554` |
| After `ConfigEntryAuthFailed` the coordinator schedules no further fetch (`if not auth_failed and self._listeners …: self._schedule_refresh()`). Corrected: The first version spoke of listeners that were not notified. | `helpers/update_coordinator.py:585`, test |
| By default the coordinator debounces `async_request_refresh` with only a 10 s cooldown. | `helpers/update_coordinator.py:35-36` |
| `process_uploaded_file` returns a `Path` and deletes the file after the context. | `components/file_upload/__init__.py:36-57` |
| Enum sensors without `options` are permitted; with `options` the value is validated. | `components/sensor/__init__.py:697-712` |
| Setting `_attr_available` also clears a `functools.cached_property` named `available` defined in the subclass. | Test as part of the review |
| `AsyncAppleAccount.to_json()` contains `account.password` in plain text. | `findmy/reports/account.py:439-455` |
| `_gsa_authenticate` stores username and password for later silent re-sign-ins. | `findmy/reports/account.py:799-802` |
| After an HTTP 401 during a fetch, FindMy signs in again automatically and renews the tokens in the object. | `findmy/reports/account.py:690-700` |
| The fetch retries empty responses up to five times (2–10 s backoff) and then raises `EmptyResponseError`. | `findmy/reports/account.py:650-688` |
| The HTTP session of FindMy has a total timeout of 5 s. | `findmy/util/http.py:102` |
| `__hash__` and `__eq__` of `FindMyAccessory`, `FixedRollingKeyPairAccessory` and `KeyPair` depend only on immutable keys, not on the alignment. The devices are therefore safe as dict keys. | `findmy/accessory.py:214-222`, `:529-542`, `findmy/keys.py:63-67` |
| `FindMyAccessory.from_plist` raises `KeyError` on missing fields. | `findmy/accessory.py:412-440` |
| `LocalAnisetteProvider` downloads the libraries on every initialisation without `libs_path`, from `anisette.dl.mikealmel.ooo`, without timeout, status check or hash check. | `findmy/reports/anisette.py:318-370`, `anisette/anisette.py:26`, `anisette/_util.py:44-50` |
| `fs` 2.4.16 calls `pkg_resources.declare_namespace`, but is not imported by `anisette` 1.2.4 and `findmy` 0.10.2. | `fs/__init__.py:4`, import test |
| `FindMyAccessory.update_alignment` discards calls with an older date or a smaller index. | `findmy/accessory.py:368-381`, test |
| Per accessory and fetch, FindMy derives 2,016 key pairs synchronously (3 per index, 672 indices). | Profile, see strategy chapter 3 |
| Home Assistant pins `bleak==3.0.2`, `mashumaro>=3.17.0` and `setuptools<82.0.0`. | `homeassistant/package_constraints.txt` |
