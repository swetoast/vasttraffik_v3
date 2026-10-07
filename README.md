# Västtrafik for Home Assistant

Custom Home Assistant integration for Västtrafik using the Planera Resa v4 API.

Monitor selected Västtrafik lines directly in Home Assistant: upcoming departures, the next trip between two stops on any line, when to leave, disruptions and alerts, vehicle tracking, ticket prices, and commuter parking where available.

## Features

- UI setup through Home Assistant config flow
- Monitor one or more lines from selected stops
- Optional destination / direction filtering
- Realtime departure sensor with delay, platform, occupancy and line branding
- Estimated arrival time at your destination stop
- Disruption binary sensor for affected traffic
- Vehicle tracker for available position data
- Ticket price sensor for configured journeys
- Swedish and English API response language support
- Credential reconnect (reauth) flow and downloadable diagnostics
- Trip sensor per stop pair: the next way to get from A to B on any line
- "Leave at" time sensors when a walk time is set
- Door-to-door trips from your home, with the walk (or the drive to a park-and-ride) included
- One alerts entity per line: cancellations, platform changes, skipped stops, new disruptions
- Actions for automations and voice: journey search, stop board, a line's vehicles
- Commuter parking near your boarding stop (free spaces, forecast, cameras) when available
- Shared data fetching: lines at the same stop use one departures request per refresh

## Requirements

- Home Assistant 2024.11 or newer (tested on 2025.12.2)
- Västtrafik developer account
- Västtrafik API key and secret

Your application on the developer portal needs a subscription to each API it uses:

| API on the portal | Needed for | Required |
|---|---|---|
| Planera Resa v4 | Departures, trips, tickets, vehicle positions | Yes |
| Störning v1 | Disruption sensors and disruption alerts | No |
| Geografi v3 | Tariff zones, Närtrafik, parking facilities | No |
| Smart Pendelparkering (SPP) v3 | Commuter parking: free spaces, forecasts, cameras | No |

A key only works for the APIs its application subscribes to; the others answer HTTP 403. Setup asks your key which ones it has and switches the rest off, so nothing is left unavailable. You can add a subscription later and switch it on in the options.

Create credentials at:

```text
https://developer.vasttrafik.se
```

## Installation

**HACS:** add this repository as a custom repository of type *Integration*, install **Västtrafik v3**, and restart Home Assistant.

**Manually:** copy the integration to:

```text
custom_components/vasttrafik_v3/
```

Example structure:

```text
custom_components/vasttrafik_v3/
├── __init__.py
├── _helpers.py
├── api.py
├── binary_sensor.py
├── camera.py
├── config_flow.py
├── const.py
├── coordinator.py
├── device_tracker.py
├── diagnostics.py
├── event.py
├── manifest.json
├── options.py
├── sensor.py
├── services.py
├── services.yaml
├── translations/
│   ├── en.json
│   └── sv.json
└── brand/
    ├── icon.png
    └── logo.png
```

The folder must be named `vasttrafik_v3` (matching the integration domain), regardless of the repository name.

Restart Home Assistant after copying the files.

## Setup

1. Go to **Settings → Devices & services**
2. Click **Add integration**
3. Search for **Västtrafik v3**
4. Enter API key and secret, and choose whether your home location may be used (see *Your home location* below)
5. Confirm which extra Västtrafik APIs to use. The boxes are ticked for the ones your key has access to
6. Select the boarding stop: pick one of the suggested nearby stops or type any stop name
7. Optionally select a destination stop. With one, direction is worked out for you and you also get a trip sensor, arrival times and ticket prices
8. Select line
9. Set the walk time to the stop if needed; it is pre-filled when you picked a suggested stop

Additional monitored lines can be added from the integration options. One config entry is created per API account; monitored lines are managed within that entry.

## Entities

Each monitored line is one Home Assistant device with a departure sensor, a disruption sensor, an alerts entity, a vehicle tracker and, depending on its settings, a leave-at sensor and a ticket sensor. The same line tracked in two directions creates two separate devices.

Two more kinds of device appear on their own: one **trip** per boarding stop → destination stop pair, and one per **commuter parking** linked to a stop you board at.

### Departure sensor

Shows the next matching departure as a timestamp (Home Assistant renders it as a live "in X min" countdown).

Common attributes:

- `line`
- `stop`
- `direction`
- `end_stop`
- `walk_minutes`
- `departure_time`
- `minutes_until`
- `delay_minutes`
- `platform`
- `stop_moved`
- `designation` — the public line number
- `transport_mode`
- `transport_sub_mode` — e.g. `regionaltrain`, `vasttagen`
- `occupancy`
- `occupancy_source` — `prediction` or `realtime`
- `wheelchair_accessible`
- `is_realtime`
- `is_realtime_journey`
- `is_cancelled`
- `is_part_cancelled`
- `line_color` / `line_text_color` / `line_border_color`
- `service_journey_gid`
- `details_reference`
- `upcoming` — a list of the next few departures
- `leave_at` — departure minus walk time (HH:MM)
- `cancelled_departures` — upcoming trips your way that are cancelled or skip one of your stops
- `disruption` / `disruption_scope` — the active traffic situation that touches your stops or next buses, if any
- `local_service` — whether Närtrafik serves the stop (needs the Geografi API)
- `direction_matched` — `false` when no departure in the configured direction was found and the sensor fell back to the line's next departure in any direction

