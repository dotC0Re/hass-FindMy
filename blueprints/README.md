# FindMy - HA Automation Blueprints

Blueprints ship with the integration but are not auto-loaded by Home Assistant.
Import them manually via URL. Both need Home Assistant 2024.10 or newer.

## `tag_zone_change.yaml`

Fires a notification when a tag enters or leaves a zone. Handles the common
"my bag/bike/laptop just left home" case out of the box.

Import URL:
```
https://raw.githubusercontent.com/dotC0Re/hass-FindMy/main/blueprints/automation/tag_zone_change.yaml
```

Steps: Settings → Automations & Scenes → Blueprints → Import Blueprint →
paste URL. Then Create Automation from the imported blueprint and pick tag,
zone and notification target.

## `tag_stale.yaml`

Warns when a tag hasn't reported for X hours. Catches dead batteries,
firmware crashes, or physical removal (thief unpaired the tag / hidden in a
Faraday cage).

Import URL:
```
https://raw.githubusercontent.com/dotC0Re/hass-FindMy/main/blueprints/automation/tag_stale.yaml
```

How it behaves:
- You get **one** warning when the tag's `detected_at` attribute becomes older
  than the threshold. Home Assistant re-checks the condition every minute, so
  there is no check interval to configure.
- No repeat warnings while the tag stays stale. After the tag has reported
  again and then goes stale once more, you get a new warning.
- If the tag is already stale when the automation is created, reloaded or Home
  Assistant starts, you are **not** warned until it has reported and gone
  stale again.
- A tag without a known `detected_at` (never located, or unavailable) is never
  reported as stale.

If you imported an earlier version of this blueprint, re-import it and check
the automations created from it: the "Check every N minutes" input is gone.

## Notes on notification targets

For the `notify_service` input:
- Leave empty for a persistent notification (in the HA UI notification bell)
- `notify.mobile_app_<devicename>` for the HA Companion App (mobile push)
- `notify.telegram` if you configured Telegram
- Any other `notify.*` service you have

To find your service name: Developer Tools → Actions → search "notify".
