"""W2: availability, DST, POST /reservations, idempotency and concurrency."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import PASSWORD, error_code, fixture, future_date, new_key, restaurant, user

BERLIN_SPRING, BERLIN_FALL = "2026-03-29", "2026-10-25"
NY_SPRING, NY_FALL = "2026-03-08", "2026-11-01"


def slots(http, rid="r_main", date=None, party="2"):
    resp = http.get("/availability", params={
        "restaurant_id": rid, "date": date or future_date(), "party_size": party})
    assert resp.status_code == 200, resp.text
    return resp.json()["slots"]


def times(slot_list):
    return [s["starts_at_local"].split("T")[1] for s in slot_list]


# ---- availability ------------------------------------------------------------

def test_grid_runs_while_slot_plus_duration_fits(world, http):
    assert times(slots(http)) == ["18:00", "18:30", "19:00", "19:30", "20:00",
                                  "20:30", "21:00", "21:30"]


def test_grid_with_uneven_step(reset, http):
    reset(restaurants=[restaurant(opens="12:00", closes="14:00", slot=45, duration=60)])
    assert times(slots(http)) == ["12:00", "12:45"]


def test_capacity_filter_keeps_fixture_order(world, http):
    assert slots(http, party="3")[0]["available_table_ids"] == ["b", "c"]
    assert slots(http, party="7")[0]["available_table_ids"] == []


def test_booking_removes_the_table_from_every_overlapping_slot(world, http):
    alice, _ = world
    assert alice.book(table="b", at=f"{future_date()}T19:00").status_code == 201
    by_time = {s["starts_at_local"][-5:]: s["available_table_ids"] for s in slots(http)}
    for t in ("18:00", "18:30", "19:00", "19:30", "20:00"):
        assert "b" not in by_time[t], t
    assert "b" in by_time["20:30"]


def test_closed_day_has_no_slots(reset, http):
    reset(restaurants=[restaurant(days=())])
    assert slots(http) == []


@pytest.mark.parametrize("params", [
    {"date": "2030-01-01", "party_size": "2"},
    {"restaurant_id": "r_main", "party_size": "2"},
    {"restaurant_id": "r_main", "date": "2030-01-01"},
    {"restaurant_id": "", "date": "2030-01-01", "party_size": "2"},
    {"restaurant_id": "r_main", "date": "2030-02-30", "party_size": "2"},
    {"restaurant_id": "r_main", "date": "2030-1-01", "party_size": "2"},
    {"restaurant_id": "r_main", "date": "0001-01-01", "party_size": "2"},
    {"restaurant_id": "r_main", "date": "9999-12-31", "party_size": "2"},
    *({"restaurant_id": "r_main", "date": "2030-01-01", "party_size": p}
      for p in ("0", "-1", "abc", "1e9", "4.0", "+4", " 4", "4 ", "٤", "9" * 5000)),
])
def test_availability_validation(world, http, params):
    assert error_code(http.get("/availability", params=params)) == (422, "validation_failed")


def test_availability_unknown_restaurant_is_404(world, http):
    resp = http.get("/availability", params={"restaurant_id": "r_x", "date": "2030-01-01",
                                             "party_size": "2"})
    assert error_code(resp) == (404, "not_found")


def test_huge_party_is_valid_but_fits_nowhere(world, http):
    assert all(s["available_table_ids"] == [] for s in slots(http, party="9" * 30))


# ---- DST -----------------------------------------------------------------------

@pytest.fixture
def zoned(reset, login):
    reset(restaurants=[
        restaurant("r_berlin", opens="00:00", closes="23:30"),
        restaurant("r_ny", timezone="America/New_York", opens="00:00", closes="23:30")])
    return login("alice")


def test_spring_forward_gap_never_appears(zoned, http):
    berlin = times(slots(http, "r_berlin", BERLIN_SPRING))
    assert "02:00" not in berlin and "02:30" not in berlin and "03:00" in berlin
    ny = times(slots(http, "r_ny", NY_SPRING))
    assert "02:00" not in ny and "02:30" not in ny


@pytest.mark.parametrize("rid,date,at", [("r_berlin", BERLIN_SPRING, "02:30"),
                                         ("r_berlin", BERLIN_SPRING, "02:00"),
                                         ("r_ny", NY_SPRING, "02:30")])
def test_booking_in_the_gap_is_invalid_local_time(zoned, rid, date, at):
    resp = zoned.book(rid=rid, at=f"{date}T{at}")
    assert error_code(resp) == (422, "invalid_local_time")


def test_fall_back_hour_appears_once_with_first_offset(zoned, http):
    listed = slots(http, "r_berlin", BERLIN_FALL)
    assert times(listed).count("02:00") == 1 and times(listed).count("02:30") == 1
    two = next(s for s in listed if s["starts_at_local"].endswith("02:00"))
    assert two["starts_at"].endswith("+02:00")
    ny = slots(http, "r_ny", NY_FALL)
    one = next(s for s in ny if s["starts_at_local"].endswith("01:00"))
    assert times(ny).count("01:00") == 1 and one["starts_at"].endswith("-04:00")


def test_duration_is_absolute_across_fall_back(zoned):
    body = zoned.book(rid="r_berlin", at=f"{BERLIN_FALL}T01:30").json()
    assert body["starts_at"] == f"{BERLIN_FALL}T01:30:00+02:00"
    assert body["ends_at"] == f"{BERLIN_FALL}T02:00:00+01:00"


def test_duration_is_absolute_across_spring_forward(zoned):
    body = zoned.book(rid="r_berlin", at=f"{BERLIN_SPRING}T01:00").json()
    assert body["ends_at"] == f"{BERLIN_SPRING}T03:30:00+02:00"


def test_fall_back_overlap_uses_real_time(zoned):
    """01:30 CEST + 90 min ends 02:00 CET; a 02:00 (CEST) booking overlaps it."""
    assert zoned.book(rid="r_berlin", at=f"{BERLIN_FALL}T01:30").status_code == 201
    assert error_code(zoned.book(rid="r_berlin", at=f"{BERLIN_FALL}T02:00")) == \
        (409, "table_unavailable")


def test_same_instant_in_two_zones(zoned):
    berlin = zoned.book(rid="r_berlin", at="2026-12-01T18:00").json()["starts_at"]
    ny = zoned.book(rid="r_ny", at="2026-12-01T12:00").json()["starts_at"]
    assert berlin == "2026-12-01T18:00:00+01:00" and ny == "2026-12-01T12:00:00-05:00"


# ---- create ------------------------------------------------------------------

def test_create_shape(world):
    alice, _ = world
    date = future_date()
    resp = alice.book(table="b", at=f"{date}T19:00", party=4)
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"reservation_id", "reference", "restaurant_id", "table_id",
                         "table_ids", "party_size", "status", "starts_at_local", "starts_at",
                         "ends_at", "created_at"}
    assert body["status"] == "confirmed" and body["starts_at_local"] == f"{date}T19:00"
    assert body["created_at"].endswith("+00:00")
    assert 6 <= len(body["reference"]) <= 12 and body["reference"].isalnum()
    assert body["reference"] == body["reference"].upper()


@pytest.mark.parametrize("at,expected", [
    ("19:15", (422, "not_on_slot_grid")),
    ("17:15", (422, "not_on_slot_grid")),
    ("17:00", (422, "outside_opening_hours")),
    ("22:00", (422, "outside_opening_hours")),
    ("23:00", (422, "outside_opening_hours")),
    ("21:30", (201, None)),
])
def test_grid_and_hours(world, at, expected):
    alice, _ = world
    resp = alice.book(at=f"{future_date()}T{at}")
    got = (resp.status_code, resp.json().get("error", {}).get("code"))
    assert got == expected


def test_closed_day_booking_is_outside_hours(reset, login):
    reset(restaurants=[restaurant(days=())])
    resp = login("alice").book()
    assert error_code(resp) == (422, "outside_opening_hours")


@pytest.mark.parametrize("over,expected", [
    ({"party_size": 5}, (422, "party_exceeds_capacity")),
    ({"party_size": 0}, (422, "validation_failed")),
    ({"party_size": -1}, (422, "validation_failed")),
    ({"party_size": "4"}, (422, "validation_failed")),
    ({"party_size": 1.5}, (422, "validation_failed")),
    ({"party_size": 4.0}, (422, "validation_failed")),
    ({"party_size": True}, (422, "validation_failed")),
    ({"party_size": None}, (422, "validation_failed")),
    ({"party_size": [4]}, (422, "validation_failed")),
    ({"starts_at_local": "2030-01-10T19:00:00"}, (422, "validation_failed")),
    ({"starts_at_local": "2030-01-10T19:00Z"}, (422, "validation_failed")),
    ({"starts_at_local": "2030-01-10T19:00+01:00"}, (422, "validation_failed")),
    ({"starts_at_local": "2030-01-10 19:00"}, (422, "validation_failed")),
    ({"starts_at_local": "2030-01-10T19:00\n"}, (422, "validation_failed")),
    ({"starts_at_local": "2030-13-10T19:00"}, (422, "validation_failed")),
    ({"starts_at_local": "2030-01-10T24:00"}, (422, "validation_failed")),
    ({"starts_at_local": "9999-12-31T19:00"}, (422, "validation_failed")),
    ({"starts_at_local": 1900}, (400, "malformed_request")),
    ({"table_id": 2}, (400, "malformed_request")),
    ({"restaurant_id": None}, (422, "validation_failed")),
    ({"restaurant_id": "r_x"}, (404, "not_found")),
    ({"table_id": "zz"}, (404, "not_found")),
])
def test_create_field_rules(world, over, expected):
    alice, _ = world
    assert error_code(alice.book(**over)) == expected


def test_missing_fields_are_422(world):
    alice, _ = world
    for field in ("restaurant_id", "table_id", "starts_at_local", "party_size"):
        body = {"restaurant_id": "r_main", "table_id": "b",
                "starts_at_local": f"{future_date()}T19:00", "party_size": 2}
        del body[field]
        resp = alice.post("/reservations", json=body, key=new_key())
        assert error_code(resp) == (422, "validation_failed"), field


def test_table_of_another_restaurant_is_404(reset, login):
    reset(restaurants=[restaurant("r_main"),
                       restaurant("r_other", tables=[{"id": "x", "capacity": 4}])])
    assert error_code(login("alice").book(table="x")) == (404, "not_found")


def test_body_errors(world):
    alice, _ = world
    assert error_code(alice.post("/reservations", content=b"{bad", key=new_key())) == \
        (400, "malformed_request")
    assert error_code(alice.post("/reservations", json=[], key=new_key())) == \
        (400, "malformed_request")


def test_auth_is_checked_first(world, http):
    resp = http.post("/reservations", content=b"{bad")
    assert error_code(resp) == (401, "unauthenticated")


def test_past_bookings_are_allowed(world):
    alice, _ = world
    assert alice.book(at="2020-01-10T19:00").status_code == 201


def test_overlap_is_half_open(world):
    alice, bruno = world
    date = future_date()
    assert alice.book(at=f"{date}T19:00").status_code == 201
    assert error_code(bruno.book(at=f"{date}T20:00")) == (409, "table_unavailable")
    assert error_code(bruno.book(at=f"{date}T18:00")) == (409, "table_unavailable")
    assert bruno.book(at=f"{date}T20:30").status_code == 201
    assert bruno.book(at=f"{date}T19:00", table="c").status_code == 201


# ---- idempotency -----------------------------------------------------------------

def test_replay_returns_original_body_with_200(world):
    alice, _ = world
    first = alice.book(key="k1")
    replay = alice.book(key="k1")
    assert (first.status_code, replay.status_code) == (201, 200)
    assert replay.json() == first.json()
    assert len(alice.get("/reservations").json()["reservations"]) == 1


def test_replay_matches_parsed_json_not_bytes(world):
    alice, _ = world
    date = future_date()
    first = alice.post("/reservations", key="k", content=(
        '{"restaurant_id":"r_main","table_id":"b","starts_at_local":"%sT19:00",'
        '"party_size":4}' % date).encode(), headers={"Content-Type": "application/json"})
    again = alice.post("/reservations", key="k", content=(
        '{ "party_size" : 4, "starts_at_local":"%sT19:00", "table_id":"b",'
        ' "restaurant_id":"r_main" }' % date).encode(),
        headers={"Content-Type": "application/json"})
    assert (first.status_code, again.status_code) == (201, 200)


def test_reuse_with_different_body_beats_validation(world):
    alice, _ = world
    assert alice.book(key="k").status_code == 201
    assert error_code(alice.book(key="k", party=3)) == (409, "idempotency_key_reuse")
    assert error_code(alice.book(key="k", party="junk")) == (409, "idempotency_key_reuse")
    assert error_code(alice.book(key="k", note=True)) == (409, "idempotency_key_reuse")


def test_failed_key_is_reusable(world):
    alice, _ = world
    assert error_code(alice.book(key="k", table="zz")) == (404, "not_found")
    assert alice.book(key="k").status_code == 201


def test_keys_are_per_user(world):
    alice, bruno = world
    assert alice.book(key="shared", table="b").status_code == 201
    assert bruno.book(key="shared", table="c").status_code == 201


def test_same_key_other_path_is_not_a_replay(world):
    alice, _ = world
    ref = alice.book(key="k", table="a", party=2).json()["reference"]
    resp = alice.post("/reservation-moves", key="k",
                      json={"moves": [{"reference": ref, "table_id": "b"}]})
    assert resp.status_code == 201


@pytest.mark.parametrize("key,expected", [
    (None, (400, "missing_idempotency_key")),
    ("", (400, "missing_idempotency_key")),
    ("   ", (400, "missing_idempotency_key")),
    ("x" * 256, (422, "validation_failed")),
])
def test_key_header_rules(world, key, expected):
    alice, _ = world
    body = {"restaurant_id": "r_main", "table_id": "b",
            "starts_at_local": f"{future_date()}T19:00", "party_size": 2}
    headers = {} if key is None else {"Idempotency-Key": key}
    resp = alice.post("/reservations", json=body, headers=headers)
    assert error_code(resp) == expected


def test_key_of_255_characters_is_fine(world):
    alice, _ = world
    assert alice.book(key="x" * 255).status_code == 201


# ---- concurrency -------------------------------------------------------------------

def _burst(http, requests):
    """Send every request at once; the app serves them concurrently on its event loop."""
    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return list(pool.map(lambda r: http.request(**r), requests))


def _crowd(reset, http, count):
    names = [f"diner{i}" for i in range(count)]
    reset(body=fixture(users=names))
    tokens = []
    for name in names:
        resp = http.post("/auth/login", json={"email": user(name)["email"],
                                              "password": PASSWORD})
        tokens.append(resp.json()["token"])
    return tokens


def _booking_request(token, key, date):
    return {"method": "POST", "url": "/reservations",
            "headers": {"Authorization": f"Bearer {token}", "Idempotency-Key": key},
            "json": {"restaurant_id": "r_main", "table_id": "b",
                     "starts_at_local": f"{date}T19:00", "party_size": 4}}


@pytest.mark.parametrize("count", [10, 50])
def test_one_slot_many_clients_exactly_one_wins(reset, http, count):
    tokens = _crowd(reset, http, count)
    date = future_date()
    out = _burst(http, [_booking_request(t, new_key(), date) for t in tokens])
    statuses = sorted(r.status_code for r in out)
    assert statuses == [201] + [409] * (count - 1)


def test_one_key_replayed_concurrently_books_once(reset, http):
    token = _crowd(reset, http, 1)[0]
    date = future_date()
    out = _burst(http, [_booking_request(token, "same", date)] * 20)
    statuses = sorted(r.status_code for r in out)
    assert statuses == [200] * 19 + [201]
    assert len({r.text for r in out}) == 1
    listed = http.get("/reservations", headers={"Authorization": f"Bearer {token}"})
    assert len(listed.json()["reservations"]) == 1


# ---- strict JSON (review of W2) ------------------------------------------------------

def nested(depth: int):
    value = []
    for _ in range(depth - 1):
        value = [value]
    return value


@pytest.mark.parametrize("depth", [500, 900, 990])
def test_over_deep_body_is_400_on_first_use_and_retry(world, depth):
    alice, _ = world
    for _ in range(2):
        assert error_code(alice.book(key="deep", x=nested(depth))) == \
            (400, "malformed_request")
    assert alice.book(key="deep").status_code == 201   # the refused key stays usable


def test_nested_body_within_limit_replays(world):
    alice, _ = world
    first = alice.book(key="nest", x=nested(60))
    replay = alice.book(key="nest", x=nested(60))
    assert (first.status_code, replay.status_code) == (201, 200)
    assert replay.json() == first.json()


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e99999"])
def test_non_json_numbers_are_400_everywhere(world, http, literal):
    alice, _ = world
    ref = alice.book().json()["reference"]
    body = ('{"restaurant_id":"r_main","table_id":"c","starts_at_local":"%sT19:00",'
            '"party_size":2,"note":%s}' % (future_date(), literal)).encode()
    headers = {"Content-Type": "application/json"}
    for method, path in (("POST", "/reservations"), ("PATCH", f"/reservations/{ref}"),
                         ("POST", "/reservation-moves"), ("POST", "/auth/signup"),
                         ("POST", "/auth/login"), ("POST", "/_test/import"),
                         ("POST", "/_test/reset")):
        resp = alice.request(method, path, content=body, headers=headers, key=new_key())
        assert error_code(resp) == (400, "malformed_request"), (method, path)
    assert len(alice.get("/reservations").json()["reservations"]) == 1


def test_json_equal_is_depth_safe_and_type_strict():
    from app.service import json_equal
    assert json_equal(nested(5000), nested(5000))
    assert not json_equal(nested(5000), nested(4999))
    assert json_equal({"a": [1, 2.0]}, {"a": [1.0, 2]})
    assert not json_equal({"a": True}, {"a": 1})
    assert not json_equal({"a": None}, {"b": None})
    assert not json_equal(["1"], [1])
