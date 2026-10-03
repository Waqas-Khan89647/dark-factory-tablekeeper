"""Domain records and their JSON shapes."""
from __future__ import annotations

import datetime as dt
from bisect import bisect_left, bisect_right, insort
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from . import timeutil

CONFIRMED, CANCELLED = "confirmed", "cancelled"


@dataclass(frozen=True)
class OpeningHours:
    weekday: str
    opens: int    # minutes since local midnight
    closes: int

    def to_json(self) -> dict:
        return {"weekday": self.weekday, "opens": _hhmm(self.opens), "closes": _hhmm(self.closes)}


@dataclass(frozen=True)
class Table:
    id: str
    capacity: int
    label: str | None = None

    def to_json(self) -> dict:
        out = {"id": self.id}
        if self.label is not None:
            out["label"] = self.label
        out["capacity"] = self.capacity
        return out


@dataclass(frozen=True)
class Restaurant:
    id: str
    name: str
    timezone: str
    slot_minutes: int
    duration_minutes: int
    cutoff_minutes: int
    opening_hours: tuple[OpeningHours, ...]
    tables: tuple[Table, ...]

    @property
    def tz(self) -> ZoneInfo:
        return timeutil.zone(self.timezone)

    @property
    def duration(self) -> dt.timedelta:
        return dt.timedelta(minutes=self.duration_minutes)

    @property
    def cutoff(self) -> dt.timedelta:
        return dt.timedelta(minutes=self.cutoff_minutes)

    def table(self, table_id: str) -> Table | None:
        return next((t for t in self.tables if t.id == table_id), None)

    def hours_on(self, day: dt.date) -> list[OpeningHours]:
        name = timeutil.weekday(day)
        return sorted((h for h in self.opening_hours if h.weekday == name),
                      key=lambda h: h.opens)

    def summary_json(self) -> dict:
        return {"id": self.id, "name": self.name, "timezone": self.timezone}

    def to_json(self) -> dict:
        return {
            **self.summary_json(),
            "slot_minutes": self.slot_minutes,
            "reservation_duration_minutes": self.duration_minutes,
            "cancellation_cutoff_minutes": self.cutoff_minutes,
            "opening_hours": [h.to_json() for h in self.opening_hours],
            "tables": [t.to_json() for t in self.tables],
        }


@dataclass
class User:
    id: str
    email: str
    display_name: str
    password_hash: str


@dataclass
class Reservation:
    id: str
    reference: str
    user_id: str
    restaurant_id: str
    table_id: str
    party_size: int
    starts_at_local: str
    starts_at: dt.datetime     # aware, UTC
    created_at: str
    status: str = CONFIRMED

    def ends_at(self, restaurant: Restaurant) -> dt.datetime:
        return self.starts_at + restaurant.duration

    def to_json(self, restaurant: Restaurant) -> dict:
        tz = restaurant.tz
        return {
            "reservation_id": self.id,
            "reference": self.reference,
            "restaurant_id": self.restaurant_id,
            "table_id": self.table_id,
            "party_size": self.party_size,
            "status": self.status,
            "starts_at_local": self.starts_at_local,
            "starts_at": timeutil.rfc3339(self.starts_at, tz),
            "ends_at": timeutil.rfc3339(self.ends_at(restaurant), tz),
            "created_at": self.created_at,
        }


@dataclass
class Receipt:
    """A completed idempotent request: the body it was sent and the 201 it produced."""
    user_id: str
    method: str
    path: str
    key: str
    body: object
    response: dict


@dataclass
class State:
    users: dict[str, User] = field(default_factory=dict)
    user_by_email: dict[str, str] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)          # token -> user id
    restaurants: dict[str, Restaurant] = field(default_factory=dict)
    reservations: dict[str, Reservation] = field(default_factory=dict)
    by_reference: dict[str, str] = field(default_factory=dict)    # reference -> id
    receipts: dict[tuple[str, str, str, str], Receipt] = field(default_factory=dict)
    # (restaurant id, table id) -> confirmed bookings as sorted (starts_at, booking id).
    # Every change of a booking's status, table or start goes through the methods below.
    occupancy: dict[tuple[str, str], list[tuple[dt.datetime, str]]] = field(
        default_factory=dict)

    def add_reservation(self, booking: Reservation) -> None:
        self.reservations[booking.id] = booking
        self.by_reference[booking.reference] = booking.id
        if booking.status == CONFIRMED:
            self._occupy(booking)

    def cancel_reservation(self, booking: Reservation) -> None:
        if booking.status == CONFIRMED:
            self._release(booking)
            booking.status = CANCELLED

    def relocate(self, changes: list[tuple[Reservation, str, dt.datetime]]) -> None:
        """Move confirmed bookings to new (table id, start) together: all the old
        slots are released before any new one is taken, so a swap works."""
        for booking, _, _ in changes:
            self._release(booking)
        for booking, table_id, starts_at in changes:
            booking.table_id, booking.starts_at = table_id, starts_at
            self._occupy(booking)

    def is_free(self, restaurant: Restaurant, table_id: str, start: dt.datetime,
                *, excluding=()) -> bool:
        """No confirmed booking on the table overlaps [start, start + duration).

        Every booking at a restaurant lasts the same time, so the overlapping ones are
        exactly those starting strictly within one duration either side of `start`.
        """
        entries = self.occupancy.get((restaurant.id, table_id))
        if not entries:
            return True
        low = bisect_right(entries, start - restaurant.duration, key=_entry_start)
        high = bisect_left(entries, start + restaurant.duration, key=_entry_start)
        return all(entries[i][1] in excluding for i in range(low, high))

    def _occupy(self, booking: Reservation) -> None:
        insort(self.occupancy.setdefault((booking.restaurant_id, booking.table_id), []),
               (booking.starts_at, booking.id))

    def _release(self, booking: Reservation) -> None:
        key = (booking.restaurant_id, booking.table_id)
        entries = self.occupancy[key]
        del entries[bisect_left(entries, (booking.starts_at, booking.id))]
        if not entries:
            del self.occupancy[key]


def _entry_start(entry: tuple[dt.datetime, str]) -> dt.datetime:
    return entry[0]


def email_key(email: str) -> str:
    return email.strip().lower()


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
