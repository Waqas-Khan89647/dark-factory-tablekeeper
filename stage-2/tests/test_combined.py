"""Stage 2 W1: combined tables -- model, availability, create, PATCH, moves, seeds."""
import pytest

from conftest import error_code, fixture, future_date, new_key, restaurant

DAY = future_date()
TABLES = [
    {"id": "a", "label": "A", "capacity": 2},
    {"id": "b", "label": "B", "capacity": 4},
    {"id": "c", "label": "C", "capacity": 6},
    {"id": "d", "label": "D", "capacity": 2},
]
PAIRS = [["a", "b"], ["c", "b"]]      # b+c is declared in the order c, b


def at(hhmm: str) -> str:
    return f"{DAY}T{hhmm}"


def combo_restaurant(**kwargs) -> dict:
    kwargs.setdefault("tables", TABLES)
    kwargs.setdefault("combinable", PAIRS)
    return restaurant(**kwargs)


@pytest.fixture
def pairs(reset, login):
    reset(restaurants=[combo_restaurant()])
    return login("alice"), login("bruno")


def book(client, table_ids, *, hhmm="19:00", party=2, key=None, **extra):
    body = {"restaurant_id": "r_main", "table_ids": table_ids,
            "starts_at_local": at(hhmm), "party_size": party, **extra}
    return client.post("/reservations", json=body, key=key or new_key())


def slot(http, hhmm="19:00", party=1):
    resp = http.get(f"/availability?restaurant_id=r_main&date={DAY}&party_size={party}")
    assert resp.status_code == 200
    return next(s for s in resp.json()["slots"] if s["starts_at_local"] == at(hhmm))


def options(http, hhmm="19:00", party=1):
    return [(o["table_ids"], o["capacity"]) for o in slot(http, hhmm, party)["available_options"]]


# ---- model and fixture -------------------------------------------------------------

def test_restaurant_json_carries_combinable(pairs, http):
    body = http.get("/restaurants/r_main").json()
    assert body["combinable"] == [["a", "b"], ["c", "b"]]


def test_combinable_defaults_to_empty(world, http):
    assert http.get("/restaurants/r_main").json()["combinable"] == []


@pytest.mark.parametrize("bad", [
    [["a", "z"]], [["a", "a"]], [["a", "b", "c"]], [["a"]], ["a"], [[1, 2]], "a,b",
])
def test_reset_rejects_bad_pairs(http, bad):
    body = fixture(restaurants=[combo_restaurant(combinable=bad)])
    assert error_code(http.post("/_test/reset", json=body)) == (422, "validation_failed")


def test_a_repeated_pair_is_refused(http):
    """This tree treats a pair declared twice (in either order) as an invalid fixture."""
    body = fixture(restaurants=[combo_restaurant(combinable=[["a", "b"], ["b", "a"]])])
    assert error_code(http.post("/_test/reset", json=body)) == (422, "validation_failed")


# ---- availability ---------------------------------------------------------------

def test_options_list_singles_then_pairs_in_declared_order(pairs, http):
    assert options(http, party=1) == [
        (["a"], 2), (["b"], 4), (["c"], 6), (["d"], 2), (["a", "b"], 6), (["c", "b"], 10)]
    assert slot(http, party=1)["available_table_ids"] == ["a", "b", "c", "d"]


def test_options_filter_by_summed_capacity(pairs, http):
    assert options(http, party=6) == [(["c"], 6), (["a", "b"], 6), (["c", "b"], 10)]
    assert slot(http, party=6)["available_table_ids"] == ["c"]
    assert options(http, party=7) == [(["c", "b"], 10)]
    assert slot(http, party=7)["available_table_ids"] == []
    assert options(http, party=11) == []


def test_a_taken_member_removes_every_pair_holding_it(pairs, http):
    alice, _ = pairs
    assert alice.book(table="b", party=2, at=at("19:00")).status_code == 201
    assert options(http, "19:00") == [(["a"], 2), (["c"], 6), (["d"], 2)]
    assert options(http, "20:00") == [(["a"], 2), (["c"], 6), (["d"], 2)]  # still overlaps
    assert (["a", "b"], 6) in options(http, "20:30")                        # half-open


