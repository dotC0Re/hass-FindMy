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

## Your Apple ID password is not stored

The integration keeps the session of your Apple account, not its password. When Apple ends the
session, Home Assistant asks you to sign in again with password and 2FA code (re-authentication);
there is no silent sign-in in the background. Signing in again keeps the device identity of the
account, so Apple should not announce a new device each time; to start over with a new identity,
remove the account and add it again. Entries created by older versions are cleaned
automatically on the next start, disabled ones included. The session tokens themselves are still
stored in the config entry, and are part of every backup.

Going back to the original integration: restore a backup made before the switch. Without one the
entries load there, but an account without a stored password fails for good once its session
expires and has to be removed and added again.

## Integrated Anisette provider

Without a remote Anisette server the integration emulates one, which needs a library bundle from
the server of the `anisette` package. The bundle is downloaded once, kept as
`.storage/findmy.anisette_libs` in your configuration directory and checked against the SHA-256
recorded at that first download before every use. A damaged file is repaired from a download that
matches this checksum. If the check fails for good, the error names the file; deleting it accepts
a newly downloaded bundle.

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
the clock. It is recognised again from the keys that follow its last sighting, up to 90 days' worth. These
are derived in the background over a few local refreshes, so it can take a little while until it shows up.

All key matching happens inside Home Assistant. Local presence tracking therefore continues without an internet
connection after the rolling-key accessory has been imported, although remote Find My network locations still
require an Apple account and internet access.
