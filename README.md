# UPET / Airrobo / Meowant Cat Litter Box for Home Assistant

Custom Home Assistant integration for UPET, Airrobo, and Meowant smart cat litter boxes.

This integration uses the vendor cloud API for account/device data and one persistent vendor IM/MQTT connection for work commands and live updates pushed by the boxes.

## Warning

This is an unofficial integration and is not affiliated with, endorsed by, or supported by UPET, Airrobo, or the vendor.

Use it at your own risk. The integration depends on private vendor APIs and may stop working or trigger vendor-side account restrictions at any time. I am not responsible for blocked accounts, lost access, device issues, or any other consequences of using this integration.

For safer use, create a separate UPET/Airrobo account and share litter box access to that account instead of using your primary account.

## Features

- Account login with UPET/Airrobo or Meowant credentials.
- Selectable account provider so credentials are routed to the correct vendor tenant.
- Device discovery from the vendor cloud account.
- Read-only box, waste-bin, deodorant, online, and firmware data.
- Visit and clean history: last visit, the cat that last used the box, and today's visits (per box and per cat) and cleans.
- Cat profile sensors and cat picture URL attributes when returned by the API.
- Config controls for confirmed settings:
  - Auto clean delay.
  - Auto clean on/off.
  - Deodorant alert.
  - Child lock.
  - Waste-bin full alert and its threshold.
  - Shake mode and foreign object detection, on boxes that report them.
  - Empty waste-bin reminder switch, time, and weekdays.
  - Do not disturb schedule.
  - Light schedule.
- MQTT work commands:
  - Start clean.
  - Pause clean.
  - Resume clean.
  - Flatten.
  - Pause flatten.
  - Resume flatten.
  - Raise litter rake.
  - Lower litter rake.
  - Request MQTT state.