def test_a_pair_booking_takes_both_tables_out_of_availability(pairs, http):
    alice, _ = pairs
    assert book(alice, ["a", "b"], party=5).status_code == 201
    s = slot(http, "19:30")
    assert s["available_table_ids"] == ["c", "d"]
    assert [o["table_ids"] for o in s["available_options"]] == [["c"], ["d"]]


def test_cancelled_pair_frees_both_tables(pairs, http):
    alice, _ = pairs
    ref = book(alice, ["a", "b"], party=5).json()["reference"]
    assert alice.post(f"/reservations/{ref}/cancel").json()["status"] == "cancelled"
    assert slot(http)["available_table_ids"] == ["a", "b", "c", "d"]
    assert book(alice, ["a"]).status_code == 201
    assert book(alice, ["b"]).status_code == 201


# ---- create ---------------------------------------------------------------------

def test_pair_booking_shape(pairs):
    alice, _ = pairs
    resp = book(alice, ["a", "b"], party=6)
    assert resp.status_code == 201
    body = resp.json()
    assert body["table_ids"] == ["a", "b"] and "table_id" not in body
    assert alice.get(f"/reservations/{body['reference']}").json() == body
    assert alice.get("/reservations").json()["reservations"] == [body]


def test_pair_is_stored_in_combinable_order(pairs):
    alice, _ = pairs
    assert book(alice, ["b", "a"], party=3).json()["table_ids"] == ["a", "b"]
    assert book(alice, ["b", "c"], party=3, hhmm="21:00").json()["table_ids"] == ["c", "b"]


@pytest.mark.parametrize("request_tables", [{"table_ids": ["b"]}, {"table_id": "b"}])
def test_single_table_carries_table_id_and_table_ids(pairs, request_tables):
    alice, _ = pairs
    body = {"restaurant_id": "r_main", "starts_at_local": at("19:00"), "party_size": 2,
            **request_tables}
    resp = alice.post("/reservations", json=body, key=new_key())
    assert resp.status_code == 201
    assert (resp.json()["table_id"], resp.json()["table_ids"]) == ("b", ["b"])


@pytest.mark.parametrize("tables,party,expected", [
    (["a", "c"], 2, (422, "combination_not_allowed")),        # both exist, not declared
    (["a", "b", "c"], 2, (422, "combination_not_allowed")),   # never more than two
    (["a", "b", "d"], 2, (422, "combination_not_allowed")),
    (["a", "b"], 7, (422, "party_exceeds_capacity")),         # 2 + 4 < 7
    (["a", "a"], 2, (422, "validation_failed")),              # duplicate id
    (["a", "zz"], 2, (404, "not_found")),                     # unknown member
    ([], 2, (422, "validation_failed")),
])
def test_create_rule_table(pairs, tables, party, expected):
    alice, _ = pairs
    assert error_code(book(alice, tables, party=party)) == expected


def test_combining_is_not_transitive(pairs):
    alice, _ = pairs    # a+b and c+b are declared; a+c is not
    assert error_code(book(alice, ["a", "c"])) == (422, "combination_not_allowed")
    assert error_code(book(alice, ["c", "a"])) == (422, "combination_not_allowed")


def test_party_may_fill_the_summed_capacity_exactly(pairs):
    alice, _ = pairs
    assert book(alice, ["a", "b"], party=6).status_code == 201


def test_sending_table_id_and_table_ids_is_422(pairs):
    alice, _ = pairs
    resp = book(alice, ["a"], table_id="a")
    assert error_code(resp) == (422, "validation_failed")


@pytest.mark.parametrize("value,expected", [
    ("a", (400, "malformed_request")),
    ([1], (400, "malformed_request")),
    (["a", None], (400, "malformed_request")),
    ({"0": "a"}, (400, "malformed_request")),
    (None, (422, "validation_failed")),
])
def test_table_ids_must_be_a_list_of_strings(pairs, value, expected):
    alice, _ = pairs
    assert error_code(book(alice, value)) == expected


