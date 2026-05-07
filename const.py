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
DEFAULT_LANGUAGE = "sv"

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_DELAY = 0
DEFAULT_MIN_SEVERITY = "UNKNOWN"

# ── Störning severity levels (ascending order) ────────────────────────────────
SEVERITY_ORDER = ["UNKNOWN", "SLIGHT", "NORMAL", "SEVERE", "VERY_SEVERE"]

# ── Scan intervals ────────────────────────────────────────────────────────────
DEPARTURE_SCAN_INTERVAL  = timedelta(seconds=120)
DISRUPTION_SCAN_INTERVAL = timedelta(seconds=180)
VEHICLE_SCAN_INTERVAL    = timedelta(seconds=30)

