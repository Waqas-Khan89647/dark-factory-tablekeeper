"""Stage 2: combinable table pairs -- fixture, availability, booking, moves, concurrency,
and cross-version import from a stage-1-shaped export."""
from __future__ import annotations

import threading

import pytest

from conftest import error_code, fixture, future_date, new_key, restaurant

COMBO_TABLES = [
    {"id": "a", "label": "A", "capacity": 2},
    {"id": "b", "label": "B", "capacity": 4},
    {"id": "c", "label": "C", "capacity": 6},
]
PAIRS = [["a", "b"], ["b", "c"]]


def combo_restaurant(**kw):
    return restaurant(tables=COMBO_TABLES, combinable=PAIRS, **kw)


@pytest.fixture
def combo_world(reset, login):
    reset(fixture(restaurants=[combo_restaurant()]))
    return login("alice"), login("bruno")


# ---- fixture validation -----------------------------------------------------------

@pytest.mark.parametrize("combinable", [
    [["a", "a"]],              # self-pair
    [["a", "z"]],               # unknown table
    [["a", "b", "c"]],           # three tables, not a pair
    [["a"]],                    # not a pair
    ["a-b"],                    # not a list of two ids
    [[1, 2]],                   # wrong element type
    [["a", "b"], ["b", "a"]],     # same pair declared twice (unordered)
])
def test_invalid_combinable_fixture_entries_are_rejected(http, combinable):
    body = fixture(restaurants=[restaurant(tables=COMBO_TABLES, combinable=combinable)])
    resp = http.post("/_test/reset", json=body)
    assert error_code(resp) == (422, "validation_failed")


def test_valid_combinable_fixture_is_echoed_on_restaurant_detail(reset, http):
    reset(fixture(restaurants=[combo_restaurant()]))
    detail = http.get("/restaurants/r_main").json()
    assert detail["combinable"] == PAIRS


def test_restaurant_without_combinable_field_has_an_empty_list(reset, http):
    reset()   # the default fixture restaurant predates stage 2's combinable field
    detail = http.get("/restaurants/r_main").json()
    assert detail["combinable"] == []


# ---- availability -------------------------------------------------------------------

def test_availability_lists_singles_then_pairs_in_declared_order(reset, http):
    reset(fixture(restaurants=[combo_restaurant()]))
    date = future_date()
    slots = http.get("/availability", params={
        "restaurant_id": "r_main", "date": date, "party_size": "2"}).json()["slots"]
    slot = next(s for s in slots if s["starts_at_local"] == f"{date}T19:00")
    assert [o["table_ids"] for o in slot["available_options"]] == [
        ["a"], ["b"], ["c"], ["a", "b"], ["b", "c"]]
    assert [o["capacity"] for o in slot["available_options"]] == [2, 4, 6, 6, 10]
    assert slot["available_table_ids"] == ["a", "b", "c"]


def test_availability_excludes_a_pair_once_a_member_table_is_booked(combo_world, http):
    alice, _ = combo_world
    date = future_date()
    at = f"{date}T19:00"
    assert alice.book(rid="r_main", table="b", at=at, party=2).status_code == 201
    slots = http.get("/availability", params={
        "restaurant_id": "r_main", "date": date, "party_size": "2"}).json()["slots"]
    slot = next(s for s in slots if s["starts_at_local"] == at)
    ids = [o["table_ids"] for o in slot["available_options"]]
    assert ["b"] not in ids and ["a", "b"] not in ids and ["b", "c"] not in ids
    assert ["a"] in ids and ["c"] in ids
    assert slot["available_table_ids"] == ["a", "c"]


def test_availability_options_require_party_size_le_pair_capacity(reset, http):
    reset(fixture(restaurants=[combo_restaurant()]))
    date = future_date()
    slots = http.get("/availability", params={
        "restaurant_id": "r_main", "date": date, "party_size": "7"}).json()["slots"]
    slot = next(s for s in slots if s["starts_at_local"] == f"{date}T19:00")
    # only b+c (capacity 10) can seat 7; a+b (capacity 6) cannot, nor any single table
    assert slot["available_options"] == [{"table_ids": ["b", "c"], "capacity": 10}]
    assert slot["available_table_ids"] == []


# ---- POST /reservations --------------------------------------------------------------

def test_booking_a_combo_returns_table_ids_without_table_id(combo_world):
    alice, _ = combo_world
    resp = alice.book(rid="r_main", table=None, table_ids=["b", "a"],
                      at=f"{future_date()}T19:00", party=6)
    assert resp.status_code == 201
    body = resp.json()
    assert body["table_ids"] == ["a", "b"]    # normalised to declared `combinable` order
    assert "table_id" not in body


def test_booking_a_single_table_still_returns_table_id(combo_world):
    alice, _ = combo_world
    resp = alice.book(rid="r_main", table="a", at=f"{future_date()}T19:00", party=2)
    body = resp.json()
    assert body["table_id"] == "a" and body["table_ids"] == ["a"]


