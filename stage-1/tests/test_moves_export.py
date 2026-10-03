"""W4: atomic reservation moves (§11) and export/import (§10)."""
import copy

import pytest

from conftest import Client, error_code, future_date, new_key, restaurant

DAY = future_date()


def at(hhmm: str) -> str:
    return f"{DAY}T{hhmm}"


def move(client, moves, key=None):
    return client.post("/reservation-moves", json={"moves": moves}, key=key or new_key())


@pytest.fixture
def two(world):
    """Alice holds a 2-top on table a and a 4-top on table b, both at 19:00."""
    alice, bruno = world
    first = alice.book(table="a", party=2, at=at("19:00")).json()
    second = alice.book(table="b", party=2, at=at("19:00")).json()
    return alice, bruno, first, second


def snapshot(client, refs):
    return [client.get(f"/reservations/{r}").json() for r in refs]


# ---- moves ---------------------------------------------------------------------

def test_swap_tables_atomically(two):
    alice, _, first, second = two
    resp = move(alice, [{"reference": first["reference"], "table_id": "b"},
                        {"reference": second["reference"], "table_id": "a"}])
    assert resp.status_code == 201
    out = resp.json()["reservations"]
    assert [(r["reference"], r["table_id"]) for r in out] == [
        (first["reference"], "b"), (second["reference"], "a")]
    assert out[0]["reservation_id"] == first["reservation_id"]
    assert out[0]["created_at"] == first["created_at"]


def test_no_op_move_returns_unchanged_booking(two):
    alice, _, first, _ = two
    resp = move(alice, [{"reference": first["reference"], "note": "ignored"}])
    assert resp.status_code == 201 and resp.json()["reservations"] == [first]


def test_replay_returns_original_even_after_changes(two):
    alice, _, first, second = two
    body = [{"reference": first["reference"], "table_id": "c"}]
    original = move(alice, body, key="batch")
    alice.post(f"/reservations/{first['reference']}/cancel")
    replay = move(alice, body, key="batch")
    assert (original.status_code, replay.status_code) == (201, 200)
    assert replay.json() == original.json()
    assert error_code(move(alice, [{"reference": second["reference"]}], key="batch")) == \
        (409, "idempotency_key_reuse")


def test_failure_changes_nothing_and_key_stays_reusable(two):
    alice, bruno, first, second = two
    bruno.book(table="c", party=2, at=at("19:30"))
    before = snapshot(alice, [first["reference"], second["reference"]])
    moves = [{"reference": first["reference"], "table_id": "b"},
             {"reference": second["reference"], "table_id": "c"}]
    assert error_code(move(alice, moves, key="k")) == (409, "table_unavailable")
    assert snapshot(alice, [first["reference"], second["reference"]]) == before
    ok = [{"reference": first["reference"], "table_id": "b"},
          {"reference": second["reference"], "table_id": "a"}]
    assert move(alice, ok, key="k").status_code == 201


def test_overlap_among_results_is_409(two):
    alice, _, first, second = two
    moves = [{"reference": first["reference"], "table_id": "c"},
             {"reference": second["reference"], "table_id": "c",
              "starts_at_local": at("20:00")}]
    assert error_code(move(alice, moves)) == (409, "table_unavailable")


def test_unchanged_listed_booking_keeps_its_occupancy(two):
    alice, _, first, second = two
    moves = [{"reference": first["reference"]},
             {"reference": second["reference"], "table_id": "a"}]
    assert error_code(move(alice, moves)) == (409, "table_unavailable")


@pytest.mark.parametrize("body", [
    {}, {"moves": []}, {"moves": "x"}, {"moves": [1]}, {"moves": [{}]},
    {"moves": [{"reference": 5}]},
    {"moves": [{"reference": f"R{i:05d}"} for i in range(9)]},
])
def test_bad_shape_is_422(world, body):
    alice, _ = world
    resp = alice.post("/reservation-moves", json=body, key=new_key())
    assert error_code(resp) == (422, "validation_failed")


