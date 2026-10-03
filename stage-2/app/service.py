"""The booking rules, applied to the in-memory State.

Every method here is synchronous and never awaits, so a caller holding `Service.lock`
sees and changes the state atomically. The HTTP layer takes the lock around each
mutation (and around idempotent check-and-execute), which is what rules out
double-booking under concurrent requests.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import secrets
import string
from dataclasses import dataclass

from . import timeutil, validation
from .errors import ApiError, invalid, not_found
from .loader import resolve_start
from .model import CANCELLED, Receipt, Reservation, Restaurant, State

REFERENCE_ALPHABET = string.ascii_uppercase + string.digits
REFERENCE_LENGTH = 8


@dataclass
class Placement:
    """A validated set of tables (one, or a declared combinable pair) and start time."""
    table_ids: tuple[str, ...]
    party_size: int
    starts_at_local: str
    starts_at: dt.datetime


def json_equal(a, b) -> bool:
    """Equality of two parsed JSON values, keeping booleans apart from numbers.

    Walks an explicit stack, so nesting depth can never exhaust the Python stack.
    """
    pending = [(a, b)]
    while pending:
        x, y = pending.pop()
        if isinstance(x, bool) or isinstance(y, bool):
            if not (isinstance(x, bool) and isinstance(y, bool) and x == y):
                return False
        elif isinstance(x, dict):
            if not isinstance(y, dict) or x.keys() != y.keys():
                return False
            pending.extend((x[k], y[k]) for k in x)
        elif isinstance(x, list):
            if not isinstance(y, list) or len(x) != len(y):
                return False
            pending.extend(zip(x, y))
        elif isinstance(x, (int, float)):
            if not (isinstance(y, (int, float)) and x == y):
                return False
        elif type(x) is not type(y) or x != y:
            return False
    return True


class Service:
    def __init__(self) -> None:
        self.state = State()
        self.lock = asyncio.Lock()

    # ---- identity -------------------------------------------------------------

    def user_for_token(self, token: str) -> str:
        user_id = self.state.tokens.get(token)
        if user_id is None:
            raise ApiError(401, "unauthenticated", "unknown bearer token")
        return user_id

    def issue_token(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self.state.tokens[token] = user_id
        return token

    def new_user_id(self) -> str:
        while True:
            candidate = f"u_{secrets.token_hex(8)}"
            if candidate not in self.state.users:
                return candidate

    # ---- restaurants and availability ---------------------------------------

    def restaurant(self, restaurant_id: str) -> Restaurant:
        restaurant = self.state.restaurants.get(restaurant_id)
        if restaurant is None:
            raise not_found("no such restaurant")
        return restaurant

    def _is_free(self, restaurant: Restaurant, table_ids, start: dt.datetime,
                 *, excluding=()) -> bool:
        return self.state.is_free(restaurant, table_ids, start, excluding=excluding)

    def slots(self, restaurant: Restaurant, day: dt.date, party_size: int) -> list[dict]:
        tz, found = restaurant.tz, {}
        midnight = dt.datetime.combine(day, dt.time())
        for hours in restaurant.hours_on(day):
            closes = timeutil.boundary_instant(
                midnight + dt.timedelta(minutes=hours.closes), tz)
            for minute in range(hours.opens, hours.closes, restaurant.slot_minutes):
                local = midnight + dt.timedelta(minutes=minute)
                label = timeutil.local_minute(local)
                if label in found:
                    continue
                try:
                    start = timeutil.to_instant(local, tz)
                except timeutil.NonexistentLocalTime:
                    continue
                if start + restaurant.duration > closes:
                    continue
                singles = [t for t in restaurant.tables
                          if t.capacity >= party_size and self._is_free(restaurant, (t.id,), start)]
                options = [{"table_ids": [t.id], "capacity": t.capacity} for t in singles]
                for pair in restaurant.combinable:
                    tables = [restaurant.table(tid) for tid in pair]
                    capacity = sum(t.capacity for t in tables)
                    if capacity >= party_size and self._is_free(restaurant, pair, start):
                        options.append({"table_ids": list(pair), "capacity": capacity})
                found[label] = {
                    "starts_at_local": label,
                    "starts_at": timeutil.rfc3339(start, tz),
                    "available_table_ids": [t.id for t in singles],
                    "available_options": options,
                }
        return [found[label] for label in sorted(found)]

    # ---- placement rules (shared by create, PATCH and moves) ------------------

    def place(self, restaurant: Restaurant, table_ids: tuple[str, ...], local_value: str,
              party_size: int) -> Placement:
        """Check a set of tables, time and party against the restaurant's rules (not
        occupancy).

        Order: unknown table 404 -> combination_not_allowed -> invalid_local_time ->
        grid -> hours -> party_exceeds_capacity.
        """
        tables = []
        for table_id in table_ids:
            table = restaurant.table(table_id)
            if table is None:
                raise not_found("no such table at this restaurant")
            tables.append(table)
        if len(table_ids) == 2:
            pair = restaurant.canonical_pair(frozenset(table_ids))
            if pair is None:
                raise ApiError(422, "combination_not_allowed",
                               "that pair of tables cannot be combined")
            table_ids = pair
            tables = [restaurant.table(tid) for tid in pair]
        local = validation.local_datetime(local_value)
        _, start = resolve_start(restaurant, local_value)
        self._check_opening(restaurant, local, start)
        capacity = sum(t.capacity for t in tables)
        if party_size > capacity:
            raise ApiError(422, "party_exceeds_capacity", "party is larger than the table(s)")
        return Placement(tuple(table_ids), party_size, local_value, start)

    def _check_opening(self, restaurant: Restaurant, local: dt.datetime,
                       start: dt.datetime) -> None:
        entries = restaurant.hours_on(local.date())
        if not entries:
            raise ApiError(422, "outside_opening_hours", "the restaurant is closed that day")
        minute = local.hour * 60 + local.minute
        on_grid = [h for h in entries if (minute - h.opens) % restaurant.slot_minutes == 0]
        if not on_grid:
            raise ApiError(422, "not_on_slot_grid", "that time is not on the slot grid")
        midnight = dt.datetime.combine(local.date(), dt.time())
        try:
            end = start + restaurant.duration
            for hours in on_grid:
                closes = timeutil.boundary_instant(
                    midnight + dt.timedelta(minutes=hours.closes), restaurant.tz)
                if hours.opens <= minute and end <= closes:
                    return
        except (OverflowError, ValueError):
            pass
        raise ApiError(422, "outside_opening_hours", "outside opening hours")

    def check_cutoff(self, restaurant: Restaurant, booking: Reservation) -> None:
        if timeutil.now() >= booking.starts_at - restaurant.cutoff:
            raise ApiError(409, "cutoff_passed", "too close to the start to change this booking")

    # ---- reservations ---------------------------------------------------------

    def new_reference(self) -> str:
        while True:
            candidate = "".join(secrets.choice(REFERENCE_ALPHABET)
                                for _ in range(REFERENCE_LENGTH))
            if candidate not in self.state.by_reference:
                return candidate

    def new_reservation_id(self) -> str:
        while True:
            candidate = f"res_{secrets.token_hex(8)}"
            if candidate not in self.state.reservations:
                return candidate

    def create(self, user_id: str, restaurant: Restaurant, placement: Placement) -> Reservation:
        if not self._is_free(restaurant, placement.table_ids, placement.starts_at):
            raise ApiError(409, "table_unavailable", "that table is taken at that time")
        booking = Reservation(
            id=self.new_reservation_id(), reference=self.new_reference(), user_id=user_id,
            restaurant_id=restaurant.id, table_ids=placement.table_ids,
            party_size=placement.party_size, starts_at_local=placement.starts_at_local,
            starts_at=placement.starts_at,
            created_at=timeutil.now().isoformat(timespec="seconds"))
        self.state.add_reservation(booking)
        return booking

    def own(self, user_id: str, reference: str) -> Reservation:
        booking = self.state.reservations.get(self.state.by_reference.get(reference, ""))
        if booking is None or booking.user_id != user_id:
            raise not_found("no such reservation")
        return booking

    def list_for(self, user_id: str) -> list[Reservation]:
        mine = [b for b in self.state.reservations.values() if b.user_id == user_id]
        return sorted(mine, key=lambda b: b.starts_at, reverse=True)

    def as_json(self, booking: Reservation) -> dict:
        return booking.to_json(self.state.restaurants[booking.restaurant_id])

    def cancel(self, booking: Reservation) -> Reservation:
        if booking.status == CANCELLED:
            return booking
        self.check_cutoff(self.state.restaurants[booking.restaurant_id], booking)
        self.state.cancel_reservation(booking)
        return booking

    def amend(self, booking: Reservation, changes: dict) -> Reservation:
        """PATCH: cancelled -> cutoff -> field rules -> placement -> occupancy."""
        restaurant = self.state.restaurants[booking.restaurant_id]
        self.check_editable(restaurant, booking)
        placement = self.placement_for(restaurant, booking, changes)
        if not self._is_free(restaurant, placement.table_ids, placement.starts_at,
                             excluding={booking.id}):
            raise ApiError(409, "table_unavailable", "that table is taken at that time")
        self._apply([(booking, placement)])
        return booking

    def check_editable(self, restaurant: Restaurant, booking: Reservation) -> None:
        if booking.status == CANCELLED:
            raise ApiError(409, "reservation_cancelled", "this reservation is cancelled")
        self.check_cutoff(restaurant, booking)

    def placement_for(self, restaurant: Restaurant, booking: Reservation,
                      changes: dict) -> Placement:
        table_ids = validation.table_id_set(changes, required=False)
        local_value = changes.get("starts_at_local", booking.starts_at_local)
        if "starts_at_local" in changes:
            validation.local_datetime(local_value)
        party = (validation.party_size(changes["party_size"]) if "party_size" in changes
                 else booking.party_size)
        return self.place(restaurant, booking.table_ids if table_ids is None else table_ids,
                          local_value, party)

    def _apply(self, changes: list[tuple[Reservation, Placement]]) -> None:
        self.state.relocate([(b, p.table_ids, p.starts_at) for b, p in changes])
        for booking, placement in changes:
            booking.party_size = placement.party_size
            booking.starts_at_local = placement.starts_at_local

    def move(self, user_id: str, moves: list[dict]) -> list[Reservation]:
        """All-or-nothing batch amendment (§11). `moves` is already shape-checked."""
        bookings = [self.own(user_id, item["reference"]) for item in moves]
        if len({b.restaurant_id for b in bookings}) > 1:
            raise invalid("all moved bookings must be at the same restaurant")
        restaurant = self.state.restaurants[bookings[0].restaurant_id]
        placements = []
        for booking, item in zip(bookings, moves):
            self.check_editable(restaurant, booking)
            placements.append(self.placement_for(restaurant, booking, item))
        listed = {b.id for b in bookings}
        for index, placement in enumerate(placements):
            if not self._is_free(restaurant, placement.table_ids, placement.starts_at,
                                 excluding=listed):
                raise ApiError(409, "table_unavailable", "a moved booking would overlap")
            end = placement.starts_at + restaurant.duration
            for other in placements[index + 1:]:
                if (set(other.table_ids) & set(placement.table_ids) and other.starts_at < end
                        and placement.starts_at < other.starts_at + restaurant.duration):
                    raise ApiError(409, "table_unavailable", "moved bookings overlap")
        self._apply(list(zip(bookings, placements)))
        return bookings

    # ---- idempotency ----------------------------------------------------------

    def run_idempotent(self, user_id: str, method: str, path: str, key: str, body,
                       action) -> tuple[int, dict]:
        """Replay, reject reuse, or run `action` and keep its receipt on success."""
        scope = (user_id, method, path, key)
        receipt = self.state.receipts.get(scope)
        if receipt is not None:
            if json_equal(receipt.body, body):
                return 200, receipt.response
            raise ApiError(409, "idempotency_key_reuse",
                           "this key was already used with a different body")
        response = action()
        self.state.receipts[scope] = Receipt(user_id, method, path, key, body, response)
        return 201, response