def test_both_table_id_and_table_ids_is_rejected(combo_world):
    alice, _ = combo_world
    resp = alice.book(rid="r_main", table="a", table_ids=["a", "b"],
                      at=f"{future_date()}T19:00", party=2)
    assert error_code(resp) == (422, "validation_failed")


def test_pair_not_declared_combinable_is_rejected(combo_world):
    alice, _ = combo_world
    resp = alice.book(rid="r_main", table=None, table_ids=["a", "c"],
                      at=f"{future_date()}T19:00", party=4)
    assert error_code(resp) == (422, "combination_not_allowed")


def test_more_than_two_tables_is_rejected(combo_world):
    alice, _ = combo_world
    resp = alice.book(rid="r_main", table=None, table_ids=["a", "b", "c"],
                      at=f"{future_date()}T19:00", party=4)
    assert error_code(resp) == (422, "combination_not_allowed")


def test_duplicate_table_id_in_set_is_rejected(combo_world):
    alice, _ = combo_world
    resp = alice.book(rid="r_main", table=None, table_ids=["a", "a"],
                      at=f"{future_date()}T19:00", party=2)
    assert error_code(resp) == (422, "validation_failed")


def test_unknown_table_in_a_pair_is_not_found(combo_world):
    alice, _ = combo_world
    resp = alice.book(rid="r_main", table=None, table_ids=["a", "nope"],
                      at=f"{future_date()}T19:00", party=2)
    assert error_code(resp) == (404, "not_found")


def test_party_exceeding_combo_capacity_is_rejected(combo_world):
    alice, _ = combo_world
    resp = alice.book(rid="r_main", table=None, table_ids=["a", "b"],
                      at=f"{future_date()}T19:00", party=7)
    assert error_code(resp) == (422, "party_exceeds_capacity")


def test_combo_booking_blocked_by_overlap_on_either_member_table(combo_world):
    alice, bruno = combo_world
    at = f"{future_date()}T19:00"
    assert bruno.book(rid="r_main", table="c", at=at, party=2).status_code == 201
    resp = alice.book(rid="r_main", table=None, table_ids=["b", "c"], at=at, party=6)
    assert error_code(resp) == (409, "table_unavailable")


def test_single_table_request_format_still_works_at_a_combinable_restaurant(combo_world):
    alice, _ = combo_world
    resp = alice.book(rid="r_main", table="c", at=f"{future_date()}T19:00", party=3)
    assert resp.status_code == 201 and resp.json()["table_id"] == "c"


# ---- PATCH and cancel -----------------------------------------------------------------

def test_patch_can_move_a_single_table_into_a_combo_and_back(combo_world):
    alice, _ = combo_world
    at = f"{future_date()}T19:00"
    ref = alice.book(rid="r_main", table="a", at=at, party=2).json()["reference"]

    grown = alice.patch(f"/reservations/{ref}", json={"table_ids": ["b", "a"], "party_size": 6})
    assert grown.status_code == 200
    body = grown.json()
    assert body["table_ids"] == ["a", "b"] and "table_id" not in body
    assert body["reference"] == ref

    back = alice.patch(f"/reservations/{ref}", json={"table_id": "c", "party_size": 2})
    assert back.status_code == 200 and back.json()["table_id"] == "c"


def test_cancelling_a_combo_frees_every_member_table(combo_world):
    alice, bruno = combo_world
    at = f"{future_date()}T19:00"
    ref = alice.book(rid="r_main", table=None, table_ids=["a", "b"],
                     at=at, party=5).json()["reference"]
    assert alice.post(f"/reservations/{ref}/cancel").status_code == 200
    assert bruno.book(rid="r_main", table="a", at=at, party=2).status_code == 201
    assert bruno.book(rid="r_main", table="b", at=at, party=2).status_code == 201


def test_patch_rejects_an_undeclared_pair(combo_world):
    alice, _ = combo_world
    at = f"{future_date()}T19:00"
    ref = alice.book(rid="r_main", table="a", at=at, party=2).json()["reference"]
    resp = alice.patch(f"/reservations/{ref}", json={"table_ids": ["a", "c"]})
    assert error_code(resp) == (422, "combination_not_allowed")


# ---- reservation-moves ----------------------------------------------------------------

def test_moves_accept_table_ids_per_item(combo_world):
    alice, _ = combo_world
    at = f"{future_date()}T19:00"
    first = alice.book(rid="r_main", table="a", at=at, party=2).json()["reference"]
    second = alice.book(rid="r_main", table="c", at=at, party=2).json()["reference"]
    resp = alice.post("/reservation-moves", json={"moves": [
        {"reference": first, "table_ids": ["b", "a"], "party_size": 5},
        {"reference": second, "table_id": "c"},
    ]}, key=new_key())
    assert resp.status_code == 201
    bodies = {b["reference"]: b for b in resp.json()["reservations"]}
    assert bodies[first]["table_ids"] == ["a", "b"] and "table_id" not in bodies[first]
    assert bodies[second]["table_id"] == "c"