def test_duplicate_references_are_422(two):
    alice, _, first, _ = two
    ref = first["reference"]
    assert error_code(move(alice, [{"reference": ref}, {"reference": ref}])) == \
        (422, "validation_failed")


def test_unknown_or_foreign_reference_is_404(two):
    alice, bruno, first, _ = two
    assert error_code(move(alice, [{"reference": "NOPE99"}])) == (404, "not_found")
    assert error_code(move(bruno, [{"reference": first["reference"]}])) == \
        (404, "not_found")


def test_different_restaurants_is_422(reset, login):
    reset(restaurants=[restaurant("r_main"), restaurant("r_two")])
    alice = login("alice")
    one = alice.book(rid="r_main").json()["reference"]
    two_ = alice.book(rid="r_two").json()["reference"]
    assert error_code(move(alice, [{"reference": one}, {"reference": two_}])) == \
        (422, "validation_failed")


def test_cancelled_booking_is_409(two):
    alice, _, first, _ = two
    alice.post(f"/reservations/{first['reference']}/cancel")
    assert error_code(move(alice, [{"reference": first["reference"]}])) == \
        (409, "reservation_cancelled")


def test_errors_follow_input_order_and_cutoff_comes_first(reset, login, http):
    reset(restaurants=[restaurant(cutoff=0)])
    alice = login("alice")
    past = alice.book(at="2020-01-10T19:00", table="a", party=2).json()["reference"]
    future = alice.book(table="b").json()["reference"]
    assert error_code(move(alice, [{"reference": future, "table_id": "zz"},
                                   {"reference": past, "table_id": "c"}])) == \
        (404, "not_found")
    assert error_code(move(alice, [{"reference": past, "table_id": "zz"},
                                   {"reference": future, "table_id": "zz"}])) == \
        (409, "cutoff_passed")


def test_item_field_rules_match_patch(two):
    alice, _, first, _ = two
    ref = first["reference"]
    assert error_code(move(alice, [{"reference": ref, "starts_at_local": at("19:15")}])) \
        == (422, "not_on_slot_grid")
    assert error_code(move(alice, [{"reference": ref, "party_size": 3}])) == \
        (422, "party_exceeds_capacity")


def test_moves_need_auth_and_key(two, http):
    alice, _, first, _ = two
    body = {"moves": [{"reference": first["reference"]}]}
    assert error_code(http.post("/reservation-moves", json=body)) == (401, "unauthenticated")
    assert error_code(alice.post("/reservation-moves", json=body)) == \
        (400, "missing_idempotency_key")


# ---- export / import --------------------------------------------------------------

def test_round_trip_preserves_everything(two, http, reset, login):
    alice, _, first, second = two
    move_body = [{"reference": first["reference"], "table_id": "c"}]
    moved = move(alice, move_body, key="batch").json()
    booked_key_body = {"restaurant_id": "r_main", "table_id": "b",
                       "starts_at_local": at("21:00"), "party_size": 2}
    booked = alice.post("/reservations", json=booked_key_body, key="single").json()
    exported = http.get("/_test/export")
    assert exported.status_code == 200
    document = exported.json()
    assert document["track"] == "tablekeeper" and document["format_version"] == 1

    reset()
    assert error_code(alice.get("/reservations")) == (401, "unauthenticated")
    assert http.post("/_test/import", json=document).status_code == 204
    assert http.post("/_test/import", json=document).status_code == 204  # replace, not merge

    listed = alice.get("/reservations").json()["reservations"]    # old token still valid
    assert len(listed) == 3
    assert alice.get(f"/reservations/{booked['reference']}").json() == booked
    replay = alice.post("/reservations", json=booked_key_body, key="single")
    assert replay.status_code == 200 and replay.json() == booked
    batch = move(alice, move_body, key="batch")
    assert batch.status_code == 200 and batch.json() == moved
    assert http.get("/_test/export").json() == document       # replays changed nothing
    assert login("alice").get("/reservations").status_code == 200   # hash still verifies


