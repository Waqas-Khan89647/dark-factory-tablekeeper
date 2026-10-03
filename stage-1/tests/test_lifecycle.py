"""W3: reading, cancelling and amending reservations; seeded bookings."""
import pytest

from conftest import error_code, fixture, future_date, restaurant

DAY = future_date()


def at(hhmm: str, day: str = DAY) -> str:
    return f"{day}T{hhmm}"


def free_tables(http, hhmm: str, party="2"):
    listed = http.get("/availability", params={
        "restaurant_id": "r_main", "date": DAY, "party_size": party}).json()["slots"]
    return next(s for s in listed if s["starts_at_local"] == at(hhmm))["available_table_ids"]


def seed(reference="SEED01", *, user_id="alice", table="b", when=None, rid="res_seed"):
    return {"id": rid, "reference": reference, "user_id": user_id,
            "restaurant_id": "r_main", "table_id": table,
            "starts_at_local": when or at("19:00"), "party_size": 4}


# ---- reads ---------------------------------------------------------------------

def test_list_is_own_and_starts_at_descending_with_cancelled(world):
    alice, bruno = world
    refs = [alice.book(at=at(t)).json()["reference"] for t in ("18:00", "21:00")]
    alice.book(at=at("19:30"), table="c")
    bruno.book(at=at("19:30"), table="a", party=2)
    assert alice.post(f"/reservations/{refs[0]}/cancel").status_code == 200
    listed = alice.get("/reservations").json()["reservations"]
    assert [r["starts_at_local"][-5:] for r in listed] == ["21:00", "19:30", "18:00"]
    assert listed[-1]["status"] == "cancelled"


def test_empty_list(world):
    alice, _ = world
    assert alice.get("/reservations").json() == {"reservations": []}


def test_get_is_owner_only(world):
    alice, bruno = world
    created = alice.book().json()
    assert alice.get(f"/reservations/{created['reference']}").json() == created
    assert error_code(bruno.get(f"/reservations/{created['reference']}")) == \
        (404, "not_found")
    assert error_code(alice.get("/reservations/NOPE99")) == (404, "not_found")


# ---- cancel ------------------------------------------------------------------------

def test_cancel_frees_table_and_is_repeatable(world, http):
    alice, _ = world
    ref = alice.book(table="b", at=at("19:00")).json()["reference"]
    assert "b" not in free_tables(http, "19:00")
    first = alice.post(f"/reservations/{ref}/cancel")
    assert first.status_code == 200 and first.json()["status"] == "cancelled"
    assert "b" in free_tables(http, "19:00")
    again = alice.post(f"/reservations/{ref}/cancel")
    assert again.status_code == 200 and again.json() == first.json()


def test_cancel_other_users_booking_is_404(world):
    alice, bruno = world
    ref = alice.book().json()["reference"]
    assert error_code(bruno.post(f"/reservations/{ref}/cancel")) == (404, "not_found")
    assert alice.get(f"/reservations/{ref}").json()["status"] == "confirmed"


def test_cancel_inside_cutoff_is_409(reset, login):
    reset(restaurants=[restaurant(cutoff=60 * 24 * 365)])
    alice = login("alice")
    ref = alice.book().json()["reference"]
    assert error_code(alice.post(f"/reservations/{ref}/cancel")) == (409, "cutoff_passed")


def test_cancel_of_a_past_booking_is_409(reset, login):
    reset(body=fixture(reservations=[seed(when="2020-06-01T19:00")]))
    alice = login("alice")
    assert error_code(alice.post("/reservations/SEED01/cancel")) == (409, "cutoff_passed")


def test_cancel_with_zero_cutoff_before_start(reset, login):
    reset(restaurants=[restaurant(cutoff=0)])
    alice = login("alice")
    ref = alice.book().json()["reference"]
    assert alice.post(f"/reservations/{ref}/cancel").status_code == 200


# ---- PATCH -------------------------------------------------------------------------