- Live updates pushed by the box (see [Live Updates](#live-updates)):
  - Work mode.
  - Work state.
  - Work cause.
  - Last successful MQTT status update.
- Diagnostics download with sanitized raw API/coordinator data.
- Local brand icons for supported Home Assistant versions.

## Requirements

Home Assistant 2024.12 or newer.

## Installation

Install through HACS as a custom repository:

1. Open HACS in Home Assistant.
2. Go to `Integrations`.
3. Open the three-dot menu and choose `Custom repositories`.
4. Add `https://github.com/pfgcoder/meowant-hass`.
5. Select category `Integration`.
6. Install `Meowant`.

Restart Home Assistant after installation.

Manual installation is also possible by copying this repository's
`custom_components/ubpet` directory into your Home Assistant config directory:

```bash
<config>/custom_components/ubpet
```

Add the integration from Home Assistant:

```text
Settings -> Devices & services -> Add integration -> Meowant
```

## Configuration

Required fields:

- `Account provider`: select `UPET / Airrobo` or `Meowant` according to the account being used.
- `Country`: the same country selected in the phone app. Meowant keeps its own server and sends that country with the login, so United States is available. UPET / Airrobo still uses its European country list to choose the server.
- `Account`: login account for the selected provider.
- `Password`: plain account password. The integration hashes it internally before sending it to the API.

The integration also needs vendor app credentials:

- `APP_ID`
- `APP_KEY`
- `PRODUCT`

Bundled app defaults are included with the integration, so normal setup asks
only for country, account, and password. The selected country chooses the vendor
region endpoint and is sent as the login area code. The app defaults are stored
in obfuscated form to keep raw values out of the repository.

If you maintain a private deployment and want to override the bundled app credentials,
use `custom_components/ubpet/secrets.py.example` as a template for a private
`secrets.py`.

If the vendor refuses the stored password (for example after it was changed in the app),
or finds no such account, Home Assistant stops updating and asks for the current password
under `Settings -> Devices & services`. It goes by the vendor's login codes, as the app
does: 2002, 2006 and 2014 (wrong account or password) or 2342 (locked for 10 minutes) from
any account type the login tries, or a "no such account" code (1009, 2004, 2010, 2011, 2019
or 2042) from every one of them. Other login failures, such as a rate limit or an outage,
are retried at the next update instead.

To log in with another account, or with a new password, without removing the integration, use
`Reconfigure` in the entry's menu under `Settings -> Devices & services -> Meowant`. The new
account has to see the same litter boxes, so the entities, their IDs and their history stay. This
is how to move between a shared account and the box owner's (see Limitations).

If one of the calls behind the sensors fails (the box's settings, use times, deodorant,
online status, records or the cat list), its sensors keep their last values for up to an
hour, then show unknown. The log has a warning when a call starts failing and an info line
when it works again.

Options (`Settings -> Devices & services -> Meowant -> Configure`):

- `Records per fetch` (default 50): how many of each box's latest visit and clean records
  each update fetches. The today counts and Last visit are worked out from these, so raise
  it if Visits today or Cleans today miss some. Three cats make about 15 visits a day.

## Entities

Device sensors:

- Box status.
- Last visit (time of the newest visit record, with the five most recent as attributes).
- Last used by (the cat on the newest visit matched to a cat; weights the box couldn't match are skipped; kept over a restart).
- Visits today and Cleans today (restart at local midnight).
- Empty waste-bin reminder days.
- Box use times.
- Waste-bin level.
- Waste-bin last reset.
- Box full max.
- Deodorant remaining days.
- Auto clean delay.
- Firmware version.
- Wi-Fi name.
- MQTT work mode.
- MQTT work state.
- MQTT work cause.
- Last REST update and Last MQTT update. Disabled by default because they change on every
  update or push; enable them under the entity's settings if you need them.

Entity names include the device name, for example `Litter Box Visits today` and `Tiny Visits today`.

Device binary sensors:

- Online.
- Deodorant expired.
- Sensor enabled.
- Camera enabled.

Device controls:

- Child lock, waste bin full alert, and shake mode (on boxes that support it) switches.
- Foreign object detection switch, on boxes that report the capability. The MW-SC02 doesn't.
- Waste bin full threshold number, up to the bin's capacity the box reports.
- Auto clean delay number.
- Auto clean switch.
- Deodorant alert switch.
- Empty waste-bin reminder switch.
- Empty waste-bin reminder time.
- Empty waste-bin reminder weekday select.
- Do not disturb switch and start/end times.
- Light schedule switch and start/end times.

Device buttons:

- REST request.
- MQTT request state.
- Reset waste-bin counter.
- Reset deodorant (after replacing the deodorant block).
- Start clean.
- Pause clean.
- Resume clean.
- Flatten.
- Pause flatten.
- Resume flatten.
- Raise litter rake.
- Lower litter rake. Available while the rake is up and the box is paused or blocked
  (a hand or new litter on the scale shows as blocked), as in the app.

Cat sensors:

- Weight.
- Visits.
- Usage duration.
- Visits today.

Cat picture URLs are exposed as cat entity attributes when returned by the vendor API. They
are left out of the recorder's history.

## Visit Log

Each update reads each box's latest visit and clean records (`Records per fetch`,
50 by default). Every visit record newer than the last one seen fires a `ubpet_visit`
event and shows in the Logbook as "Tiny used Meowant SC03 Litter Box (6.7 kg, 58 s)",
on the cat's device page and the box's. Box records, such as a finished clean
(`event_type` 31), fire `ubpet_box_event` the same way, with empty cat fields.
Ported from [bojanrajkovic/upet-hass](https://github.com/bojanrajkovic/upet-hass).

Event data:

| Field | Meaning |
| --- | --- |
| `serial_number`, `box` | Which box |
| `record_id` | Vendor record id, unique and increasing |
| `cat_id`, `cat` | Identified cat, or `null` for an unmatched visit |
| `weight_kg`, `duration_s` | Weight measured and time spent |
| `event_type` | Vendor code: `3` matched cat, `21` unmatched, `31` clean finished |
| `time` | Visit time, ISO 8601 UTC |
| `device_id`, `entity_id` | The cat's device (the box's, for an unmatched visit) and the box's Last used by sensor, so the visit shows on both device pages |

The event reflects the cat known when the visit first appears. Matching a visit to a
cat later in the app doesn't fire a new event.

The last reported record id is stored per box, so a restart neither drops nor replays
visits. Adding the integration doesn't replay past visits, and a box paired later has
its first record reported. A failed records fetch reports nothing until it works again.
More visits on one box between two updates than `Records per fetch` (a long outage)
loses the oldest ones.

The recorder purges events after `purge_keep_days` (10 by default). To keep a
permanent CSV, point an automation at a [File](https://www.home-assistant.io/integrations/file/)
notify entity (`notify.litter_log` here):

```yaml
automation:
  - alias: Log litter box visits
    triggers:
      - trigger: event
        event_type: ubpet_visit
    actions:
      - action: notify.send_message
        target:
          entity_id: notify.litter_log
        data:
          message: >-
            {{ trigger.event.data.time }},{{ trigger.event.data.box }},
            {{ trigger.event.data.cat or 'unmatched' }},{{ trigger.event.data.weight_kg }},
            {{ trigger.event.data.duration_s }}
```

A box or cat the account no longer reports (after re-pairing a box or recreating a
cat profile) can be deleted from its device page, once an update has loaded everything.

## Live Updates

The integration holds one MQTT connection to the vendor broker (port 21000, TLS) per
account. The boxes push to every account they're linked to:

| Push | What happens |
| --- | --- |
| Work state (RISP `0x400`) | Work mode, work state and Box status update at once. |
| New record (RISP `0x3ff`) | Visit and clean records are fetched 3 seconds later. |
| Settings request (RISP `0x9e7`) | The box picked up a settings change, from the app, the box or Home Assistant. Settings are fetched 3 seconds later. |
| Online status | The box's Online sensor updates at once. |

Several pushes close together lead to one fetch. Commands go out over the same connection.
After each connect, the integration asks every box for its state, so the sensors fill in before
anything happens, and fetches the records it missed while disconnected. The REST update still
runs every 3 minutes as well.

The command buttons are unavailable while the connection is down, and a press that can't be sent
shows an error. After a button press, Box status and the MQTT sensors show the expected state
with a `guessed: true` attribute until the box pushes its new state; a press that fails puts the
last state back.

Any MQTT message the integration doesn't recognize fires `ubpet_unknown_message` with its raw
bytes in `payload_hex`, plus the sender, content type and RISP command when known. Command
receipts are skipped. This keeps pushes nobody has decoded yet, such as health alerts, for later.

Auto clean and its delay are one setting on the box, so changing either is refused while the
other isn't known (for example when the settings call failed), rather than overwriting it.

The connection starts in the background, so Home Assistant's startup doesn't wait for it.

### One session per account

The vendor allows one session per account. Any other login, such as the mobile app or a second
integration, takes over the session and disconnects this one. The integration waits a minute
before reconnecting, which signs the other login out in turn. Each further takeover doubles the
wait, up to 30 minutes, so occasional app use still works. A session that stays up for 10
minutes resets the wait.

While it waits, the integration also skips its REST updates, because any REST login would sign
the other session out early. When it reconnects, it fetches the records it missed.

The integration's own token refresh also replaces the session, so in that case it reconnects
after 5 seconds. A refused password stops the reconnects; the reauth that the next REST update
starts reconnects once the password is fixed.

To use the app and the integration side by side, share the boxes to a second account and give
the integration that one (see [Warning](#warning)). Some settings can then only be changed with
the owner's account; see [Limitations](#limitations).

Ported from [bojanrajkovic/upet-hass](https://github.com/bojanrajkovic/upet-hass).

## Confirmed MQTT Commands

Current service ids and operation payload bodies:

| Service id | Meaning | Operation body |
| --- | --- | --- |
| `start_clean_up` | Start clean | `08011001` |
| `pause_clean_up` | Pause clean | `08011002` |
| `resume_clean_up` | Resume clean | `08011003` |
| `start_flatten` | Flatten / smoothing | `08031001` |
| `pause_flatten` | Pause flatten | `08031002` |
| `resume_flatten` | Resume flatten | `08031003` |
| `start_rise` | Raise litter rake | `08071001` |
| `start_drop` | Lower litter rake | `08081001` |

Confirmed operation ordinals:

- `CLEAN = 1`
- `SMOOTHING = 3`
- `RISE = 7`
- `DROP = 8`
- `START = 1`
- `PAUSE = 2`
- `RESUME = 3`

## Limitations

- The integration depends on the vendor cloud and vendor IM/MQTT service.
- One session per account: the mobile app and the integration disconnect each other. See [One session per account](#one-session-per-account).
- Commands are confirmed by MQTT delivery/status responses, but full semantic validation of every receipt/error cause is not complete.
- The box only reports a changed setting once it applies it, so the child lock, waste-bin full alert, shake mode and foreign object detection switches show the new value at once and confirm it with a refresh 15 seconds later.
- On an account the box is shared with, the vendor refuses some settings changes that only the
  box's owner may make (`code=500`, `没有修改权限`, "no permission to modify"). The child lock is
  one; auto clean, its delay, the deodorant alert and the waste-bin full alert change fine. The
  Air Pet app refuses the same on a shared account ("The shared device is currently not
  authorized to operate"). The refused change shows a readable error, and the setting stays as it was. Change it in the app
  with the owner's account, or reconfigure the integration with that account.
- Deodorize, direct light control, and camera toggle are not implemented yet.

## Development

Run dependency-free tests:

```bash
python3 -I -B -m unittest discover -s tests -v
```

Run quick checks:

```bash
python3 -m unittest tests.test_mqtt tests.test_api
python3 -m py_compile custom_components/ubpet/*.py
python3 -m json.tool custom_components/ubpet/strings.json >/tmp/ubpet_strings.json
python3 -m json.tool custom_components/ubpet/translations/en.json >/tmp/ubpet_en.json
python3 -m json.tool custom_components/ubpet/translations/uk.json >/tmp/ubpet_uk.json
```

The current tests cover:

- API signing and password hashing.
- Account type fallback and login refusal codes.
- Authenticated request headers.
- Dashboard aggregation.
- Settings payload builders, and the auto clean guard.
- IM account lookup.
- MQTT codec, RISP/protobuf payload generation, service ordinals and push classification.
- The persistent connection: each push, commands, takeovers, reconnect backoff and refused logins.
- Coordinator: reauth, pushed states, skipped updates while yielding, guessed states, kept
  parts, outage logging and visit events.
- Command buttons: availability and failed sends.
- Sensors: setup, Last used by (and its restore), today counts, cat matching and attribute
  filtering.
- Setup: unique ID migration and the records option.
- Entity naming and translations.
- Diagnostics redaction.

## Implementation Notes

Implemented REST endpoints:

- `PUT /user-service-rest/v2/user/login`
- `GET /user-service-rest/v2/robot/common/device/list`
- `GET /catbox-server/box/config/allConfig?serialNumber=...`
- `GET /catbox-server/box/config/box-use-times/?serialNumber=...`
- `PUT /catbox-server/box/config/box-use-times/reset`
- `GET /catbox-server/app/deodorant-block/status?serialNumber=...`
- `GET /v1/ubtechinc-im-manager/im/online/device/?sn=...`
- `POST /v1/ubtechinc-im-manager/im/login`
- `GET /v1/ubtechinc-im-manager/im/friends`
- `GET /catbox-server/web/cat/info`
- `POST /catbox-server/web/box/record/new`
- `PUT /catbox-server/box/config/switch/update`
- `PUT /catbox-server/box/config/timePeriod/update`
- `PUT /catbox-server/box/config/timePoint/update`
- `POST /catbox-server/app/deodorant-block/switch`

MQTT command format:

```text
Message -> MessageContent(custom) -> Any(BytesValue) -> CommandProto(protype=2) -> RISP frame -> AirPet protobuf body
```

MQTT state request uses RISP command `0x3fd`. Operation commands use RISP command `0x3fb`. Boxes push work-state changes as `0x400`, with the same body as the `0x3fd` reply, and new-record signals as `0x3ff`.

## Roadmap

Planned or open work:

- Decode more MQTT receipt/error causes and surface command failures more clearly.
- Implement deodorize command if available.
- Add Home Assistant platform tests with `pytest-homeassistant-custom-component`.
