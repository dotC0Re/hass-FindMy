# FindMy - Home Assistant integration

Experimental custom integration to provide device tracker entities for FindMy Network-enabled devices.

This is a fork of [malmeloo/hass-FindMy](https://github.com/malmeloo/hass-FindMy). Please report issues with
this fork at [dotC0Re/hass-FindMy](https://github.com/dotC0Re/hass-FindMy/issues).

A detailed code review with known security and stability issues is available in
[docs/CODE_REVIEW.md](docs/CODE_REVIEW.md). The verified findings, the fix strategy and the
dependency analysis are in [docs/REFACTORING_STRATEGY.md](docs/REFACTORING_STRATEGY.md). What has
been fixed so far, and what is still open, is listed in [docs/STATUS.md](docs/STATUS.md).

## Setting up

1. Add `https://github.com/dotC0Re/hass-FindMy` to HACS as a custom repository (category: Integration) and
   install the `FindMy` integration.
2. Enable the integration. You must add at least two 'devices': one Apple Account and one tracker device.
   1. When adding an account, you will need to specify an anisette server. You can use a public one, but it
      might start throwing errors after a while, so private servers are preferred. Google is your friend.
3. Enjoy!

## Switching from the original integration

Both integrations use the domain `findmy`, so only one of them can be installed. Make a backup
first, and keep the key files of your devices (`.plist` or `.json`) at hand.

**Replacing the files and keeping the entries.** If the folder `custom_components/findmy` is
replaced while your entries exist (by hand, or with a store that lets you swap the repository
under existing entries), the entries are taken over on the next start: stored passwords are
removed (see below) and everything else stays. This path is covered by tests only; nobody has
reported it from a running system yet.

**Through the integration marketplace of Home Assistant 2026.11.** As observed on one
installation with Core 2026.11.2, the marketplace does not uninstall an integration while its
domain has any config entry, a disabled one included, and does not install a second repository
for a domain whose folder another repository owns. What worked there:

1. Delete all FindMy entries (devices and accounts).
2. Uninstall the original integration and remove its repository.
3. Add `https://github.com/dotC0Re/hass-FindMy` and install it.
4. Restart Home Assistant.
5. Add your devices again with the same key files, and your account if you use one.

What to expect after these steps:

- The entities of a device come back with their entity ids, their settings (such as being
  disabled) and their device, because all of these are derived from the identifier in the key
  file. Automations and dashboards keep working.
- A name you gave a device in Home Assistant is replaced by the name from the key file. Rename
  the device again.
- The entries are new, so nothing is carried over from the old ones: an account signs in again
  with password and 2FA code, as a new device.


The integration keeps the session of your Apple account, not its password. When Apple ends the
session, Home Assistant asks you to sign in again with password and 2FA code (re-authentication);
there is no silent sign-in in the background. Signing in again keeps the device identity of the
account, so Apple should not announce a new device each time; to start over with a new identity,
remove the account and add it again. Entries created by older versions are cleaned
automatically on the next start. That includes disabled entries, as soon as at least one entry of
the integration is loaded. The session tokens themselves are still
stored in the config entry, and are part of every backup.

Going back to the original integration: restore a backup made before the switch. Without one the
entries load there, but an account without a stored password fails for good once its session
expires and has to be removed and added again.

## If Apple rejects the sign-in

The form shows what Apple answered, and the log has the same text as a warning. A wrong e-mail
address or password is only one possible reason. If both are right, do not keep trying: every
attempt registers a new emulated device with Apple, and repeated failures can get an account
locked. Please open an issue with Apple's answer instead.

## Integrated Anisette provider

Without a remote Anisette server the integration emulates one, which needs a library bundle from
the server of the `anisette` package. The bundle is downloaded once, kept as
`.storage/findmy.anisette_libs` in your configuration directory and checked before every use
against a checksum of the libraries in it, recorded at that first download. A damaged file is
repaired from a download that matches this checksum. If the check fails for good, the error names
the file; deleting it accepts a newly downloaded bundle.

## Debug logs

"Enable debug logging" covers the integration and the FindMy.py library. The credentials of a
remote Anisette URL (`https://user:password@...`) are removed from their log lines, and the lines
about a fetch name the device and the time of a report, not its position. The `anisette`
package behind the integrated provider is left out on purpose: its debug lines contain the
provisioning secrets of the emulated device. Do not set its logger to debug for a log you share.

## Increasing the update frequency

By default, the integration will only use your account to fetch for updates once per 15 minutes. This is to
reduce the risk of being banned by Apple. If you want to increase the tracker update frequency, it is possible
to add additional accounts. These accounts will divide the available time; 2 accounts will generate updates every
7.5 minutes, 3 will update every 5 minutes, etc.

The limit is kept across restarts and reloads. If Home Assistant restarts shortly after a fetch, the first
fetch waits until 15 minutes have passed since the last one of that account. Until then the trackers show
the last location known before the restart, with its original timestamp. That location is kept in
`.storage/findmy.last_reports` inside your Home Assistant configuration directory.

A fetch that fails (Apple not reachable, a session that expired) shows in the sensors of the account.
Trackers and sensors of a device stay available as long as anything is known about it. Calling
`homeassistant.update_entity` on an entity of this integration does not fetch from Apple.

## Local Bluetooth tracking

Rolling-key accessories such as official AirTags can also be detected by Home Assistant Bluetooth adapters and
ESPHome Bluetooth proxies. Local detections update independently of the 15-minute Find My network polling interval.

The tracker has no `battery_level` attribute (Home Assistant deprecated it for trackers); use the
battery sensors of the device instead.

The device tracker exposes the confirmed current address in `mac_address` and adds these attributes:

- `local_detected_at`
- `local_rssi`
- `local_source`
- `local_state` (`nearby` or `separated`)
- `local_battery` (`Full`, `Medium`, `Low`, or `Very Low`)
- `local_key_candidates`

An accessory that was without power for more than a week continues with the key it stopped at, far behind
the clock. It is recognised again from the keys around its last sighting: a day's worth below it and up to
90 days' worth above. These are derived in the background over a few local refreshes, so it can take a
little while until it shows up.

The entities of a device do not become unavailable when a fetch from Apple fails or no account is set up.
What is known about a device stays as it is, and what is not known shows as unknown. The state of the
Apple account is shown by the account sensors.

Local presence is not a security feature. Bluetooth advertisements are not authenticated: someone who
records an advertisement of a tag can send it again near one of your scanners up to about 36 hours later,
and the tag then counts as present although it is not. Use presence for reminders and checks, not to
unlock doors or to disarm an alarm system.

All key matching happens inside Home Assistant. Local presence tracking therefore continues without an internet
connection after the rolling-key accessory has been imported, although remote Find My network locations still
require an Apple account and internet access.
