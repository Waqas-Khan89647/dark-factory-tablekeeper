"""Stage 2 W1: a stage-1 export imports unchanged into stage 2 (D8).

`data/stage1_export.json` was captured from the real stage-1 container: reset, a login
(Ada) and a signup (Bea), two bookings (Ada's one plays the "response lost before
export" booking), and a 4xx under `failed-key`. Its tokens are test-only.
"""
import copy
import json
import pathlib

import pytest

from conftest import Client, error_code

CAPTURE = json.loads((pathlib.Path(__file__).parent / "data" / "stage1_export.json")
                     .read_text())
EXPORT = CAPTURE["export"]
LOST = CAPTURE["lost_request"]


@pytest.fixture
def upgraded(http):
    assert http.post("/_test/import", json=copy.deepcopy(EXPORT)).status_code == 204
    return Client(http, CAPTURE["tokens"]["ada"]), Client(http, CAPTURE["tokens"]["bea"])


def test_capture_is_a_stage_1_export():
    reservations = EXPORT["state"]["reservations"]
    assert (EXPORT["track"], EXPORT["format_version"]) == ("tablekeeper", 1)
    assert all("table_id" in r and "table_ids" not in r for r in reservations)
    assert all("combinable" not in r for r in EXPORT["state"]["restaurants"])


def test_existing_tokens_stay_signed_in(upgraded):
    ada, bea = upgraded
    assert ada.get("/reservations").status_code == 200
    assert bea.get("/reservations").status_code == 200


def test_old_references_look_up_in_the_stage_2_shape(upgraded):
    ada, bea = upgraded
    lost = ada.get(f"/reservations/{LOST['response']['reference']}").json()
    assert (lost["table_id"], lost["table_ids"]) == ("t_2", ["t_2"])
    for key in ("reservation_id", "reference", "created_at", "starts_at", "status"):
        assert lost[key] == LOST["response"][key]
    seeded = ada.get("/reservations/SEEDREF1").json()
    assert seeded["table_ids"] == ["t_3"] and seeded["status"] == "confirmed"
    assert bea.get(f"/reservations/{CAPTURE['bea_reference']}").json()["table_id"] == "t_1"
    assert error_code(bea.get("/reservations/SEEDREF1")) == (404, "not_found")


def test_lost_booking_retry_recovers_the_original_confirmation(upgraded):
    ada, _ = upgraded
    retry = ada.post("/reservations", json=LOST["body"], key=LOST["key"])
    assert retry.status_code == 200
    assert retry.json() == LOST["response"]            # the stage-1 receipt, verbatim
    assert len(ada.get("/reservations").json()["reservations"]) == 2
    changed = dict(LOST["body"], party_size=3)
    assert error_code(ada.post("/reservations", json=changed, key=LOST["key"])) == \
        (409, "idempotency_key_reuse")


def test_failed_key_is_still_reusable(upgraded):
    ada, _ = upgraded
    body = {"restaurant_id": "r_anker", "table_ids": ["t_3"],
            "starts_at_local": "2030-06-14T21:00", "party_size": 6}
    assert ada.post("/reservations", json=body, key="failed-key").status_code == 201


def test_imported_bookings_still_hold_their_tables(upgraded, http):
    ada, _ = upgraded
    assert error_code(ada.post("/reservations", key="new", json={
        "restaurant_id": "r_anker", "table_ids": ["t_2"],
        "starts_at_local": "2030-06-14T19:30", "party_size": 2})) == (409, "table_unavailable")
    slots = http.get("/availability?restaurant_id=r_anker&date=2030-06-14&party_size=1")
    at_19 = next(s for s in slots.json()["slots"] if s["starts_at_local"].endswith("19:00"))
    assert at_19["available_table_ids"] == []
    assert at_19["available_options"] == []
    assert http.get("/restaurants/r_anker").json()["combinable"] == []


def test_upgraded_state_accepts_cancel_and_re_exports(upgraded, http):
    ada, _ = upgraded
    ref = LOST["response"]["reference"]
    assert ada.post(f"/reservations/{ref}/cancel").json()["status"] == "cancelled"
    again = http.get("/_test/export").json()
    assert http.post("/_test/import", json=again).status_code == 204
    assert ada.get(f"/reservations/{ref}").json()["status"] == "cancelled"
    retry = ada.post("/reservations", json=LOST["body"], key=LOST["key"])
    assert (retry.status_code, retry.json()) == (200, LOST["response"])


def test_login_with_the_stage_1_password_hash(upgraded, http):
    resp = http.post("/auth/login", json={"email": "bea@example.com",
                                          "password": "another horse"})
    assert resp.status_code == 200 and resp.json()["display_name"] == "Bea"
