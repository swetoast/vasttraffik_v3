"""Shared helpers used across sensor, binary_sensor, and device_tracker."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_dt(value: str | None) -> datetime | None:
    """
    Parse an ISO-8601 string into a timezone-aware datetime.
    Returns None on any parse failure.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def to_float(value: Any) -> float | None:
    """Safely cast to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
