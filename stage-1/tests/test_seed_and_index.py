"""W8: reset tolerates fields stage 1 does not define. W9: the per-table occupancy index."""
import time

import pytest

from app.model import CONFIRMED
from app.routes import service
from conftest import fixture, future_date, new_key, restaurant

DAY = future_date()


def at(hhmm: str, day: str = DAY) -> str:
    return f"{day}T{hhmm}"


def seed(reference="SEED01", *, table="b", when=None, **extra):
    return {"id": f"res_{reference}", "reference": reference, "user_id": "alice",
            "restaurant_id": "r_main", "table_id": table,
            "starts_at_local": when or at("19:00"), "party_size": 4, **extra}


def free_tables(http, hhmm: str):
    listed = http.get("/availability", params={
        "restaurant_id": "r_main", "date": DAY, "party_size": "2"}).json()["slots"]
    return next(s for s in listed if s["starts_at_local"] == at(hhmm))["available_table_ids"]


def assert_index_matches_bookings():
    """The index holds exactly the confirmed bookings, sorted, under their current table."""
    state = service.state
    expected: dict = {}
    for booking in state.reservations.values():
        if booking.status == CONFIRMED:
            expected.setdefault((booking.restaurant_id, booking.table_id), []).append(
                (booking.starts_at, booking.id))
    assert state.occupancy == {key: sorted(entries) for key, entries in expected.items()}


# ---- W8 -------------------------------------------------------------------------

def test_seeded_cancelled_reservation_is_kept_and_does_not_occupy(reset, login, http):
    reset(fixture(reservations=[seed(status="cancelled")]))
    alice = login("alice")
    assert alice.get("/reservations/SEED01").json()["status"] == "cancelled"
    assert "b" in free_tables(http, "19:00")
    assert login("bruno").book(table="b", at=at("19:00")).status_code == 201
    assert_index_matches_bookings()


def test_seeded_valid_created_at_is_honoured(reset, login):
    reset(fixture(reservations=[seed(created_at="2026-01-02T03:04:05+00:00")]))
    assert login("alice").get("/reservations/SEED01").json()["created_at"] == \
        "2026-01-02T03:04:05+00:00"


@pytest.mark.parametrize("extra", [
    {"created_at": "yesterday"}, {"created_at": 17}, {"created_at": "2026-01-02T03:04:05"},
    {"status": "pending"}, {"status": 5}, {"status": None},
    {"note": "window seat", "tags": [1, {"a": None}]},
])
def test_seeded_undefined_or_invalid_extras_fall_back_to_defaults(reset, login, http, extra):
    reset(fixture(reservations=[seed(**extra)]))
    body = login("alice").get("/reservations/SEED01").json()
    assert body["status"] == "confirmed"
    assert body["created_at"].endswith("+00:00") and body["created_at"] != "yesterday"
    assert "b" not in free_tables(http, "19:00")
    assert set(body) == {"reservation_id", "reference", "restaurant_id", "table_id",
                         "party_size", "status", "starts_at_local", "starts_at", "ends_at",
                         "created_at"}


def test_cancelled_seed_may_share_a_slot_with_a_confirmed_seed(reset, http):
    reset(fixture(reservations=[seed("GONE01", status="cancelled"), seed("HERE01")]))
    assert "b" not in free_tables(http, "19:00")
    assert_index_matches_bookings()


def test_import_stays_strict_about_status_and_created_at(world, http):
    alice, _ = world
    alice.book()
    for field, value in (("status", "pending"), ("created_at", "yesterday")):
        document = http.get("/_test/export").json()
        document["state"]["reservations"][0][field] = value
        resp = http.post("/_test/import", json=document)
        assert (resp.status_code, resp.json()["error"]["code"]) == (422, "validation_failed")


# ---- W9 -------------------------------------------------------------------------

