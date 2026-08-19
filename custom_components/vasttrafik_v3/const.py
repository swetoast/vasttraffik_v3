"""Constants for the Västtrafik v3 integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "vasttrafik_v3"

CONF_KEY = "key"
CONF_SECRET = "secret"

# A "monitored line" = one line at one stop; each spawns a departure sensor,
# a Störning binary_sensor and a vehicle device_tracker.
CONF_MONITORED_LINES = "monitored_lines"

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
CONF_LANGUAGE  = "language"

SUPPORTED_LANGUAGES = {
    "sv": "Svenska",
    "en": "English",
}
DEFAULT_LANGUAGE = "en"

DEFAULT_DELAY = 0
DEFAULT_MIN_SEVERITY = "UNKNOWN"

# Störning severity, ascending.
SEVERITY_ORDER = ["UNKNOWN", "SLIGHT", "NORMAL", "SEVERE", "VERY_SEVERE"]

DEPARTURE_SCAN_INTERVAL  = timedelta(seconds=120)
DISRUPTION_SCAN_INTERVAL = timedelta(seconds=600)
VEHICLE_SCAN_INTERVAL    = timedelta(seconds=60)