def test_missing_tables_is_422(pairs):
    alice, _ = pairs
    body = {"restaurant_id": "r_main", "starts_at_local": at("19:00"), "party_size": 2}
    assert error_code(alice.post("/reservations", json=body, key=new_key())) == \
        (422, "validation_failed")


@pytest.mark.parametrize("taken", ["a", "b"])
def test_any_taken_member_is_409(pairs, taken):
    alice, bruno = pairs
    assert bruno.book(table=taken, party=2, at=at("19:30")).status_code == 201
    assert error_code(book(alice, ["a", "b"], party=5)) == (409, "table_unavailable")


def test_pair_blocks_later_singles_and_pairs_on_either_member(pairs):
    alice, bruno = pairs
    assert book(alice, ["a", "b"], party=5).status_code == 201
    assert error_code(bruno.book(table="a", party=2, at=at("20:00"))) == \
        (409, "table_unavailable")
    assert error_code(bruno.book(table="b", party=2, at=at("18:00"))) == \
        (409, "table_unavailable")
    assert error_code(book(bruno, ["c", "b"], party=8)) == (409, "table_unavailable")
    assert bruno.book(table="c", party=2, at=at("19:00")).status_code == 201
    assert book(bruno, ["a", "b"], hhmm="20:30", party=5).status_code == 201


@pytest.mark.parametrize("tables,hhmm,party,expected", [
    (["a", "c"], "19:15", 99, (422, "combination_not_allowed")),  # before time checks
    (["a", "b"], "19:15", 99, (422, "not_on_slot_grid")),         # time before capacity
    (["a", "b"], "22:00", 99, (422, "outside_opening_hours")),
    (["a", "b"], "19:00", 99, (422, "party_exceeds_capacity")),   # capacity before 409
    (["a", "zz"], "19:15", 99, (404, "not_found")),               # 404 before time checks
    (["a", "zz", "c"], "19:15", 99, (422, "combination_not_allowed")),  # count is shape
])
def test_error_precedence(pairs, tables, hhmm, party, expected):
    alice, bruno = pairs
    bruno.book(table="a", party=2, at=at("19:00"))
    assert error_code(book(alice, tables, hhmm=hhmm, party=party)) == expected


def test_pair_replay_and_key_reuse(pairs):
    alice, _ = pairs
    first = book(alice, ["a", "b"], party=5, key="pair-key")
    again = book(alice, ["a", "b"], party=5, key="pair-key")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert error_code(book(alice, ["b", "a"], party=5, key="pair-key")) == \
        (409, "idempotency_key_reuse")


def test_rejected_pair_request_leaves_nothing_behind(pairs, http):
    alice, bruno = pairs
    bruno.book(table="b", party=2, at=at("19:00"))
    assert error_code(book(alice, ["a", "b"], party=5, key="k")) == (409, "table_unavailable")
    assert alice.get("/reservations").json()["reservations"] == []
    assert "a" in slot(http)["available_table_ids"]
    assert book(alice, ["a"], key="k").status_code == 201    # a failed key is reusable


# ---- PATCH ------------------------------------------------------------------------

def patch(client, ref, **changes):
    return client.patch(f"/reservations/{ref}", json=changes)


def test_patch_single_to_pair_and_back(pairs, http):
    alice, _ = pairs
    ref = alice.book(table="a", party=2, at=at("19:00")).json()["reference"]
    grown = patch(alice, ref, table_ids=["b", "a"], party_size=5)
    assert grown.status_code == 200
    assert grown.json()["table_ids"] == ["a", "b"] and "table_id" not in grown.json()
    assert slot(http)["available_table_ids"] == ["c", "d"]
    shrunk = patch(alice, ref, table_id="c")
    assert (shrunk.json()["table_id"], shrunk.json()["table_ids"]) == ("c", ["c"])
    assert slot(http)["available_table_ids"] == ["a", "b", "d"]


def test_patch_keeps_the_pair_when_tables_are_not_named(pairs):
    alice, _ = pairs
    ref = book(alice, ["a", "b"], party=5).json()["reference"]
    moved = patch(alice, ref, starts_at_local=at("20:00"), party_size=6)
    assert moved.json()["table_ids"] == ["a", "b"] and moved.json()["party_size"] == 6
    assert error_code(patch(alice, ref, party_size=7)) == (422, "party_exceeds_capacity")


