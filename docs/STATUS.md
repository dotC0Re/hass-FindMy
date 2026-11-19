# Implementation status

Where the refactoring stands. The finding IDs (S1, R1, …) come from the
[code review](CODE_REVIEW.md) and the [refactoring strategy](REFACTORING_STRATEGY.md).

Last updated: 2026-11-19, commit `c150e57`.

## Done

| Finding | What changed |
|---|---|
| S2 | Config flow logs go through a central redaction; passwords, private keys, 2FA codes and upload ids never reach a log. Credentials inside an Anisette URL are stripped as well. |
| S3, R15 | `findmy.delete_devices` is admin-only, requires `filter` and `confirm: true`, and its `rolling` filter matches both rolling device types again. |
| S6 | Latitude and longitude sensors no longer have a state class, so coordinates are not kept as long-term statistics. Existing statistics have to be removed by hand (Developer tools → Statistics). |
| R1 | A failed sign-in starts re-authentication for the entry of that account. The account rests until its entry is reloaded; the other accounts keep being fetched. |
| R2 | Fetch failures are recorded as failures and reported as `UpdateFailed` with a short message; the traceback is kept at debug level. |
| R3 | The fetch interval is recalculated when an account entry is unloaded. |
| R6, R7, R14 | Config flow: retryable form errors instead of tracebacks, re-authentication rejects a different account, and the flow closes its Apple account sessions. |
| R12 | `hacs.json` states the Home Assistant version the tests run against. |
| R16 | The "Next fetch" sensor is correct with several accounts. |
| R17 | The fetch log is bounded (ten entries per account). |
| R19 | The stale-tag blueprint warns once per stale period and uses the current automation syntax. |
| R20 | Derived key pairs are cached and warmed up outside the event loop. Measured for one accessory on a desktop PC: a fetch occupies the event loop for 5 ms instead of 930 ms. |
| R21 | A fetch searches at most one week of key indices per accessory (three requests). Accessories that fell below that window are caught up on in hourly windows. |
| W5 | Dead code removed (see commit `c150e57`). |
| — | `file_upload` is declared as a dependency (found by hassfest). |
| — | A malformed nearby advertisement can no longer raise inside the Bluetooth callback. |
| — | Test suite (239 tests), CI for tests, hassfest and HACS validation, and a check that keeps version pins consistent. |

## Partly done

| Finding | Done | Open |
|---|---|---|
| R4 | Entities no longer trigger a fetch through `homeassistant.update_entity`. | The 15 minute limit is still not stored across restarts or enforced per account. |
| R13 | `CONFIG_SCHEMA` declared; interval reload on unload. | The shared storage object is never torn down. |
| R20 | Stages 1 and 3 (key cache, bounded search). `worker.py` exists and is tested. | The worker is not wired in yet, so report decryption, sign-in and Anisette emulation still run on the event loop. |
| S5 | Realignment refreshes are debounced and only run when the alignment really moved. | Plausibility check for alignments taken from Bluetooth; documentation that presence is not a security feature. |
| S7 | E-mail addresses are masked in the config flow logs. | The FindMy library logs the user name at info level; device names still contain name and e-mail. |

## Open

| Finding | Summary |
|---|---|
| S1 | The Apple ID password is stored in plain text in the config entry. |
| S4 | Local Anisette downloads its libraries on every start, without an integrity check. |
| R5 | Renewed session data is not written back to the config entry. |
| R8, R9 | Device entries have no unique id; account devices are keyed by the local part of the e-mail address. |
| R10 | Broken entry data and signed-out accounts are not detected at setup. |
| R11 | Bluetooth is a hard dependency although local detection is optional. |
| R18 | The config entry is rewritten on every fetch that moves an alignment. |
| R22 | Apple's sign-in change of October 2026 is not handled specifically. New sign-ins are expected to fail until FindMy.py is fixed upstream; this has not been verified here. |
| W1–W4, W6–W10 | Entry type enum, shared entity base classes, Bluetooth manager, documentation, translations. |
| new | `FindMyDeviceTracker` overrides the deprecated `battery_level` property, which Home Assistant stops supporting in 2027.7. |

## Not verified

- Nothing was tested against Apple's servers. All tests use mocks of the FindMy library.
- The blueprints load and validate, but were not triggered in a running Home Assistant.
- The integration was not run inside a real Home Assistant instance after these changes.
