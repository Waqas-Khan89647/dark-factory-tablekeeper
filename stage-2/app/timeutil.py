"""Wall-clock to instant conversion under IANA rules (§9).

All instants are held as aware UTC datetimes. A local wall-clock time is resolved
against the restaurant's zone with fold=0, i.e. the first occurrence on a fall-back
night. A local time inside a spring-forward gap does not exist: converting it to UTC
and back does not return the same wall-clock reading.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.timezone.utc
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class NonexistentLocalTime(ValueError):
    """The wall-clock time falls in a spring-forward gap."""


@lru_cache(maxsize=None)
def zone(name: str) -> ZoneInfo:
    """The IANA zone, or ValueError for an unknown or unsafe name."""
    if not isinstance(name, str) or not name or name.startswith("/") or ".." in name:
        raise ValueError(f"unknown timezone {name!r}")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"unknown timezone {name!r}") from None


def to_instant(local: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    """UTC instant for a naive local time; raises NonexistentLocalTime in a gap.

    Raises OverflowError/ValueError when the instant is outside datetime's range.
    """
    aware = local.replace(tzinfo=tz, fold=0)
    instant = aware.astimezone(UTC)
    if instant.astimezone(tz).replace(tzinfo=None) != local:
        raise NonexistentLocalTime(local.isoformat())
    return instant


def exists(local: dt.datetime, tz: ZoneInfo) -> bool:
    try:
        to_instant(local, tz)
    except NonexistentLocalTime:
        return False
    return True


def boundary_instant(local: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    """A closing time as an instant; a closing time inside a gap maps just past it."""
    return local.replace(tzinfo=tz, fold=0).astimezone(UTC)


def rfc3339(instant: dt.datetime, tz: dt.tzinfo) -> str:
    return instant.astimezone(tz).isoformat(timespec="seconds")


def local_minute(local: dt.datetime) -> str:
    return (f"{local.year:04d}-{local.month:02d}-{local.day:02d}"
            f"T{local.hour:02d}:{local.minute:02d}")


def weekday(day: dt.date) -> str:
    return WEEKDAYS[day.weekday()]


def now() -> dt.datetime:
    return dt.datetime.now(UTC)