@pytest.mark.parametrize("changes,expected", [
    ({"table_ids": ["a", "c"]}, (422, "combination_not_allowed")),
    ({"table_ids": ["a", "b", "c"]}, (422, "combination_not_allowed")),
    ({"table_ids": ["b", "b"]}, (422, "validation_failed")),
    ({"table_ids": ["c"], "table_id": "c"}, (422, "validation_failed")),
    ({"table_ids": "c"}, (400, "malformed_request")),
    ({"table_ids": ["c", "b"], "party_size": 11}, (422, "party_exceeds_capacity")),
    ({"table_ids": ["c", "b"]}, (409, "table_unavailable")),
])
def test_patch_rule_table_leaves_booking_unchanged(pairs, changes, expected):
    alice, bruno = pairs
    ref = alice.book(table="a", party=2, at=at("19:00")).json()["reference"]
    bruno.book(table="c", party=2, at=at("19:30"))
    before = alice.get(f"/reservations/{ref}").json()
    assert error_code(patch(alice, ref, **changes)) == expected
    assert alice.get(f"/reservations/{ref}").json() == before


def test_patch_pair_ignores_its_own_occupancy(pairs):
    alice, _ = pairs
    ref = book(alice, ["a", "b"], party=5).json()["reference"]
    assert patch(alice, ref, starts_at_local=at("19:30")).status_code == 200


# ---- moves ------------------------------------------------------------------------

def move(client, moves, key=None):
    return client.post("/reservation-moves", json={"moves": moves}, key=key or new_key())


def test_moves_swap_a_pair_and_a_single(pairs, http):
    alice, _ = pairs
    pair = book(alice, ["a", "b"], party=4).json()["reference"]
    single = alice.book(table="c", party=4, at=at("19:00")).json()["reference"]
    resp = move(alice, [{"reference": pair, "table_ids": ["c"]},
                        {"reference": single, "table_ids": ["b", "a"]}])
    assert resp.status_code == 201
    out = resp.json()["reservations"]
    assert [(r["reference"], r["table_ids"]) for r in out] == [
        (pair, ["c"]), (single, ["a", "b"])]
    assert out[0]["table_id"] == "c" and "table_id" not in out[1]
    assert slot(http)["available_table_ids"] == ["d"]


def test_moves_refuse_results_sharing_a_member(pairs):
    alice, _ = pairs
    first = alice.book(table="a", party=2, at=at("19:00")).json()["reference"]
    second = alice.book(table="d", party=2, at=at("19:30")).json()["reference"]
    before = [alice.get(f"/reservations/{r}").json() for r in (first, second)]
    resp = move(alice, [{"reference": first, "table_ids": ["a", "b"]},
                        {"reference": second, "table_ids": ["c", "b"]}], key="mk")
    assert error_code(resp) == (409, "table_unavailable")
    assert [alice.get(f"/reservations/{r}").json() for r in (first, second)] == before
    assert move(alice, [{"reference": first, "table_ids": ["a", "b"]}],
                key="mk").status_code == 201


def test_moves_refuse_overlap_with_an_unlisted_pair(pairs):
    alice, bruno = pairs
    book(bruno, ["c", "b"], party=8)
    mine = alice.book(table="a", party=2, at=at("19:00")).json()["reference"]
    assert error_code(move(alice, [{"reference": mine, "table_ids": ["a", "b"]}])) == \
        (409, "table_unavailable")


@pytest.mark.parametrize("item,expected", [
    ({"table_ids": ["a", "c"]}, (422, "combination_not_allowed")),
    ({"table_ids": ["a"], "table_id": "a"}, (422, "validation_failed")),
    ({"table_ids": ["a", "a"]}, (422, "validation_failed")),
    ({"table_ids": ["a", "b"], "party_size": 7}, (422, "party_exceeds_capacity")),
])
def test_move_rule_table(pairs, item, expected):
    alice, _ = pairs
    ref = alice.book(table="d", party=2, at=at("19:00")).json()["reference"]
    assert error_code(move(alice, [{"reference": ref, **item}])) == expected