def test_moves_reject_overlap_between_a_listed_combo_and_an_unlisted_booking(combo_world):
    alice, bruno = combo_world
    at = f"{future_date()}T19:00"
    ref = alice.book(rid="r_main", table="a", at=at, party=2).json()["reference"]
    assert bruno.book(rid="r_main", table="c", at=at, party=2).status_code == 201
    resp = alice.post("/reservation-moves", json={
        "moves": [{"reference": ref, "table_ids": ["b", "c"]}]}, key=new_key())
    assert error_code(resp) == (409, "table_unavailable")


def test_moves_allow_a_swap_between_a_single_table_and_a_combo(combo_world):
    alice, _ = combo_world
    at = f"{future_date()}T19:00"
    first = alice.book(rid="r_main", table="c", at=at, party=3).json()["reference"]   # holds c
    second = alice.book(rid="r_main", table="a", at=at, party=2).json()["reference"]  # holds a
    # first wants a (held by second) + b; second wants c (held by first): a clean swap.
    resp = alice.post("/reservation-moves", json={"moves": [
        {"reference": first, "table_ids": ["a", "b"], "party_size": 6},
        {"reference": second, "table_id": "c"},
    ]}, key=new_key())
    assert resp.status_code == 201
    bodies = {b["reference"]: b for b in resp.json()["reservations"]}
    assert bodies[first]["table_ids"] == ["a", "b"]
    assert bodies[second]["table_id"] == "c"


# ---- concurrency -----------------------------------------------------------------------

def test_concurrent_combo_bookings_sharing_a_table_serialize_to_one_winner(combo_world):
    alice, bruno = combo_world
    at = f"{future_date()}T19:00"
    results: list[int] = []

    def attempt(client, table_ids):
        resp = client.book(rid="r_main", table=None, table_ids=table_ids, at=at, party=5)
        results.append(resp.status_code)

    # "a"+"b" and "b"+"c" both need table b: exactly one request may win it.
    threads = [threading.Thread(target=attempt, args=(alice, ["a", "b"])),
              threading.Thread(target=attempt, args=(bruno, ["b", "c"]))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [201, 409]


def test_concurrent_combo_and_single_table_bookings_do_not_double_book(combo_world):
    alice, bruno = combo_world
    at = f"{future_date()}T19:00"
    results: list[int] = []

    def book_combo():
        results.append(alice.book(rid="r_main", table=None, table_ids=["a", "b"],
                                  at=at, party=5).status_code)

    def book_single():
        results.append(bruno.book(rid="r_main", table="a", at=at, party=2).status_code)

    threads = [threading.Thread(target=book_combo), threading.Thread(target=book_single)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [201, 409]


# ---- cross-version import: accepting a stage-1-shaped export --------------------------

def test_import_accepts_a_stage1_shaped_export(reset, http):
    """Stage 1 exports carry a singular `table_id` and restaurants with no `combinable`
    field at all; stage 2 must accept that shape unchanged (stage-2.md, "Existing clients
    after an upgrade")."""
    reset(fixture(users=["alice"], restaurants=[restaurant(rid="r_legacy")]))
    stage1_hash = http.get("/_test/export").json()["state"]["users"][0]["password_hash"]
    doc = {
        "track": "tablekeeper", "format_version": 1,
        "state": {
            "users": [{"id": "u_1", "email": "alice@test.example",
                       "display_name": "Alice", "password_hash": stage1_hash}],
            "tokens": [{"token": "legacy-token-1", "user_id": "u_1"}],
            "restaurants": [restaurant(rid="r_legacy")],   # no "combinable" key
            "reservations": [{
                "id": "res_legacy", "reference": "LEGACY1", "user_id": "u_1",
                "restaurant_id": "r_legacy", "table_id": "a",
                "party_size": 2, "starts_at_local": f"{future_date()}T19:00",
                "status": "confirmed", "created_at": "2026-01-01T00:00:00+00:00",
            }],
            "receipts": [{"user_id": "u_1", "method": "POST", "path": "/reservations",
                          "key": "legacy-key", "body": {"was": "the original request"},
                          "response": {"reference": "LEGACY1"}}],
        },
    }
    resp = http.post("/_test/import", json=doc)
    assert resp.status_code == 204, resp.text

    got = http.get("/reservations/LEGACY1", headers={"Authorization": "Bearer legacy-token-1"})
    assert got.status_code == 200
    body = got.json()
    assert body["table_id"] == "a" and body["table_ids"] == ["a"]

    detail = http.get("/restaurants/r_legacy").json()
    assert detail["combinable"] == []

    # the pre-existing idempotency receipt is still a replay, not a fresh action
    replay = http.post("/reservations", json={"was": "the original request"},
                       headers={"Authorization": "Bearer legacy-token-1",
                               "Idempotency-Key": "legacy-key"})
    assert replay.status_code == 200 and replay.json() == {"reference": "LEGACY1"}
