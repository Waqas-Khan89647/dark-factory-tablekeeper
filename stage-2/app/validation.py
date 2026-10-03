"""Field and query-parameter validation shared by every endpoint (§5).

Rule of thumb: a field of the wrong JSON type is 400 `malformed_request`; a missing
field or a value of the right type with a bad format or range is 422
`validation_failed`. `party_size` and `starts_at_local` have their own stricter rules.
"""
from __future__ import annotations

import datetime as dt
import re

from .errors import ApiError, invalid, malformed

LOCAL_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}\Z")
LOCAL_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
HHMM = re.compile(r"^[0-9]{2}:[0-9]{2}\Z")
DIGITS = re.compile(r"^[0-9]+\Z")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\Z")
MAX_ID_LENGTH = 64
MAX_IDEMPOTENCY_KEY = 255
# Calendar years the service accepts; keeps offsets, durations and cutoffs in range.
MIN_YEAR, MAX_YEAR = 1000, 9000


def is_int(value) -> bool:
    """A JSON integer: Python bools are ints, JSON booleans are not."""
    return isinstance(value, int) and not isinstance(value, bool)


def required_string(body: dict, name: str) -> str:
    value = body.get(name)
    if value is None:
        raise invalid(f"{name} is required")
    if not isinstance(value, str):
        raise malformed(f"{name} must be a string")
    return value


def table_id_set(body: dict, *, required: bool) -> tuple[str, ...] | None:
    """`table_id` (legacy single) or `table_ids` (array), per stage 2 §API.

    Returns `None` only when neither is present and `required` is false (PATCH/move
    fields that default to the booking's current tables). Order is the caller's; it is
    not yet normalised against a restaurant's declared `combinable` order.
    """
    has_single = body.get("table_id") is not None
    has_multi = body.get("table_ids") is not None
    if has_single and has_multi:
        raise invalid("send either table_id or table_ids, not both")
    if has_multi:
        value = body["table_ids"]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise malformed("table_ids must be a list of strings")
        if not value:
            raise invalid("table_ids must include at least one table")
        if len(value) > 2:
            raise ApiError(422, "combination_not_allowed", "at most two tables can be combined")
        if len(set(value)) != len(value):
            raise invalid("table_ids must not repeat a table")
        return tuple(value)
    if has_single:
        value = body["table_id"]
        if not isinstance(value, str):
            raise malformed("table_id must be a string")
        return (value,)
    if required:
        raise invalid("table_id or table_ids is required")
    return None


def party_size(value) -> int:
    """Any bad `party_size` -- missing, string, bool, float, below 1 -- is 422."""
    if not is_int(value) or value < 1:
        raise invalid("party_size must be an integer of at least 1")
    return value


def local_datetime(value) -> dt.datetime:
    """A bare local `YYYY-MM-DDTHH:MM`, no seconds and no offset.

    A non-string that is present is a wrong JSON type (400); a string in any
    other shape, or naming an impossible date or time, is 422.
    """
    if value is None:
        raise invalid("starts_at_local is required")
    if not isinstance(value, str):
        raise malformed("starts_at_local must be a string")
    if not LOCAL_TIME.match(value):
        raise invalid("starts_at_local must be a local YYYY-MM-DDTHH:MM")
    try:
        local = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise invalid("starts_at_local is not a real date and time") from None
    if not MIN_YEAR <= local.year <= MAX_YEAR:
        raise invalid("starts_at_local is out of range")
    return local


def query_date(value: str | None) -> dt.date:
    if not value:
        raise invalid("date is required")
    if not LOCAL_DATE.match(value):
        raise invalid("date must be YYYY-MM-DD")
    try:
        day = dt.date.fromisoformat(value)
    except ValueError:
        raise invalid("date is not a real calendar date") from None
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        raise invalid("date is out of range")
    return day


def query_positive_int(value: str | None, name: str) -> int:
    """Query integers are plain decimal digits: `4.0`, `+4`, ` 4` and `1e9` are 422."""
    if not value:
        raise invalid(f"{name} is required")
    if not DIGITS.match(value):
        raise invalid(f"{name} must be written as decimal digits")
    try:
        number = int(value)
    except ValueError:  # more digits than Python will convert
        raise invalid(f"{name} is out of range") from None
    if number < 1:
        raise invalid(f"{name} must be at least 1")
    return number


def hhmm_minutes(value, *, allow_end_of_day: bool = False) -> int:
    """Minutes since midnight for a 24-hour `HH:MM`; `24:00` only as a closing time."""
    if not isinstance(value, str) or not HHMM.match(value):
        raise invalid("times must be HH:MM")
    hours, minutes = int(value[:2]), int(value[3:])
    if allow_end_of_day and hours == 24 and minutes == 0:
        return 24 * 60
    if hours > 23 or minutes > 59:
        raise invalid("times must be HH:MM")
    return hours * 60 + minutes


def idempotency_key(value: str | None) -> str:
    if not value or not value.strip():
        raise ApiError(400, "missing_idempotency_key", "Idempotency-Key header is required")
    if len(value) > MAX_IDEMPOTENCY_KEY:
        raise invalid("Idempotency-Key must be 1 to 255 characters")
    return value
