# Västtrafik for Home Assistant

Custom Home Assistant integration for Västtrafik using the Planera Resa v4 API.

Monitor selected Västtrafik lines directly in Home Assistant with upcoming departures, disruptions, vehicle tracking, and ticket price data where available.

## Features

- UI setup through Home Assistant config flow
- Monitor one or more lines from selected stops
- Optional destination / direction filtering
- Realtime departure sensor with delay and platform data
- Disruption binary sensor for affected traffic
- Vehicle tracker for available position data
- Ticket price sensor for configured journeys
- Swedish and English API response language support

## Requirements

- Home Assistant
- Västtrafik developer account
- Västtrafik API key and secret

Create credentials at:

```text
https://developer.vasttrafik.se
```

## Installation

Copy the integration to:

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
├── config_flow.py
├── const.py
├── coordinator.py
├── device_tracker.py
├── manifest.json
├── options.py
├── sensor.py
├── strings.json
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
4. Enter API key and secret
5. Select boarding stop
6. Optionally select destination stop
7. Select line
8. Set walk time offset if needed

Additional monitored lines can be added from the integration options. One config entry is created per API account; monitored lines are managed within that entry.

## Entities

Each monitored line is grouped as one Home Assistant device. The same line tracked in two directions creates two separate devices.

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
- `transport_mode`
- `occupancy`
- `wheelchair_accessible`
- `is_realtime`
- `is_realtime_journey`
- `is_cancelled`
- `is_part_cancelled`
- `line_color` / `line_text_color`
- `service_journey_gid`
- `details_reference`
- `upcoming` — a list of the next few departures

### Disruption binary sensor

Turns on when an active traffic situation affects the monitored line or stop.

Common attributes:

- `line`
- `stop`
- `disruption_count`
- `worst_severity` — one of `SEVERE`, `NORMAL`, `SLIGHT`
- `disruptions` — a list; each entry contains `situation_number`, `severity`, `title`, `description`, `start_time`, `end_time`, and the nested `affected_lines`, `affected_stops`, and `affected_journeys`

### Vehicle tracker

Tracks the monitored service journey when position data is available. Position is either realtime (from the positions endpoint) or interpolated along the journey's GPS path.

Common attributes:

- `line`
- `direction`
- `transport_mode`
- `next_stop`
- `current_segment`
- `progress_percent`
- `details_reference`
- `position_source` — `realtime_gps` or `path_interpolation`

### Ticket sensor

Shows the cheapest available adult single ticket price for the configured origin and destination. Created only when a destination stop is configured.

## Options

Use the integration options to:

- Add monitored lines
- Remove monitored lines
- Change the API response language

## Troubleshooting

- **No disruption data / disruption sensor unavailable.** The disruption (Störning) data uses a separate Västtrafik API. If your developer application isn't granted access to it, the API returns 403/404 and the disruption sensors stay unavailable — the integration logs a single warning and stops retrying. Grant the Störning API to your application on the developer portal.
- **Vehicle tracker or ticket sensor stays empty.** The positions and ticket endpoints may not be part of every subscription. When they return no data, the tracker falls back to interpolation (or reports no active service) and the ticket sensor stays empty, without erroring.
- **Authentication failed during setup.** Re-check the API key and secret, and that the application is active on the developer portal.
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
