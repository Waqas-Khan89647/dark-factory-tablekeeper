"""W1: health, error envelope, reset validation, auth and restaurants."""
import pytest

from conftest import Client, error_code, fixture, restaurant, user


def test_health(http):
    resp = http.get("/health")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}
    assert resp.headers["content-type"] == "application/json; charset=utf-8"


def test_unknown_route_is_404_envelope(http):
    assert error_code(http.get("/nope")) == (404, "not_found")


def test_wrong_method_is_405_envelope(http):
    assert error_code(http.delete("/restaurants")) == (405, "method_not_allowed")


# ---- reset -------------------------------------------------------------------

def test_reset_replaces_state(reset, http):
    reset(restaurants=[restaurant("r_one")])
    reset(restaurants=[restaurant("r_two")])
    assert [r["id"] for r in http.get("/restaurants").json()["restaurants"]] == ["r_two"]


def test_reset_invalidates_old_tokens(world, reset):
    alice, _ = world
    reset()
    assert error_code(alice.get("/reservations")) == (401, "unauthenticated")


def _bad_seed(**over):
    seed = {"id": "res_1", "reference": "SEED01", "user_id": "alice",
            "restaurant_id": "r_main", "table_id": "b",
            "starts_at_local": "2030-01-10T19:00", "party_size": 2}
    seed.update(over)
    return fixture(reservations=[seed])


@pytest.mark.parametrize("body", [
    fixture(users=["u" * 65]),
    fixture(restaurants=[restaurant("r" * 65)]),
    fixture(restaurants=[restaurant(tables=[{"id": "t" * 65, "capacity": 2}])]),
    fixture(restaurants=[restaurant(timezone="Mars/Olympus")]),
    fixture(restaurants=[restaurant(opens="23:00", closes="18:00")]),
    fixture(restaurants=[restaurant(slot=0)]),
    fixture(restaurants=[restaurant(tables=[{"id": "a", "capacity": 0}])]),
    fixture(users=["alice", "alice"]),
    _bad_seed(reference="lower01"),
    _bad_seed(reference="X"),
    _bad_seed(reference="TOO-LONG-WITH-DASH"),
    _bad_seed(id="r" * 65),
    _bad_seed(user_id="nobody"),
    _bad_seed(table_id="zz"),
    _bad_seed(starts_at_local="2026-03-29T02:30"),
    _bad_seed(party_size=0),
], ids=lambda b: "fixture")
def test_reset_rejects_bad_fixtures_without_changing_state(http, reset, body):
    reset(restaurants=[restaurant("r_keep")])
    assert error_code(http.post("/_test/reset", json=body)) == (422, "validation_failed")
    assert [r["id"] for r in http.get("/restaurants").json()["restaurants"]] == ["r_keep"]


def test_reset_with_non_object_is_400(http):
    assert error_code(http.post("/_test/reset", json=[1])) == (400, "malformed_request")
    assert error_code(http.post("/_test/reset", content=b"{")) == (400, "malformed_request")


# ---- auth ----------------------------------------------------------------------

def test_signup_then_token_works(world, http):
    resp = http.post("/auth/signup", json={"email": "new@x.io", "password": "12345678",
                                           "display_name": "New"})
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"user_id", "display_name", "token"}
    assert Client(http, body["token"]).get("/reservations").status_code == 200


@pytest.mark.parametrize("body,expected", [
    ({"email": "alice@test.example", "password": "12345678", "display_name": "A"},
     (409, "email_taken")),
    ({"email": "ALICE@test.example", "password": "12345678", "display_name": "A"},
     (409, "email_taken")),
    ({"email": "x@y.z", "password": "1234567", "display_name": "A"},
     (422, "validation_failed")),
    ({"email": "no-at-sign", "password": "12345678", "display_name": "A"},
     (422, "validation_failed")),
    ({"email": "a@b@c", "password": "12345678", "display_name": "A"},
     (422, "validation_failed")),
    ({"password": "12345678", "display_name": "A"}, (422, "validation_failed")),
    ({"email": 17, "password": "12345678", "display_name": "A"}, (400, "malformed_request")),
    ({"email": "x@y.z", "password": ["x"], "display_name": "A"}, (400, "malformed_request")),
])
def test_signup_rejections(world, http, body, expected):
    assert error_code(http.post("/auth/signup", json=body)) == expected


def test_signup_body_must_be_an_object(world, http):
    assert error_code(http.post("/auth/signup", json="x")) == (400, "malformed_request")


@pytest.mark.parametrize("email,password", [
    ("alice@test.example", "wrong password"), ("ghost@test.example", "long enough")])
def test_bad_login_is_401(world, http, email, password):
    resp = http.post("/auth/login", json={"email": email, "password": password})
    assert error_code(resp) == (401, "unauthenticated")


def test_several_tokens_stay_valid(world, login):
    first, second = login("alice"), login("alice")
    assert first.token != second.token
    assert first.get("/reservations").status_code == 200
    assert second.get("/reservations").status_code == 200


@pytest.mark.parametrize("header", ["", "Bearer", "Bearer ", "Basic abc", "Bearer nope",
                                    "token"])
def test_bad_authorization_headers_are_401(world, http, header):
    resp = http.get("/reservations", headers={"Authorization": header})
    assert error_code(resp) == (401, "unauthenticated")


def test_passwords_are_not_stored_in_plaintext(world, http):
    exported = http.get("/_test/export").text
    assert "long enough" not in exported and "scrypt$" in exported


# ---- restaurants --------------------------------------------------------------

def test_restaurant_list_and_detail(reset, http):
    reset(restaurants=[restaurant("r_1"), restaurant("r_2", timezone="America/New_York")])
    listed = http.get("/restaurants").json()["restaurants"]
    assert listed == [
        {"id": "r_1", "name": "Restaurant r_1", "timezone": "Europe/Berlin"},
        {"id": "r_2", "name": "Restaurant r_2", "timezone": "America/New_York"}]
    detail = http.get("/restaurants/r_1").json()
    source = restaurant("r_1")
    for field in ("slot_minutes", "reservation_duration_minutes",
                  "cancellation_cutoff_minutes", "opening_hours", "tables", "timezone"):
        assert detail[field] == source[field], field


def test_unknown_restaurant_is_404(world, http):
    assert error_code(http.get("/restaurants/r_x")) == (404, "not_found")


def test_seeded_users_can_log_in(reset, http):
    reset(body=fixture(users=["solo"]))
    resp = http.post("/auth/login", json={"email": user("solo")["email"],
                                          "password": user("solo")["password"]})
    assert resp.status_code == 200 and resp.json()["user_id"] == "solo"
