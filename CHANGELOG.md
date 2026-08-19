# Changelog

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