def test_export_is_a_snapshot(world, http):
    alice, _ = world
    document = http.get("/_test/export").json()
    frozen = copy.deepcopy(document)
    alice.book()
    assert document == frozen and http.get("/_test/export").json() != frozen


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("track"),
    lambda d: d.update(track="other"),
    lambda d: d.update(format_version=2),
    lambda d: d.update(format_version=True),
    lambda d: d.update(format_version="1"),
    lambda d: d.pop("state"),
    lambda d: d.update(state=[]),
    lambda d: d["state"].update(users=[{"id": "x"}]),
    lambda d: d["state"]["users"][0].update(password_hash="plain"),
    lambda d: d["state"]["tokens"].append({"token": "t", "user_id": "ghost"}),
    lambda d: d["state"]["reservations"][0].update(reference="bad"),
    lambda d: d["state"]["reservations"][0].update(table_id="zz"),
    lambda d: d["state"]["reservations"][0].update(status="maybe"),
    lambda d: d["state"]["receipts"][0].pop("response"),
    lambda d: d["state"]["restaurants"][0].update(timezone="Nowhere/City"),
    lambda d: d["state"]["tokens"].append({"token": "zz", "user_id": ["x"]}),
    lambda d: d["state"]["receipts"][0].update(user_id={"a": 1}),
    lambda d: d["state"]["reservations"][0].update(user_id=["x"]),
    lambda d: d["state"]["users"][0].update(id=["x"]),
    lambda d: d["state"]["reservations"][0].update(created_at="yesterday"),
    lambda d: d["state"]["reservations"][0].update(created_at="2030-01-01T10:00:00"),
    lambda d: d["state"].update(tokens=None),
    lambda d: d["state"].pop("receipts"),
    lambda d: d["state"]["reservations"].append(
        dict(d["state"]["reservations"][0], id="res_dup", reference="DUPL1CATE")),
])
def test_invalid_import_is_422_and_changes_nothing(world, http, mutate):
    alice, _ = world
    alice.book()
    document = http.get("/_test/export").json()
    before = http.get("/_test/export").json()
    mutate(document)
    assert error_code(http.post("/_test/import", json=document)) == \
        (422, "validation_failed")
    assert http.get("/_test/export").json() == before


def test_import_non_object_is_400(world, http):
    assert error_code(http.post("/_test/import", content=b"nope")) == (400, "malformed_request")
    assert error_code(http.post("/_test/import", json=[])) == (400, "malformed_request")


def test_import_then_reset_clears_everything(two, http, reset):
    alice, _, _, _ = two
    document = http.get("/_test/export").json()
    http.post("/_test/import", json=document)
    reset()
    assert error_code(alice.get("/reservations")) == (401, "unauthenticated")


def test_imported_state_blocks_double_booking(two, http, reset, login):
    alice, _, first, _ = two
    document = http.get("/_test/export").json()
    reset()
    http.post("/_test/import", json=document)
    bruno = Client(http, login("bruno").token)
    assert error_code(bruno.book(table="a", party=2, at=at("19:00"))) == \
        (409, "table_unavailable")


def test_nested_receipt_survives_export_import_and_replays(world, http, reset):
    alice, _ = world
    deep = 1
    for _ in range(58):   # the batch body nests 3 more levels, inside the 64-level limit
        deep = [deep]
    first = alice.book(key="deep", x=deep)
    assert first.status_code == 201
    batch_body = [{"reference": first.json()["reference"], "extra": deep}]
    moved = move(alice, batch_body, key="deep-batch")
    assert moved.status_code == 201
    document = http.get("/_test/export")
    assert document.status_code == 200
    reset()
    assert http.post("/_test/import", json=document.json()).status_code == 204
    replay = alice.book(key="deep", x=deep)
    assert replay.status_code == 200 and replay.json() == first.json()
    batch_replay = move(alice, batch_body, key="deep-batch")
    assert batch_replay.status_code == 200 and batch_replay.json() == moved.json()


def test_over_deep_move_body_is_400(world):
    alice, _ = world
    value = []
    for _ in range(899):
        value = [value]
    resp = alice.post("/reservation-moves", json={"moves": [], "x": value}, key=new_key())
    assert error_code(resp) == (400, "malformed_request")