def test_patch_moves_table_and_keeps_identity(world, http):
    alice, _ = world
    created = alice.book(table="b", at=at("19:00")).json()
    resp = alice.patch(f"/reservations/{created['reference']}", json={"table_id": "c"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["table_id"] == "c"
    for field in ("reference", "reservation_id", "created_at", "starts_at"):
        assert body[field] == created[field], field
    assert "b" in free_tables(http, "19:00") and "c" not in free_tables(http, "19:00")


def test_patch_can_overlap_its_own_old_slot(world):
    alice, _ = world
    ref = alice.book(at=at("19:00")).json()["reference"]
    body = alice.patch(f"/reservations/{ref}", json={"starts_at_local": at("19:30")}).json()
    assert body["starts_at_local"] == at("19:30")
    assert body["ends_at"].startswith(f"{DAY}T21:00")


def test_patch_all_fields_and_empty_patch(world):
    alice, _ = world
    ref = alice.book(at=at("19:00")).json()["reference"]
    before = alice.get(f"/reservations/{ref}").json()
    assert alice.patch(f"/reservations/{ref}", json={}).json() == before
    body = alice.patch(f"/reservations/{ref}", json={
        "table_id": "a", "starts_at_local": at("20:00"), "party_size": 2,
        "restaurant_id": "ignored", "extra": 1}).json()
    assert (body["table_id"], body["starts_at_local"], body["party_size"]) == \
        ("a", at("20:00"), 2)


@pytest.mark.parametrize("change,expected", [
    ({"starts_at_local": at("19:15")}, (422, "not_on_slot_grid")),
    ({"starts_at_local": at("22:30")}, (422, "outside_opening_hours")),
    ({"starts_at_local": "2026-03-29T02:30"}, (422, "invalid_local_time")),
    ({"starts_at_local": at("19:00") + ":00"}, (422, "validation_failed")),
    ({"starts_at_local": None}, (422, "validation_failed")),
    ({"starts_at_local": 5}, (400, "malformed_request")),
    ({"table_id": "a"}, (422, "party_exceeds_capacity")),
    ({"table_id": "zz"}, (404, "not_found")),
    ({"table_id": 3}, (400, "malformed_request")),
    ({"party_size": 0}, (422, "validation_failed")),
    ({"party_size": "2"}, (422, "validation_failed")),
    ({"party_size": 9}, (422, "party_exceeds_capacity")),
])
def test_patch_validation_matches_create_and_changes_nothing(reset, login, change, expected):
    reset(restaurants=[restaurant(opens="00:00", closes="23:00")])
    alice = login("alice")
    created = alice.book(at=at("19:00")).json()
    ref = created["reference"]
    assert error_code(alice.patch(f"/reservations/{ref}", json=change)) == expected
    assert alice.get(f"/reservations/{ref}").json() == created


def test_patch_body_must_be_an_object(world):
    alice, _ = world
    ref = alice.book().json()["reference"]
    assert error_code(alice.patch(f"/reservations/{ref}", json=[1])) == \
        (400, "malformed_request")


def test_patch_onto_taken_table_is_409_and_keeps_original(world, http):
    alice, bruno = world
    bruno.book(table="c", at=at("19:30"))
    created = alice.book(table="b", at=at("19:00")).json()
    ref = created["reference"]
    assert error_code(alice.patch(f"/reservations/{ref}", json={"table_id": "c"})) == \
        (409, "table_unavailable")
    assert alice.get(f"/reservations/{ref}").json() == created
    assert "b" not in free_tables(http, "19:00")


def test_patch_cancelled_is_409(world):
    alice, _ = world
    ref = alice.book().json()["reference"]
    alice.post(f"/reservations/{ref}/cancel")
    assert error_code(alice.patch(f"/reservations/{ref}", json={"party_size": 2})) == \
        (409, "reservation_cancelled")


def test_patch_other_users_booking_is_404(world):
    alice, bruno = world
    ref = alice.book().json()["reference"]
    assert error_code(bruno.patch(f"/reservations/{ref}", json={"party_size": 2})) == \
        (404, "not_found")


def test_patch_cutoff_uses_current_start(reset, login):
    """A booking in the past cannot be moved into the future."""
    reset(body=fixture(reservations=[seed(when="2020-06-01T19:00")]))
    alice = login("alice")
    resp = alice.patch("/reservations/SEED01", json={"starts_at_local": at("19:00")})
    assert error_code(resp) == (409, "cutoff_passed")


def test_patch_needs_auth(world, http):
    alice, _ = world
    ref = alice.book().json()["reference"]
    assert error_code(http.patch(f"/reservations/{ref}", json={})) == (401, "unauthenticated")


# ---- seeded reservations -------------------------------------------------------------

def test_seeded_booking_occupies_and_belongs_to_its_user(reset, login, http):
    reset(body=fixture(reservations=[seed()]))
    alice, bruno = login("alice"), login("bruno")
    body = alice.get("/reservations/SEED01").json()
    assert body["status"] == "confirmed" and body["reservation_id"] == "res_seed"
    assert "b" not in free_tables(http, "19:00")
    assert error_code(bruno.get("/reservations/SEED01")) == (404, "not_found")
    assert error_code(bruno.book(table="b", at=at("19:00"))) == (409, "table_unavailable")


def test_generated_references_never_collide_with_seeded(reset, login):
    reset(body=fixture(reservations=[seed("AAAAAA"), seed("BBBBBB", table="c",
                                                          rid="res_2")]))
    alice = login("alice")
    refs = {alice.book(table="a", party=2, at=at(t)).json()["reference"]
            for t in ("18:00", "19:30", "21:00")}
    assert len(refs) == 3 and not refs & {"AAAAAA", "BBBBBB"}
