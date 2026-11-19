# Implementation status

Where the refactoring stands. The finding IDs (S1, R1, …) come from the
[code review](CODE_REVIEW.md) and the [refactoring strategy](REFACTORING_STRATEGY.md).

Last updated: 2026-11-19, after locations became restorable across restarts.

## Done

| Finding | What changed |
|---|---|
| S1 | The Apple ID password is no longer stored. New and re-authenticated entries are written without it, existing entries are cleaned by a migration (entry version 2.1), and an account restored from an entry holds no password in memory. When a session expires, the user signs in again through re-authentication. |
| S2 | Config flow logs go through a central redaction; passwords, private keys, 2FA codes and upload ids never reach a log. Credentials inside an Anisette URL are stripped as well. |
| S3, R15 | `findmy.delete_devices` is admin-only, requires `filter` and `confirm: true`, and its `rolling` filter matches both rolling device types again. |
| S6 | Latitude and longitude sensors no longer have a state class, so coordinates are not kept as long-term statistics. Existing statistics have to be removed by hand (Developer tools → Statistics). |
| R1 | A failed sign-in starts re-authentication for the entry of that account. The account rests until its entry is reloaded; the other accounts keep being fetched. |
| R2 | Fetch failures are recorded as failures and reported as `UpdateFailed` with a short message; the traceback is kept at debug level. |
| R3 | The fetch schedule is recalculated when an account entry is unloaded. |
| R4 | The limit of one fetch per account per 15 minutes is stored (`budget.py`) and enforced at the one place a fetch starts. Restarts, reloads, re-authentication and manual refreshes can no longer exceed it. Entities no longer trigger a fetch through `homeassistant.update_entity`. |
| R5 | Session data renewed during a fetch is written back to the config entry, only when it changed. |
| R6, R7, R14 | Config flow: retryable form errors instead of tracebacks, re-authentication rejects a different account, and the flow closes its Apple account sessions. |
| R10 | Account data that cannot be restored fails the setup of that entry with a clear message; an account that is not signed in starts re-authentication. A failed setup leaves nothing behind. |
| R12 | `hacs.json` states the Home Assistant version the tests run against. |
| R16 | The "Next fetch" sensor is correct with several accounts. |
| R17 | The fetch log is gone; each account keeps the state of its latest fetch. |
| R19 | The stale-tag blueprint warns once per stale period and uses the current automation syntax. |
| R20 | Derived key pairs are cached and derived outside the event loop, and every call into the FindMy library runs on a worker thread: stored accounts on a shared one (`worker.py`, `account.py`), sign-in and 2FA on one per config flow. Measured on a desktop PC: a fetch with one accessory occupies the event loop for 5 ms instead of 930 ms; 500 ms of synchronous work inside a fetch or a sign-in stalls the loop for 22 to 38 ms. |
| R21 | A fetch searches at most one week of key indices per accessory (three requests). Accessories that fell below that window are caught up on in hourly windows. |
| W5 | Dead code removed (see commit `c150e57`). |
| — | `file_upload` is declared as a dependency (found by hassfest). |
| — | A malformed nearby advertisement can no longer raise inside the Bluetooth callback. |
| — | The latest location of each device is kept in `.storage/findmy.last_reports` (`last_reports.py`) and restored when the device is set up, so trackers and location sensors have a location between a restart and the first fetch the limit allows. Only time, position, accuracy and status byte are stored, no keys. |
| — | Test suite (422 tests), CI for tests, hassfest and HACS validation, and a check that keeps version pins consistent. |

## Partly done

| Finding | Done | Open |
|---|---|---|
| R13 | `CONFIG_SCHEMA` declared; schedule recalculated on unload; entities are unloaded before their account is closed on the worker; the worker stops with Home Assistant. | The shared storage object is never torn down, and the delayed refresh after setup can still fire after the last unload. |
| R22 | A sign-in that ends without the Find My token shows a dedicated error in the config flow and creates no entry. | No specific state or repair hint for accounts that are already set up. The handling follows upstream reports and the library source; it could not be verified against Apple. |
| S5 | Realignment refreshes are debounced and only run when the alignment really moved. | Plausibility check for alignments taken from Bluetooth; documentation that presence is not a security feature. |
| S7 | E-mail addresses are masked in the config flow logs. | The FindMy library logs the user name at info level; device names still contain name and e-mail. |

## Open

| Finding | Summary |
|---|---|
| S4 | Local Anisette downloads its libraries on every start, without an integrity check. |
| R8, R9 | Device entries have no unique id; account devices are keyed by the local part of the e-mail address. |
| R11 | Bluetooth is a hard dependency although local detection is optional. |
| R18 | The config entry is rewritten on every fetch that moves an alignment. |
| W1–W4, W6–W10 | Entry type enum, shared entity base classes, Bluetooth manager, documentation, translations. |
| new | `FindMyDeviceTracker` overrides the deprecated `battery_level` property, which Home Assistant stops supporting in 2027.7. |
| new | The account sensors do not show that an account waits for re-authentication. |

## Not verified

- Nothing was tested against Apple's servers. All tests use mocks of the FindMy library.
- The blueprints load and validate, but were not triggered in a running Home Assistant.
- The integration was not run inside a real Home Assistant instance after these changes.
- Whether the integrated Anisette emulation changes its serialised state on every fetch is unknown. If it does, the config entry of such an account is rewritten after every fetch.
- The tests ran on Windows and, in CI, on Linux; never on ARM hardware.
