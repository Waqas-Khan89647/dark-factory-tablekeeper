"""W5: no input may produce a 5xx, and every error carries the envelope."""
import itertools
import random

import pytest

from conftest import future_date, new_key

ODD_VALUES = [None, True, False, 0, -1, 1.5, 4.0, 10 ** 40, 1e308, "", " ", "x" * 5000,
              "\u0000", "\ud800", "٤", "2030-01-10T19:00", "9999-12-31T23:59",
              "0001-01-01T00:00", "2026-03-29T02:30", [], {}, [1, "a"], {"a": {"b": []}}]
FIELDS = ["restaurant_id", "table_id", "starts_at_local", "party_size", "email",
          "password", "display_name", "moves", "reference", "track", "format_version",
          "state", "users", "restaurants", "reservations"]


def _bodies(seed: int, count: int):
    rng = random.Random(seed)
    for _ in range(count):
        yield {name: rng.choice(ODD_VALUES) for name in rng.sample(FIELDS, rng.randint(0, 6))}


def _assert_clean(resp):
    assert resp.status_code < 500, (resp.status_code, resp.text)
    if resp.status_code >= 400:
        assert set(resp.json()["error"]) >= {"code", "message"}


@pytest.fixture
def booked(world):
    alice, _ = world
    return alice, alice.book().json()["reference"]


def test_random_bodies_never_5xx(booked, http):
    alice, ref = booked
    targets = [("POST", "/reservations"), ("PATCH", f"/reservations/{ref}"),
               ("POST", "/reservation-moves"), ("POST", "/auth/signup"),
               ("POST", "/auth/login"), ("POST", f"/reservations/{ref}/cancel")]
    for (method, path), body in zip(itertools.cycle(targets), _bodies(7, 600)):
        _assert_clean(alice.request(method, path, json=body, key=new_key()))
    for body in _bodies(11, 40):
        _assert_clean(alice.post("/_test/import", json=body))
    for body in _bodies(13, 40):
        _assert_clean(alice.post("/_test/reset", json=body))


def test_random_move_items_never_5xx(booked):
    alice, ref = booked
    rng = random.Random(3)
    for _ in range(300):
        item = {"reference": rng.choice([ref, "NOPE99", ref.lower()])}
        for name in rng.sample(["table_id", "starts_at_local", "party_size"], rng.randint(0, 3)):
            item[name] = rng.choice(ODD_VALUES + ["a", "b", "c", "zz", 2, 6])
        _assert_clean(alice.post("/reservation-moves", json={"moves": [item]}, key=new_key()))


@pytest.mark.parametrize("raw", [b"", b"null", b"[]", b"\"x\"", b"1", b"{", b"\xff\xfe",
                                 b"[" * 100_000, b'{"a":NaN}', b'{"a":1e99999}'])
def test_raw_bodies_never_5xx(world, http, raw):
    alice, _ = world
    for path in ("/reservations", "/reservation-moves", "/auth/signup", "/auth/login",
                 "/_test/import", "/_test/reset"):
        _assert_clean(alice.post(path, content=raw, key=new_key()))


def test_odd_query_strings_never_5xx(world, http):
    rng = random.Random(5)
    values = ["", "r_main", "2030-01-10", "2", "0", "-3", "9" * 5000, "%00", "é",
              "2030-02-29", "10000-01-01", "2030-01-10T00:00"]
    for _ in range(200):
        params = {k: rng.choice(values) for k in ("restaurant_id", "date", "party_size")
                  if rng.random() < 0.8}
        _assert_clean(http.get("/availability", params=params))


def test_odd_paths_and_headers_never_5xx(world, http):
    alice, _ = world
    for path in ("/reservations/%00", "/reservations/" + "A" * 5000, "/restaurants/%2F..",
                 "/reservations//cancel", "/_test/export/extra", "/health/"):
        _assert_clean(alice.get(path))
        _assert_clean(alice.post(path, json={}))
    _assert_clean(alice.book(key="k" * 10_000))
    _assert_clean(http.get("/reservations", headers={"Authorization": "Bearer " + "x" * 10_000}))


def test_unencodable_strings_are_echoed_safely(world):
    alice, _ = world
    resp = alice.post("/auth/signup", token=None, json={
        "email": "odd@x.io", "password": "12345678", "display_name": "\ud800é"})
    assert resp.status_code == 201 and resp.json()["display_name"] == "\ud800é"
    _assert_clean(alice.post("/_test/reset", json={"users": [{"id": "\ud800" * 70}]}))


def test_full_day_one_minute_grid_stays_fast(reset, http):
    from conftest import restaurant
    tables = [{"id": f"t{i}", "capacity": 4} for i in range(40)]
    reset(restaurants=[restaurant(opens="00:00", closes="23:59", slot=1, duration=1,
                                  tables=tables)])
    resp = http.get("/availability", params={"restaurant_id": "r_main",
                                             "date": future_date(), "party_size": "2"})
    assert resp.status_code == 200 and len(resp.json()["slots"]) == 1439
