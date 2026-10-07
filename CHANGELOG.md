# Changelog

## 2026.10.0

Checked against Home Assistant 2025.12.2 and the Planera Resa v4, Störning v1, Geografi v3
and Smart Pendelparkering v3 specifications. Nothing new needs configuring: it follows from
the lines already monitored. Planera Resa is the only API that is required.

### Added
- **Trip sensor** for every boarding stop → destination stop pair: the next way to get there
  on any line, with leave time, arrival, changes, transfers, platform, occupancy, the cheapest
  ticket Västtrafik suggests for it, and the following trips.
- **Door-to-door trips.** When Home Assistant's home is within 1.5 km of one end of a pair,
  that end is searched from the home coordinate, so the walk comes from Västtrafik
  (`access_minutes`, `home_arrival_time`). A boarding stop that is too far to walk but has
  commuter parking gets the drive planned instead.
- **Use my home location** switch in setup and options. When off, the home coordinate is
  never sent to Västtrafik. Existing entries start with it off.
- **Extra APIs as switches.** Störning, Geografi and Smart Pendelparkering each need their own
  subscription on the developer portal. Setup asks the key which it has and offers only
  those; they can be switched on or off later in the options. An existing entry is checked
  once on the first start: an API the key has no access to is switched off and its entities
  are removed instead of staying unavailable.
- **Leave at** timestamp sensors for lines and trips, usable directly as a time trigger.
- **Alerts** event entity per line: `departure_cancelled`, `platform_changed`,
  `stop_skipped`, `disruption_started`, `disruption_ended`. Nothing fires for what was
  already the case at startup, and a stop relocated for a long time is announced once.
- **Commuter parking** (Pendelparkering): a parking linked to a stop you board at appears as
  a device with free spaces, a forecast of when it fills up, the forecast for when your next
  trip leaves, and its cameras.
- **Disruption scope.** Each disruption is marked `trip`, `stop` or `line`; the Störning
  sensor adds `affects_your_stop` and `affects_next_departure`, and the departure sensor
  shows the relevant one as `disruption`.
- Actions that return data: `search_journey`, `stop_board` and `line_vehicles`.
- Setup suggests the stops nearest to home and pre-fills the walk time from the distance.
- Options: change a line's walk time or name without removing it. Entity IDs are kept.
- New attributes: `cancelled_departures`, `leave_at`, `direction_matched`, `local_service`
  and tariff zones on the ticket sensor (the last two need Geografi).

### Changed
- The options menu is translated (it was English only) and shows the current language and
  the extra APIs in use.
- Housekeeping without behaviour change: unused API methods and constants removed, import
  order and logging calls tidied, and the duplicate `strings.json` dropped (the
  `translations` folder is what Home Assistant reads). Diagnostics now also report whether
  Störning is available.
- **Direction is decided by the destination stop, not the headsign.** A trip counts when it
  actually calls at your destination after your boarding stop. This is learned once per
  headsign and platform. A stored direction that points the wrong way is corrected by itself.
  Server-side `directionGid` filtering is no longer used.
- The vehicle tracker follows the bus the departure sensor tells you to catch, keeps
  following it while you walk, and reports `stops_away` and `minutes_to_stop`.
  `departed_at` is replaced by `departure_time`.
- The disruption sensor is on only while a situation is in effect; later ones are listed
  under `upcoming_disruptions`. It is unavailable, not "off", when the API cannot be asked.
- Fewer requests: lines at the same stop share one departures request, all trackers share
  one position request, and an API the application is not subscribed to is left alone
  (Störning for an hour, Geografi and Pendelparkering for six) after saying so once in the log.
- The line picker lists every line serving the stop over the next 24 hours.
- Adding a line that is already monitored is refused; the reconnect dialog accepts a new key.
- Clock-time attributes use Home Assistant's time zone. The large `disruptions` attributes
  are no longer written to the recorder database.
- The repair issue for a missing disruption API is gone.

### Fixed
- Departure sensor going `unknown` or showing a bus in the wrong direction. The API returns
  only two departures per line and direction unless `maxDeparturesPerLineAndDirection` is
  set, and with a walk time both could already be too soon to catch.
- A network outage while Home Assistant starts triggered a "reconnect credentials" prompt.
  Setup is now retried; only a rejected key or secret starts reauthentication.
- Options flow: leaving the optional destination stop blank re-showed the form forever.
- Removing a monitored line left its device and entities behind as unavailable.
- Live vehicle positions stayed off until restart after a single failed request.
- Cancelled departures were dropped silently; a trip that skips your stop is now treated
  the same way.
- The arrival estimate did not follow a delay that arose after it was first fetched.
- Direction detection during setup used the first headsign found when the destination was
  a mid-route stop.

## 2025.10.0

Initial public release of the Planera Resa v4 rewrite.

### Added
- UI config flow: pick boarding stop, optional destination, line, and walk-time offset.
- Departure sensor (timestamp) with delay, platform, occupancy (+ source), line branding,
  cancellation flags, and parsed direction details (`via`, service flags, replaced/fortified line).
- Estimated arrival time at the configured destination stop (`arrival_time`, `travel_minutes`).
- Disruption binary sensor backed by the TrafficSituations (Störning) v1 API.
- Vehicle tracker with realtime positions and journey-path interpolation fallback.
- Ticket price sensor for the configured origin → destination.
- Swedish and English API response languages.
- Reauthentication flow when stored credentials are rejected.
- Repair issue when the disruption API is not part of the subscription.
- Downloadable diagnostics (credential-redacted).

### Internal
- Shared `DataUpdateCoordinator` per monitored line so the departure sensor and vehicle
  tracker fetch departures once per interval instead of duplicating calls.
- Server-side `directionGid` filtering resolved from the journey terminus, with a
  client-side direction fallback.
- Automatic OAuth token refresh and single retry on HTTP 401.
