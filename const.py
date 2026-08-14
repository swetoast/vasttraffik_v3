"""Constants for the Västtrafik v3 integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "vasttrafik_v3"

# ── Credentials ───────────────────────────────────────────────────────────────
CONF_KEY = "key"
CONF_SECRET = "secret"

# ── Core data model ───────────────────────────────────────────────────────────
# The integration is structured around "monitored lines":
# each entry = one line at one stop → spawns departure sensor +
# Störning binary_sensor + vehicle device_tracker automatically.
CONF_MONITORED_LINES = "monitored_lines"

# Keys stored per monitored-line entry
CONF_STOP_NAME = "stop_name"
CONF_STOP_GID  = "stop_gid"
CONF_END_STOP_NAME = "end_stop_name"
CONF_END_STOP_GID  = "end_stop_gid"
CONF_LINE_NAME = "line_name"
CONF_LINE_GID  = "line_gid"
CONF_DIRECTION = "direction"
CONF_DIRECTION_GID = "direction_gid"
CONF_TRANSPORT_MODE = "transport_mode"
CONF_DELAY     = "delay"
CONF_NAME      = "name"
CONF_LANGUAGE  = "language"           # Accept-Language sent to the API

# Supported API response languages (REST.md section 12)
# Affects: notes messages, disruption descriptions, maneuverDescription
SUPPORTED_LANGUAGES = {
    "sv": "Svenska",
    "en": "English",
}
DEFAULT_LANGUAGE = "en"

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_DELAY = 0
DEFAULT_MIN_SEVERITY = "UNKNOWN"

# ── Störning severity levels (ascending order) ────────────────────────────────
SEVERITY_ORDER = ["UNKNOWN", "SLIGHT", "NORMAL", "SEVERE", "VERY_SEVERE"]

# ── Scan intervals ────────────────────────────────────────────────────────────
# Optimized based on data volatility analysis:
# - Poll frequently for data that changes rapidly (departures, vehicle positions)
# - Poll less frequently for data that changes slowly (disruptions, tickets)

# Departures: Real-time countdown data
# Changes: delays, platform changes, cancellations, next departure rotation
# 120s provides smooth countdown updates while respecting API limits
# Could reduce to 90s if needed, but 120s is the sweet spot
DEPARTURE_SCAN_INTERVAL  = timedelta(seconds=120)

# Disruptions: Long-lived events (hours to days)
# Changes: service disruptions are announced hours ahead and last for extended periods
# Emergency disruptions are rare; 10-minute detection latency is acceptable
# Optimization: 180s → 600s saves 66% API calls with near-zero impact
# Disruptions don't need second-by-second monitoring
DISRUPTION_SCAN_INTERVAL = timedelta(seconds=600)

# Vehicle tracking: Position updates (GPS or interpolated)
# How it works:
#   - Real-time GPS: fetched from API when available (~30-60s updates)
#   - Interpolated: calculated LOCALLY from journey coordinates + time
#     → Position updates smoothly in HA between API calls via interpolation
#     → API only needs to provide journey coords/times once per trip
# 60s is sufficient because:
#   - GPS updates are typically 30-60s intervals anyway
#   - Interpolated positions update locally (not dependent on API frequency)
# Optimization: 30s → 60s saves 50% API calls, minimal impact on map smoothness
VEHICLE_SCAN_INTERVAL    = timedelta(seconds=60)

# API usage impact per monitored line:
# ────────────────────────────────────────────────────────────────────
# Before optimization:  ~4,080 calls/day per line
#   - Departures:   720 calls/day (every 120s)
#   - Disruptions:  480 calls/day (every 180s)
#   - Vehicles:   2,880 calls/day (every 30s)
#
# After optimization:   ~1,824 calls/day per line (-55% reduction)
#   - Departures:   720 calls/day (every 120s) - unchanged
#   - Disruptions:  144 calls/day (every 600s) - 66% reduction
#   - Vehicles:     960 calls/day (every 60s)  - 50% reduction
#
# For 3 monitored lines: 12,240 → 5,472 calls/day (-55%)
# ────────────────────────────────────────────────────────────────────

