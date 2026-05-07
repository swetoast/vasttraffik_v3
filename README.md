````markdown
# Västtrafik v3 for Home Assistant

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
````

## Installation

Copy the integration to:

```text
custom_components/vasttrafik_v3/
```

Example structure:

```text
custom_components/vasttrafik_v3/
├── __init__.py
├── api.py
├── binary_sensor.py
├── config_flow.py
├── const.py
├── device_tracker.py
├── manifest.json
├── options.py
├── sensor.py
├── strings.json
├── translations/
│   └── en.json
└── brand/
    ├── icon.png
    └── logo.png
```

Restart Home Assistant after copying the files.

## Setup

1.  Go to **Settings → Devices & services**
2.  Click **Add integration**
3.  Search for **Västtrafik v3**
4.  Enter API key and secret
5.  Select boarding stop
6.  Optionally select destination stop
7.  Select line
8.  Set walk time offset if needed

Additional monitored lines can be added from the integration options.

## Entities

Each monitored line is grouped as one Home Assistant device.

### Departure sensor

Shows the next matching departure as a timestamp.

Common attributes:

*   `line`
*   `stop`
*   `direction`
*   `end_stop`
*   `walk_minutes`
*   `delay_minutes`
*   `platform`
*   `transport_mode`
*   `occupancy`
*   `cancelled`
*   `service_journey_gid`
*   `details_reference`

### Disruption binary sensor

Turns on when an active traffic situation affects the monitored line, stop, or journey.

Common attributes:

*   `count`
*   `severity`
*   `situations`
*   affected lines
*   affected stops
*   affected journeys

### Vehicle tracker

Tracks the monitored service journey when position data is available.

Common attributes:

*   `line`
*   `direction`
*   `transport_mode`
*   `next_stop`
*   `current_segment`
*   `progress_percent`
*   `details_reference`

### Ticket sensor

Shows the cheapest available adult single ticket price for the configured origin and destination.

## Options

Use the integration options to:

*   Add monitored lines
*   Remove monitored lines
*   Change stop, line, direction, or walk time offset

## Troubleshooting

## Notes

This is a custom integration and is not included in Home Assistant Core.

All public transport data used by this integration is provided by Västtrafik.

Västtrafik data availability, realtime quality, endpoint access, and response fields depend on the Västtrafik developer platform and the permissions granted to the configured application.

## License

MIT
