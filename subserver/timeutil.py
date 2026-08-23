"""Time parsing shared by the API and the profile loader.

Profiles store wall-clock ISO-8601 strings because a human edits them.  A
string without an explicit UTC offset is interpreted in the *server's* local
timezone, which is the timezone the operator and the browsers on the same LAN
share in practice.  A string with an offset (or a trailing ``Z``) is honoured
as written.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

# "now", "now-90m", "-2h", "-7d" -- a window anchored to the present rather than
# to a fixed instant.  A live profile saved with a fixed start date stops being
# useful the week after it is written; a relative one keeps working.
_RELATIVE_RE = re.compile(r"^\s*(?:now)?\s*(?:(?P<sign>[-+])\s*(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>[smhdw]))?\s*$", re.I)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_relative(value: str) -> float | None:
    """Parse a ``now``-anchored offset, returning epoch seconds or ``None``."""
    text = str(value).strip().lower()
    if not text or not (text.startswith(("now", "-", "+"))):
        return None
    match = _RELATIVE_RE.match(text)
    if not match:
        return None
    if not match.group("amount"):
        return time.time() if text.startswith("now") else None
    offset = float(match.group("amount")) * _UNIT_SECONDS[match.group("unit").lower()]
    return time.time() + (offset if match.group("sign") == "+" else -offset)


def is_relative(value: object) -> bool:
    """True when ``value`` is a relative time expression rather than a date."""
    return isinstance(value, str) and parse_relative(value) is not None


class TimeError(ValueError):
    """A datetime string could not be understood."""


def parse_datetime(value: str | float | int | None) -> float | None:
    """Convert an ISO-8601 string or epoch number to epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    relative = parse_relative(text)
    if relative is not None:
        return relative
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Accept the "YYYY-MM-DD HH:MM" spelling browsers and humans produce.
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise TimeError(f"Not a recognisable date/time: {value!r}") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # attach the server's local offset
    return dt.timestamp()


def format_epoch(ts: float | None, *, seconds: bool = True) -> str:
    """Render epoch seconds as a local ISO-8601 string without an offset."""
    if ts is None:
        return ""
    dt = datetime.fromtimestamp(float(ts))
    return dt.strftime("%Y-%m-%dT%H:%M:%S" if seconds else "%Y-%m-%dT%H:%M")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def format_span(oldest: float | None, newest: float | None) -> str:
    """Render a time span compactly: times only when it fits inside one day."""
    if oldest is None or newest is None:
        return ""
    start = datetime.fromtimestamp(float(oldest))
    end = datetime.fromtimestamp(float(newest))
    if start.date() == end.date():
        return f"{start:%H:%M:%S} \u2192 {end:%H:%M:%S}"
    return f"{start:%b %d %H:%M} \u2192 {end:%b %d %H:%M}"
