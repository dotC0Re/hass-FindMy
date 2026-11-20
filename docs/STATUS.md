# Implementation status

Where the refactoring stands. The finding IDs (S1, R1, …) come from the
[code review](CODE_REVIEW.md) and the [refactoring strategy](REFACTORING_STRATEGY.md).

Last updated: 2026-11-20, after the fixes from the second external review (findings A-1 to A-20).

## Done

| Finding | What changed |
|---|---|
| S1 | The Apple ID password is no longer stored. New and re-authenticated entries are written without it, existing entries are cleaned by a migration (entry version 2.1), and an account restored from an entry holds no password in memory. When a session expires, the user signs in again through re-authentication. |
| S2 | Config flow logs go through a central redaction; passwords, private keys, 2FA codes and upload ids never reach a log. Credentials inside an Anisette URL are stripped as well, also where aiohttp quotes the URL in an error (review M6). Since the second review this holds for the error text of a failed fetch too, and a filter on the loggers of FindMy.py and of the integration redacts them in every log line and traceback (review A-3). A URL whose password contains an unencoded `/`, `?` or `#` is refused when it is entered. |
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
| review H1 | An accessory whose index fell more than a week behind the clock (a battery that was empty for a while) is recognised locally again: the Bluetooth matching also gets the keys above the last alignment, up to 90 days of indices, derived once and in steps. Before, only a location report could bring such an accessory back, which a setup without an Apple account never gets. |
| review H2 | A failed fetch no longer makes trackers, location sensors and battery entities unavailable. A device stays available while anything is known about it: a report (also a restored one) or a local sighting, including DULT-only ones. The state of the fetch is what the account sensors show. |
| review M1 | A refresh without any loaded account keeps the data instead of emptying it, so restored locations survive when the account is disabled or failed to set up. |
| review M2 | The stored password is also removed from account entries Home Assistant never migrates (disabled ones), when the integration is set up. |
| review G11 | Removing a device while the integration is not loaded still drops its stored location. |
| S4 (review M3) | The libraries of the integrated Anisette provider are downloaded once, by the integration itself with a time limit, kept under `.storage/findmy.anisette_libs` and verified against a checksum recorded at the first download (trust on first use) before every use. A damaged file is replaced only by a download that matches the recorded checksum; anything else is refused with a message naming the file to delete. |
| review A-1 | That checksum covers the files inside the bundle, not the bytes of the file, so a bundle that is packed differently (another `anisette` or Python release re-saving it) keeps matching instead of locking the account. `anisette` is pinned to `1.2.4` in the manifest as well. A record written by 2026.11.0 is still honoured and renewed. |
| review A-2 | The `anisette` package, whose debug lines dump the provisioning secrets of the emulated device, is no longer covered by "Enable debug logging" or by a default log level of debug. Only setting the `anisette` logger to debug by name shows them. |
| review H3, G7 | A re-authentication signs in with the device ids and the Anisette state the entry already has instead of as a new device, and its form starts with the remote Anisette URL of the entry. FindMy.py offers nothing cheaper than a full sign-in for an expired session. |
| review M4, G6 | Key derivation runs in short slices with pauses, never for two accessories at once, and the key chain is walked from checkpoints kept in `.storage/findmy.key_checkpoints` instead of from the pairing after every restart. Each checkpoint is bound to its accessory with an HMAC and dropped if it does not verify. Measured on a desktop with four accessories paired 900 days ago: a TCP ping-pong on the event loop keeps about a third of its idle throughput during a cold refresh, instead of 0.04 %. |
| review M7 | The tracker no longer overrides the deprecated `battery_level`. The battery sensors of the device carry the value. |
| review M8 | Entities are tested through Home Assistant's real entity platforms. A contract test pins every entity id, unique id, device identifier, class, unit and attribute key; compared with the original integration, only the `state_class` of latitude/longitude and the tracker's `battery_level` attribute differ. |
| review G1 | The coordinator shuts down with Home Assistant; no refresh starts while it stops. A fetch counts against the limit once its job starts on the worker, not if it never left Home Assistant. At most one fetch is prepared or running at a time. |
| review G2 | Unloading an account cancels a fetch running with that account instead of waiting up to 120 s for it. |
| review G3 | The delayed first refresh is a background task, so the start of Home Assistant does not wait for a fetch. |
| review G5 | A device entry reloaded during a fetch gets the report and the alignment of that fetch. |
| — | Found by the new entity tests: a local alignment is saved also within the first hour after the host booted; the signal strength sensor no longer sticks to an old value; a reloaded entry keeps signal strength and source. |
| review A-4 | The presence tests prepare restored states through Home Assistant's store instead of a private attribute that 2026.11 renamed. CI runs the suite against the locked Home Assistant 2026.9.3 and, in a second job, against 2026.11.1. |
| review A-5 | A fetch that was cutting the search window of a device while the device was removed no longer brings its key chain checkpoints back. |
| review A-9, A-15, A-18, A-19 | Wording: disabled entries lose their stored password once any entry of the integration is loaded; checkpoints change hourly with an account; the event loop does take the derivation lock briefly; the checkpoint file together with one recorded advertisement is enough to recognise the accessory later. |
| review G15 | The manifest carries a version of the fork's own (year.month.patch, currently `2026.11.1`), so that it can be told apart from the original integration (`v0.0.1`). |
| — | Test suite (659 tests), CI for tests, hassfest and HACS validation, and a check that keeps version pins consistent. |

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
| R8, R9 | Device entries have no unique id; account devices are keyed by the local part of the e-mail address. |
| R11 | Bluetooth is a hard dependency although local detection is optional. |
| R18 (review G4) | The config entry is rewritten on every fetch that moves an alignment; estimated at 100 to 200 writes a day with four tags and an account. |
| review H3 (rest) | Every expired session still needs a re-authentication with password and 2FA; that is the price of not storing the password. An identity Apple no longer accepts can only be replaced by removing the account and adding it again. |
| review M4 (rest) | Report decryption and the Anisette emulation run inside library code on the worker thread and are not paced. Only a separate process would isolate them; the measurements do not call for it so far. A catch-up window whose fetch was cancelled is searched again an hour later, not at once. |
| review M5 | Going back to the original integration: an entry without a password works there until the session expires, then fails with a traceback on every interval instead of asking to sign in. Documented in the README; restoring a backup is the clean way back. |
| review G10 | Session tokens remain in plain text in the config entry and in backups. Unavoidable without the password, but not documented beyond the README note. |
| review G14, A-20 | CI does not pin actions by commit, and the Home Assistant version of its second job has to be bumped by hand. There are no GitHub releases yet; HACS installs the state of `main`. |
| review A-6, A-8, A-16 | Local gap keys: an index just below the last alignment is not recognised at once; an accessory that only sends secondary keys keeps its gap and drops out after 90 days; a gap of more than 7 days is derived step by step (672 indices per 15 minutes). |
| review A-7, A-10 | Availability follows the last refresh, not the last fetch: after a reload with failing fetches the tracker is unavailable until the next advertisement although a sighting is stored, and a refresh the budget turned away counts as a success. The delayed refresh after setup logs an error while every account waits for re-authentication. |
| review A-11, A-12 | A stored Anisette state that is valid base64 but no archive, or a bundle read while another provider rewrites it, ends a sign-in with "unknown error" instead of one retry with a fresh provider. |
| review A-13, A-14 | The size limit of the bundle download does not bound the memory its conversion needs, and the first download is trusted without a reference checksum shipped with the integration. Both need a compromised server or TLS chain. |
| review A-17 | A cancelled key derivation runs to its end in the executor; a reload during a cold start derives twice. |
| W1–W4, W6–W10 | Entry type enum, shared entity base classes, Bluetooth manager, documentation, translations. |
| new | The account sensors do not show that an account waits for re-authentication. |

