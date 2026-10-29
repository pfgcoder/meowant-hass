# UPET / Airrobo / Meowant Cat Litter Box for Home Assistant

Custom Home Assistant integration for UPET, Airrobo, and Meowant smart cat litter boxes.

This integration uses the vendor cloud API for account/device data and the vendor IM/MQTT channel for live work commands and work-state polling.

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
- Live MQTT work status:
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
does: 2002, 2006 and 2014 (wrong account or password), 2342 (locked for 10 minutes), and
2004 (no such user). Other login failures, such as a rate limit or an outage, are retried
at the next update instead.

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
- Box full alert.
- Deodorant remaining days.
- Auto clean delay.
- Firmware version.
- Wi-Fi name.
- MQTT work mode.
- MQTT work state.
- MQTT work cause.
- Last REST update and Last MQTT update. Disabled by default because they change on every
  poll; enable them under the entity's settings if you need them.

Entity names include the device name, for example `Litter Box Visits today` and `Tiny Visits today`.

Device binary sensors:

- Online.
- Deodorant expired.
- Sensor enabled.
- Camera enabled.
- Child lock.

Device controls:

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
- Start clean.
- Pause clean.
- Resume clean.
- Flatten.
- Pause flatten.
- Resume flatten.
- Raise litter rake.
- Lower litter rake.

Cat sensors:

- Weight.
- Visits.
- Usage duration.
- Visits today.

Cat picture URLs are exposed as cat entity attributes when returned by the vendor API. They
are left out of the recorder's history.

## MQTT Behavior

Work commands are sent through the vendor IM/MQTT path, not REST.

The integration obtains MQTT credentials and topics from the vendor API, publishes command payloads to the IM publish topic, and polls `request_state` for live work status.

Polling intervals:

- `RUNNING`: 5 seconds after each reply (the box answers in about a second).
- `PAUSED`: every 10 seconds.
- `PENDING`/standby/unknown: every 60 seconds.

A state request ends as soon as the box replies. Sends go one at a time, since they share the
MQTT client ID. If the broker refuses or hangs up on the cached MQTT login, the integration logs
in to IM again and retries once; nothing has been published at that point, so a command is never
sent twice. After a button press, Box status and the MQTT sensors show the expected state with a
`guessed: true` attribute until the box replies.

Auto clean and its delay are one setting on the box, so changing either is refused while the
other isn't known (for example when the settings call failed), rather than overwriting it.

MQTT polling starts after Home Assistant finishes setting up the integration, so startup is not blocked by MQTT traffic.

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
- Commands are confirmed by MQTT delivery/status responses, but full semantic validation of every receipt/error cause is not complete.
- Control board / child lock is currently exposed as read-only because the observed API flow returns a permission error for writes.
- Deodorize, direct light control, camera toggle, and full-alert threshold controls are not implemented yet.

## Development

Run dependency-free tests:

```bash
python3 -m unittest discover -s tests -v
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
- IM credential/contact lookup, one send at a time, and retry.
- MQTT codec, packet helpers, RISP/protobuf payload generation, and service ordinals.
- Coordinator: reauth, MQTT state replies, guessed states, kept parts and outage logging.
- Sensors: Last used by (and its restore), today counts, cat matching and attribute filtering.
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

MQTT state request uses RISP command `0x3fd`. Operation commands use RISP command `0x3fb`.

## Roadmap

Planned or open work:

- Decode more MQTT receipt/error causes and surface command failures more clearly.
- Implement deodorize command if available.
- Implement full-alert threshold setting.
- Implement writable control board / child lock if a permitted API flow is found.
- Add Home Assistant platform tests with `pytest-homeassistant-custom-component`.