When a destination stop is configured:

- `arrival_time` — estimated arrival at your destination (HH:MM)
- `arrival_in_minutes`
- `travel_minutes` — estimated ride time from boarding to destination

Parsed direction details (from the API's direction string):

- `via` — the "via X" part of the direction, when present
- `short_direction`
- `replaces_line` / `fortifies_line`
- `service_flags` — a list of active flags such as `extra_bus`, `express_bus`, `school_bus`, `front_entry`, `direct_bus`

With a destination stop configured, direction is not taken from the headsign: a departure counts when its trip actually calls at your destination after your boarding stop. Lines whose buses show several different destinations therefore work without any setup, and a direction stored wrongly by an earlier version is corrected on its own.

### Leave at

Created for a line when its walk time is above zero, and for a trip when it has a walk time or starts from your home. The state is the time to walk out the door for the next departure, as a timestamp, so it can be used directly as a time trigger in automations.

### Trip sensor

One device per boarding stop → destination stop pair among your lines, named like `Brunnsparken, Göteborg → Korsvägen, Göteborg`. Its sensor is the departure time of the next trip that gets you there, whatever the line, including trips with a change.

Attributes: `line`, `lines`, `direction`, `departure_time`, `minutes_until`, `leave_at`, `arrival_time`, `arrival_in_minutes`, `travel_minutes`, `changes`, `transfers`, `platform`, `delay_minutes`, `occupancy`, `risk_of_missing_connection`, `notes`, `legs`, and `upcoming` (the next few trips).

**From and to your door.** If Home Assistant's home location is within 1.5 km of one end of the pair, that end is searched from the home coordinate. Only trips through the pair's own stop are used; if the search offers none, the trip is planned stop to stop as usual.

- Leaving from home: `access_mode`, `access_minutes` and `access_distance_m` describe the walk to `board_at`. With no walk time configured, Västtrafik's walk is used for `leave_at` and the Leave at sensor.
- Arriving home: `home_arrival_time` is when you reach the door after getting off at `alight_at`.
- If the boarding stop is too far to walk but has commuter parking, the drive there is planned and `access_mode` is `car`.

**Your home location.** The integration contains no stops, places or coordinates of its own. The home coordinate it uses is the one set in Home Assistant, it is sent only to Västtrafik's API, and only for these two things: suggesting nearby stops during setup and the door-to-door search above. Turn off **Use my home location** during setup, or in the options, and it is never sent; trips are then planned stop to stop. An entry created before this switch existed starts with it off: turn it on in the options if you want door-to-door trips.

**Ticket.** `ticket_name`, `ticket_price`, `ticket_price_youth`, `ticket_validity` and `ticket_zones` are the cheapest ticket Västtrafik suggests for this trip.

### Alerts

An event entity per line. It fires when something changes for an upcoming trip in your direction:

| Event | Data |
|---|---|
| `departure_cancelled` | `departure`, `line`, `direction` |
| `platform_changed` | `departure`, `from_platform`, `to_platform` |
| `stop_skipped` | `departure`, `stop` (`boarding` or `destination`) |
| `disruption_started` | `title`, `description`, `severity`, `scope` |
| `disruption_ended` | `situation_number` |

Use it as a state trigger and read `event_type` and the data from the attributes. Nothing fires for conditions that already existed when Home Assistant started.

### Commuter parking

If Västtrafik has a commuter parking linked to a stop you board at, it shows up as its own device:

- **Free spaces** — only for parkings that count spaces; attributes list capacity, each lot, and facilities (charging, disabled spaces, paid parking) when Geografi has them. `free_at_departure` is the forecast for when the next trip leaves that stop (`forecast_for`).
- **Expected full** — today's forecast of when it fills up; unknown when it is not expected to.
- **Camera** — a still image per camera, fetched when viewed.

### Disruption binary sensor

Turns on while a traffic situation affecting the monitored line or stop is in effect. Situations that start later are listed under `upcoming_disruptions` and do not turn the sensor on. The sensor is unavailable when the disruption API cannot be reached.

Common attributes:

- `line`
- `stop`
- `disruption_count`
- `worst_severity` — one of `SEVERE`, `NORMAL`, `SLIGHT`
- `affects_your_stop` — a situation hits your boarding or destination stop, or one of your next buses
- `affects_next_departure` — a situation names one of your next buses
- each entry in `disruptions` has a `scope`: `trip`, `stop` or `line`
- `upcoming_disruptions` — situations with a start time in the future, same shape as `disruptions`
- `disruptions` — a list; each entry contains `situation_number`, `severity`, `title`, `description`, `start_time`, `end_time`, and the nested `affected_lines`, `affected_stops`, and `affected_journeys`

### Vehicle tracker

Shows where the bus the departure sensor points at is right now, and keeps following that bus while you walk to the stop. Position is either realtime (from the positions endpoint) or interpolated along the journey's GPS path.

Common attributes:

- `line`
- `direction`
- `transport_mode`
- `departure_time` — when it leaves your stop
- `minutes_to_stop`
- `stops_away` — stops it still has to reach before yours; 0 means yours is next
- `next_stop`
- `current_segment`
- `progress_percent`
- `details_reference`
- `position_source` — `realtime_gps` or `path_interpolation`
- `notes` — realtime journey messages, when available (realtime GPS only)

### Ticket sensor

Shows the cheapest available adult single ticket price for the configured origin and destination. Created only when a destination stop is configured. `origin_zones` and `destination_zones` are added when the Geografi API is available.

## Actions

All three return data and change nothing. Stops can be given by name or 16-digit gid.

- `vasttrafik_v3.search_journey` — `origin`, `destination`, optional `via`, `time`, `arrive_by`, `only_direct`, `transport_modes`, `limit`. Returns `journeys` in the same shape as the trip sensor.
- `vasttrafik_v3.stop_board` — `stop`, `board` (`departures` or `arrivals`), optional `minutes`, `line`, `limit`. Returns `entries`.
- `vasttrafik_v3.line_vehicles` — `line`, optional `stop` by name (defaults to home) and `radius_km`. Returns `vehicles` with their positions.

```yaml
action: vasttrafik_v3.search_journey
data:
  origin: Brunnsparken, Göteborg
  destination: Korsvägen, Göteborg
response_variable: trips
```

## Options

Use the integration options to:

- Add monitored lines (nearby stops are suggested, and the walk time is estimated from the distance)
- Change a line's walk time or name, keeping its entities
- Remove monitored lines (their devices and entities are removed too)
- Change the API response language
- Turn the use of your home location on or off
- Switch the extra APIs (Störning, Geografi, Smart Pendelparkering) on or off. One your key has no access to cannot be switched on

## Reconnecting credentials

If the API rejects the stored key and secret (for example after they are rotated on the developer portal), Home Assistant prompts you to reconnect through a **Reconnect to Västtrafik** dialog — no need to delete and re-add the integration. Enter a valid key and secret and the entry reloads in place.

## Diagnostics

The integration supports Home Assistant's built-in diagnostics. Open the device or config entry and choose **Download diagnostics** for a credential-redacted snapshot (per-line update state, departure counts, next-arrival data, trips, stop details, and which of the optional APIs are available). This is the easiest thing to attach when reporting an issue.

## Troubleshooting

- **There are no disruption sensors.** Disruption data is the separate Störning API. If your key had no access to it when the integration was set up or updated, that part was switched off (the log says so once). Subscribe your application to Störning on the developer portal, then open the integration's options, choose **Extra Västtrafik APIs** and tick it.
- **Disruption sensors are unavailable.** Störning is switched on but the key is refused, for example because the subscription was removed. The sensors show unavailable rather than "off", and the API is tried again once an hour. Restore the subscription or switch Störning off in the options.
- **No parking, tariff zones or `local_service`.** These come from the Smart Pendelparkering and Geografi APIs. Subscribe your application to them and tick them in the options. A parking only appears if Västtrafik links one to a stop you board at.
- **The sensor shows a bus going the wrong way.** Check `direction_matched`. With a destination stop the direction corrects itself; `false` then means no upcoming trip of that line reaches your destination. Without a destination stop it means the stored direction matches no departure: remove the line in the options and add it again, preferably with a destination stop.
- **The trip is planned stop to stop although home is nearby.** Door-to-door needs **Use my home location** on and Home Assistant's home within 1.5 km of one of the two stops. It is also dropped if Västtrafik's search from the coordinate repeatedly offers nothing through that stop.
- **Vehicle tracker or ticket sensor stays empty.** The positions and ticket endpoints may not be part of every subscription. When they return no data, the tracker falls back to interpolation (or reports no active service) and the ticket sensor stays empty, without erroring.
- **Destination arrival time is missing.** `arrival_time` / `travel_minutes` only appear when a destination stop is configured for that line.
- **Authentication failed.** Re-check the API key and secret and that the application is active on the developer portal. If they changed, use the reconnect dialog (above).
- **"Already configured".** Only one entry per API account is allowed — add more lines through the existing entry's options instead.
- **Enable debug logging** to see the exact API calls and responses:

  ```yaml
  logger:
    logs:
      custom_components.vasttrafik_v3: debug
  ```

## Notes

This is a custom integration and is not included in Home Assistant Core.

All public transport data used by this integration is provided by Västtrafik.

Västtrafik data availability, realtime quality, endpoint access, and response fields depend on the Västtrafik developer platform and the permissions granted to the configured application.

## License

MIT