## Not verified

- Nothing was tested against Apple's servers. All tests use mocks of the FindMy library.
- The blueprints load and validate, but were not triggered in a running Home Assistant.
- One real installation runs the integration since 2026-11-20: version 2026.11.1 (`3cc0af7`) on Home Assistant Core 2026.11.2, four AirTags tracked locally, no account. Nothing that needs an account has run on a real system: sign-in, re-authentication, fetching, the download of the Anisette bundle. Neither has the migration of existing entries, because the switch there went through deleting and re-adding them (see the README).
- The steps for switching through Home Assistant's integration marketplace in the README are what that one installation reported; they were not reproduced.
- Whether the integrated Anisette emulation changes its serialised state on every fetch is unknown. If it does, the config entry of such an account is rewritten after every fetch.
- The tests ran on Windows and, in CI, on Linux; never on ARM hardware.
- Whether Apple treats the identity reused in a re-authentication as the known device.
- The download and conversion of the real Anisette library bundle. The conversion uses the same code path of the `anisette` package as its own download; tests use a stand-in archive.
- The throughput numbers of the key derivation come from one Windows desktop; nothing was measured on Linux or a NAS.
- FindMy.py rewrites the Anisette bundle in place on every provider start. The checksum of its content survives another packing; a release of `anisette` that renames the files inside or stops writing a tar archive would still need the file to be deleted once. The pin keeps such a release out until the integration is tested with it.
- The log filter covers the loggers of FindMy.py by name. A module a later FindMy.py release adds is not covered until it is listed in `redaction.py`.
- The second CI job replaces Home Assistant and its pinned libraries in the locked environment; it ran on Windows locally and on Linux in CI, against 2026.11.1 only.