def test_index_follows_create_cancel_patch_and_moves(world, http):
    alice, bruno = world
    first = alice.book(table="b", at=at("19:00")).json()["reference"]
    second = alice.book(table="c", at=at("19:00")).json()["reference"]
    bruno.book(table="a", at=at("21:00"), party=2)
    assert_index_matches_bookings()

    assert alice.patch(f"/reservations/{first}",
                       json={"starts_at_local": at("20:30")}).status_code == 200
    assert_index_matches_bookings()
    assert "b" in free_tables(http, "18:30") and "b" not in free_tables(http, "20:30")

    failed = alice.patch(f"/reservations/{first}", json={"table_id": "c",
                                                       "starts_at_local": at("19:30")})
    assert failed.status_code == 409
    assert_index_matches_bookings()

    swap = {"moves": [{"reference": first, "table_id": "c", "starts_at_local": at("19:00")},
                      {"reference": second, "table_id": "b"}]}
    assert alice.post("/reservation-moves", json=swap, key=new_key()).status_code == 201
    assert_index_matches_bookings()
    assert free_tables(http, "19:00") == ["a"]

    clash = {"moves": [{"reference": first, "table_id": "b"}]}   # b is held by `second`
    assert alice.post("/reservation-moves", json=clash, key=new_key()).status_code == 409
    assert_index_matches_bookings()
    assert free_tables(http, "19:00") == ["a"]

    assert alice.post(f"/reservations/{second}/cancel").status_code == 200
    assert alice.post(f"/reservations/{second}/cancel").status_code == 200  # idempotent
    assert_index_matches_bookings()
    assert free_tables(http, "19:00") == ["a", "b"]


def test_index_is_rebuilt_by_reset_and_import(world, http, reset):
    alice, _ = world
    kept = alice.book(table="b", at=at("19:00")).json()["reference"]
    gone = alice.book(table="c", at=at("19:00")).json()["reference"]
    alice.post(f"/reservations/{gone}/cancel")
    document = http.get("/_test/export").json()

    reset(fixture(reservations=[seed(table="a", party_size=2)]))
    assert_index_matches_bookings()
    assert free_tables(http, "19:00") == ["b", "c"]

    assert http.post("/_test/import", json=document).status_code == 204
    assert_index_matches_bookings()
    assert free_tables(http, "19:00") == ["a", "c"]
    assert alice.patch(f"/reservations/{kept}", json={"table_id": "c"}).status_code == 200
    assert_index_matches_bookings()
    assert free_tables(http, "19:00") == ["a", "b"]


def test_adjacent_bookings_do_not_conflict_but_overlapping_ones_do(world):
    alice, _ = world
    assert alice.book(table="b", at=at("19:30")).status_code == 201
    assert alice.book(table="b", at=at("18:00")).status_code == 201   # ends 19:30
    assert alice.book(table="b", at=at("21:00")).status_code == 201   # starts at its end
    for clash in ("18:30", "19:00", "20:00", "20:30"):
        assert alice.book(table="b", at=at(clash)).status_code == 409
    assert_index_matches_bookings()


def test_availability_stays_fast_with_many_stored_bookings(reset, http):
    tables = [{"id": f"t{n}", "label": str(n), "capacity": 4} for n in range(20)]
    seeds = [{"id": f"s{i}", "reference": f"S{i:07d}", "user_id": "alice",
              "restaurant_id": "r_main", "table_id": f"t{i % 20}",
              "starts_at_local": f"{future_date(20 + i // 20)}T19:00", "party_size": 2}
             for i in range(10_000)]
    reset(fixture(restaurants=[restaurant(tables=tables)], reservations=seeds))
    started = time.perf_counter()
    for _ in range(20):
        resp = http.get("/availability", params={
            "restaurant_id": "r_main", "date": future_date(25), "party_size": "2"})
    per_request = (time.perf_counter() - started) / 20
    assert resp.status_code == 200
    assert resp.json()["slots"][2]["available_table_ids"] == []
    assert per_request < 0.05, f"{per_request * 1000:.1f} ms per availability request"
