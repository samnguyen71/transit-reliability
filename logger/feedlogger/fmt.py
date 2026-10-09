"""Small formatting helpers for times and sizes. All times are UTC."""

from __future__ import annotations

import time
from datetime import datetime, timezone


def utc_iso(ts: float) -> str:
    """2026-10-08T21:04:30.123Z"""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_stamp(ts: float) -> str:
    """20261008T210430Z, used for file names."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts))


def utc_day(ts: float) -> str:
    """2026-10-08, used for the daily folders."""
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def human_bytes(n: float) -> str:
    size = float(n)
    if abs(size) < 1024:
        return f"{size:.0f} B"
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
    return f"{size / 1024:.1f} TB"


def human_duration(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} min"
    if seconds < 36 * 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"
