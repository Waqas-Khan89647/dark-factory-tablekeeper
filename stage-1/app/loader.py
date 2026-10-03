"""Building a whole `State` from a reset fixture (§3.3, §4) or an export (§10).

Everything here validates first and builds a fresh `State`; nothing touches the live
state, so a rejected fixture or import leaves the service unchanged.
"""
from __future__ import annotations

import datetime as dt
import re

from . import passwords, timeutil, validation
from .errors import ApiError, invalid, malformed
from .model import (CANCELLED, CONFIRMED, OpeningHours, Receipt, Reservation,
                    Restaurant, State, Table, User, email_key)

REFERENCE = re.compile(r"^[A-Z0-9]{6,12}\Z")
RFC3339 = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?"
                     r"([+-][0-9]{2}:[0-9]{2}|Z)\Z")
TRACK = "tablekeeper"
FORMAT_VERSION = 1
# Bounds that keep every date computation inside datetime's range.
MAX_MINUTES = 100_000
MAX_CUTOFF_MINUTES = 100 * 366 * 24 * 60
MAX_COUNT = 1_000_000


# ---- small typed readers ---------------------------------------------------

def _obj(value, what: str) -> dict:
    if not isinstance(value, dict):
        raise invalid(f"{what} must be an object")
    return value


def _list(value, what: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise invalid(f"{what} must be a list")
    return value


def _required_list(raw: dict, name: str) -> list:
    if not isinstance(raw.get(name), list):
        raise invalid(f"state.{name} must be a list")
    return raw[name]


def _str(raw: dict, name: str, what: str, *, default: str | None = None) -> str:
    value = raw.get(name, default)
    if not isinstance(value, str):
        raise invalid(f"{what}.{name} must be a string")
    return value


def _id(raw: dict, name: str, what: str) -> str:
    value = _str(raw, name, what)
    if not value or len(value) > validation.MAX_ID_LENGTH:
        raise invalid(f"{what}.{name} must be 1 to {validation.MAX_ID_LENGTH} characters")
    return value


def _int(raw: dict, name: str, what: str, *, minimum: int, maximum: int) -> int:
    value = raw.get(name)
    if not validation.is_int(value) or not minimum <= value <= maximum:
        raise invalid(f"{what}.{name} must be an integer from {minimum} to {maximum}")
    return value


# ---- fixture pieces ---------------------------------------------------------

def parse_restaurant(raw) -> Restaurant:
    raw = _obj(raw, "restaurant")
    rid = _id(raw, "id", "restaurant")
    what = f"restaurant {rid}"
    tz_name = _str(raw, "timezone", what)
    try:
        timeutil.zone(tz_name)
    except ValueError:
        raise invalid(f"{what}: unknown timezone") from None
    hours = []
    for entry in _list(raw.get("opening_hours"), f"{what}.opening_hours"):
        entry = _obj(entry, f"{what}.opening_hours[]")
        day = entry.get("weekday")
        if day not in timeutil.WEEKDAYS:
            raise invalid(f"{what}: weekday must be one of {' '.join(timeutil.WEEKDAYS)}")
        opens = validation.hhmm_minutes(entry.get("opens"))
        closes = validation.hhmm_minutes(entry.get("closes"), allow_end_of_day=True)
        if closes <= opens:
            raise invalid(f"{what}: closes must be later than opens")
        hours.append(OpeningHours(day, opens, closes))
    tables, seen = [], set()
    for entry in _list(raw.get("tables"), f"{what}.tables"):
        entry = _obj(entry, f"{what}.tables[]")
        tid = _id(entry, "id", f"{what}.table")
        if tid in seen:
            raise invalid(f"{what}: duplicate table id {tid}")
        seen.add(tid)
        label = entry.get("label")
        if label is not None and not isinstance(label, str):
            raise invalid(f"{what}: table label must be a string")
        tables.append(Table(tid, _int(entry, "capacity", f"{what}.table", minimum=1,
                                             maximum=MAX_COUNT), label))
    return Restaurant(
        id=rid,
        name=_str(raw, "name", what, default=rid),
        timezone=tz_name,
        slot_minutes=_int(raw, "slot_minutes", what, minimum=1, maximum=MAX_MINUTES),
        duration_minutes=_int(raw, "reservation_duration_minutes", what,
                              minimum=1, maximum=MAX_MINUTES),
        cutoff_minutes=_int(raw, "cancellation_cutoff_minutes", what,
                            minimum=0, maximum=MAX_CUTOFF_MINUTES),
        opening_hours=tuple(hours),
        tables=tuple(tables),
    )


def _user_fields(raw) -> tuple[str, str, str]:
    raw = _obj(raw, "user")
    uid = _id(raw, "id", "user")
    email = _str(raw, "email", f"user {uid}")
    if not validation.EMAIL.match(email):
        raise invalid(f"user {uid}: email must be local@domain")
    return uid, email, _str(raw, "display_name", f"user {uid}", default="")


def resolve_start(restaurant: Restaurant, value) -> tuple[str, dt.datetime]:
    """(`starts_at_local`, UTC instant) for a stored or seeded booking."""
    local = validation.local_datetime(value)
    try:
        instant = timeutil.to_instant(local, restaurant.tz)
    except timeutil.NonexistentLocalTime:
        raise ApiError(422, "invalid_local_time", "that local time does not exist") from None
    except (OverflowError, ValueError):
        raise invalid("starts_at_local is out of range") from None
    return value, instant


def _reservation(raw, state: State, *, stored: bool) -> Reservation:
    raw = _obj(raw, "reservation")
    rid = _id(raw, "id", "reservation")
    what = f"reservation {rid}"
    reference = _str(raw, "reference", what)
    if not REFERENCE.match(reference):
        raise invalid(f"{what}: reference must be 6 to 12 characters of A-Z0-9")
    user_id = _str(raw, "user_id", what)
    if user_id not in state.users:
        raise invalid(f"{what}: unknown user")
    restaurant = state.restaurants.get(_str(raw, "restaurant_id", what))
    if restaurant is None:
        raise invalid(f"{what}: unknown restaurant")
    table_id = _str(raw, "table_id", what)
    if restaurant.table(table_id) is None:
        raise invalid(f"{what}: unknown table")
    party = raw.get("party_size")
    if not validation.is_int(party) or party < 1:
        raise invalid(f"{what}: party_size must be an integer of at least 1")
    try:
        local, instant = resolve_start(restaurant, raw.get("starts_at_local"))
    except ApiError as exc:
        raise invalid(f"{what}: {exc.message}") from None
    status, created_at = raw.get("status", CONFIRMED), raw.get("created_at")
    if stored:
        if status not in (CONFIRMED, CANCELLED):
            raise invalid(f"{what}: status must be confirmed or cancelled")
        if not _is_rfc3339(created_at):
            raise invalid(f"{what}: created_at must be an RFC 3339 timestamp with an offset")
    else:
        # Stage 1 defines neither field for a fixture, and unknown fields are never an
        # error (3.4): a valid value is honoured, anything else falls back to the default.
        if status != CANCELLED:
            status = CONFIRMED
        if not _is_rfc3339(created_at):
            created_at = timeutil.now().isoformat(timespec="seconds")
    return Reservation(rid, reference, user_id, restaurant.id, table_id, party,
                       local, instant, created_at, status)


def _is_rfc3339(value) -> bool:
    if not isinstance(value, str) or not RFC3339.match(value):
        return False
    try:
        return dt.datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _check_no_double_booking(state: State) -> None:
    """Two confirmed bookings may never share a table at overlapping times."""
    by_table: dict[tuple[str, str], list[Reservation]] = {}
    for booking in state.reservations.values():
        if booking.status == CONFIRMED:
            by_table.setdefault((booking.restaurant_id, booking.table_id), []).append(booking)
    for (restaurant_id, _), bookings in by_table.items():
        duration = state.restaurants[restaurant_id].duration
        bookings.sort(key=lambda b: b.starts_at)
        for earlier, later in zip(bookings, bookings[1:]):
            if later.starts_at < earlier.starts_at + duration:
                raise invalid(f"reservations {earlier.reference} and {later.reference} "
                              f"overlap on the same table")


def _add_reservation(state: State, booking: Reservation) -> None:
    if booking.id in state.reservations:
        raise invalid(f"duplicate reservation id {booking.id}")
    if booking.reference in state.by_reference:
        raise invalid(f"duplicate reservation reference {booking.reference}")
    state.add_reservation(booking)


def _add_restaurants(state: State, raw_list) -> None:
    for raw in _list(raw_list, "restaurants"):
        restaurant = parse_restaurant(raw)
        if restaurant.id in state.restaurants:
            raise invalid(f"duplicate restaurant id {restaurant.id}")
        state.restaurants[restaurant.id] = restaurant


def _add_user(state: State, uid: str, email: str, name: str, password_hash: str) -> None:
    if uid in state.users:
        raise invalid(f"duplicate user id {uid}")
    if email_key(email) in state.user_by_email:
        raise invalid(f"duplicate user email {email}")
    state.users[uid] = User(uid, email, name, password_hash)
    state.user_by_email[email_key(email)] = uid


# ---- reset fixture ----------------------------------------------------------

def read_fixture(fixture) -> tuple[list[tuple[str, str, str, str]], dict]:
    """Validate a fixture's shape; return (users with plaintext passwords, fixture).

    Hashing is slow, so it happens after this check and before `build_fixture`.
    """
    if not isinstance(fixture, dict):
        raise malformed("the fixture must be a JSON object")
    users, emails, ids = [], set(), set()
    for raw in _list(fixture.get("users"), "users"):
        uid, email, name = _user_fields(raw)
        password = raw.get("password")
        if not isinstance(password, str) or not password:
            raise invalid(f"user {uid}: password must be a non-empty string")
        if uid in ids or email_key(email) in emails:
            raise invalid(f"duplicate user {uid}")
        ids.add(uid)
        emails.add(email_key(email))
        users.append((uid, email, name, password))
    # Build once without hashes so every other error surfaces before any hashing.
    build_fixture(fixture, [(uid, email, name, "") for uid, email, name, _ in users])
    return users, fixture


def build_fixture(fixture: dict, users: list[tuple[str, str, str, str]]) -> State:
    """A fresh State from a fixture whose users already carry password hashes."""
    state = State()
    for uid, email, name, password_hash in users:
        _add_user(state, uid, email, name, password_hash)
    _add_restaurants(state, fixture.get("restaurants"))
    for raw in _list(fixture.get("reservations"), "reservations"):
        _add_reservation(state, _reservation(raw, state, stored=False))
    _check_no_double_booking(state)
    return state


# ---- export / import ----------------------------------------------------------

def export_state(state: State) -> dict:
    return {
        "track": TRACK,
        "format_version": FORMAT_VERSION,
        "state": {
            "users": [{"id": u.id, "email": u.email, "display_name": u.display_name,
                       "password_hash": u.password_hash} for u in state.users.values()],
            "tokens": [{"token": t, "user_id": uid} for t, uid in state.tokens.items()],
            "restaurants": [r.to_json() for r in state.restaurants.values()],
            "reservations": [{
                "id": b.id, "reference": b.reference, "user_id": b.user_id,
                "restaurant_id": b.restaurant_id, "table_id": b.table_id,
                "party_size": b.party_size, "starts_at_local": b.starts_at_local,
                "status": b.status, "created_at": b.created_at,
            } for b in state.reservations.values()],
            "receipts": [{"user_id": r.user_id, "method": r.method, "path": r.path,
                          "key": r.key, "body": r.body, "response": r.response}
                         for r in state.receipts.values()],
        },
    }


def _known_user(state: State, uid) -> bool:
    return isinstance(uid, str) and uid in state.users


def import_state(document) -> State:
    """Validate an export document completely and return the State it describes."""
    if not isinstance(document, dict):
        raise malformed("the import must be a JSON object")
    if document.get("track") != TRACK:
        raise invalid("track must be tablekeeper")
    version = document.get("format_version")
    if not validation.is_int(version) or version != FORMAT_VERSION:
        raise invalid("format_version must be 1")
    raw = _obj(document.get("state"), "state")
    state = State()
    for entry in _required_list(raw, "users"):
        uid, email, name = _user_fields(entry)
        password_hash = entry.get("password_hash")
        if not passwords.is_valid_hash(password_hash):
            raise invalid(f"user {uid}: bad password hash")
        _add_user(state, uid, email, name, password_hash)
    for entry in _required_list(raw, "tokens"):
        entry = _obj(entry, "token")
        token, uid = entry.get("token"), entry.get("user_id")
        if not isinstance(token, str) or not token or not _known_user(state, uid):
            raise invalid("tokens must name a token and a known user")
        state.tokens[token] = uid
    _add_restaurants(state, _required_list(raw, "restaurants"))
    for entry in _required_list(raw, "reservations"):
        _add_reservation(state, _reservation(entry, state, stored=True))
    _check_no_double_booking(state)
    for entry in _required_list(raw, "receipts"):
        entry = _obj(entry, "receipt")
        uid, method, path, key = (entry.get(k) for k in ("user_id", "method", "path", "key"))
        if not _known_user(state, uid) or not all(isinstance(v, str) and v
                                             for v in (method, path, key)):
            raise invalid("receipts must name a known user, method, path and key")
        if len(key) > validation.MAX_IDEMPOTENCY_KEY or "body" not in entry:
            raise invalid("receipt is incomplete")
        response = _obj(entry.get("response"), "receipt.response")
        state.receipts[(uid, method, path, key)] = Receipt(
            uid, method, path, key, entry["body"], response)
    return state
