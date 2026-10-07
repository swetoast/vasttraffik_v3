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
# Whether Home Assistant's home coordinate may be sent to Västtrafik, for stop
# suggestions and door-to-door trips.
CONF_USE_HOME  = "use_home"

# The APIs beyond Planera Resa. An application has to be given each of them
# separately on the developer portal, so each can be switched off.
CONF_USE_DISRUPTIONS = "use_disruptions"
CONF_USE_GEOGRAFI    = "use_geografi"
CONF_USE_PARKING     = "use_parking"
API_SWITCHES = {
    CONF_USE_DISRUPTIONS: "Störning",
    CONF_USE_GEOGRAFI:    "Geografi",
    CONF_USE_PARKING:     "Pendelparkering",
}

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
PARKING_SCAN_INTERVAL    = timedelta(seconds=300)