def test_move_replay_returns_the_original_pair_response(pairs):
    alice, _ = pairs
    ref = alice.book(table="d", party=2, at=at("19:00")).json()["reference"]
    body = [{"reference": ref, "table_ids": ["a", "b"]}]
    first = move(alice, body, key="mv")
    alice.post(f"/reservations/{ref}/cancel")
    again = move(alice, body, key="mv")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()


# ---- seeded reservations ------------------------------------------------------------

def seed(**extra) -> dict:
    return {"id": f"s_{len(extra)}_{sorted(extra)}", "reference": "SEED0001",
            "user_id": "alice", "restaurant_id": "r_main",
            "starts_at_local": at("19:00"), "party_size": 2, **extra}


def test_seeded_pair_and_cancelled_seed(reset, login, http):
    reset(restaurants=[combo_restaurant()], reservations=[
        {**seed(table_ids=["b", "a"]), "id": "s1", "reference": "SEEDPAIR"},
        {**seed(table_id="c", status="cancelled"), "id": "s2", "reference": "SEEDGONE"},
        {**seed(table_ids=["d"]), "id": "s3", "reference": "SEEDONE1"},
    ])
    alice = login("alice")
    assert alice.get("/reservations/SEEDPAIR").json()["table_ids"] == ["a", "b"]
    gone = alice.get("/reservations/SEEDGONE").json()
    assert gone["status"] == "cancelled" and gone["table_id"] == "c"
    assert alice.get("/reservations/SEEDONE1").json()["table_id"] == "d"
    assert slot(http)["available_table_ids"] == ["c"]


def test_seeded_cancelled_booking_may_overlap(reset):
    reset(restaurants=[combo_restaurant()], reservations=[
        {**seed(table_id="a", status="cancelled"), "id": "s1", "reference": "SEEDONE1"},
        {**seed(table_ids=["a", "b"]), "id": "s2", "reference": "SEEDONE2"},
    ])


@pytest.mark.parametrize("extra", [
    {"table_id": "a", "table_ids": ["a"]},
    {"table_ids": ["a", "c"]},
    {"table_ids": ["a", "b", "c"]},
    {"table_ids": ["a", "a"]},
    {"table_ids": []},
    {},
])
def test_bad_seeds_are_refused(http, extra):
    body = fixture(restaurants=[combo_restaurant()], reservations=[seed(**extra)])
    assert error_code(http.post("/_test/reset", json=body)) == (422, "validation_failed")


def test_seeded_pair_overlapping_a_single_is_refused(http):
    body = fixture(restaurants=[combo_restaurant()], reservations=[
        {**seed(table_ids=["a", "b"]), "id": "s1", "reference": "SEEDONE1"},
        {**seed(table_id="b"), "id": "s2", "reference": "SEEDONE2",
         "starts_at_local": at("20:00")},
    ])
    assert error_code(http.post("/_test/reset", json=body)) == (422, "validation_failed")


# ---- export / import ------------------------------------------------------------------

def test_export_import_round_trip_keeps_pairs(pairs, http):
    alice, _ = pairs
    created = book(alice, ["c", "b"], party=8, key="pair-export")
    snapshot = http.get("/_test/export").json()
    assert http.post("/_test/import", json=snapshot).status_code == 204
    assert http.get("/_test/export").json() == snapshot
    assert http.get("/restaurants/r_main").json()["combinable"] == [["a", "b"], ["c", "b"]]
    again = book(alice, ["c", "b"], party=8, key="pair-export")
    assert (again.status_code, again.json()) == (200, created.json())
    assert error_code(book(alice, ["b"])) == (409, "table_unavailable")


def test_a_seed_status_other_than_cancelled_is_ignored(reset, login):
    """RUN.md: on a seeded reservation `status` counts only when it is "cancelled"."""
    reset(restaurants=[combo_restaurant()],
          reservations=[{**seed(table_id="a", status="pending"), "reference": "SEEDODD1"}])
    assert login("alice").get("/reservations/SEEDODD1").json()["status"] == "confirmed"
