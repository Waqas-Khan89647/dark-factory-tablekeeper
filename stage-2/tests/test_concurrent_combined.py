"""Stage 2 W1: 50 concurrent requests mixing pairs and singles never double-book.

The app runs in a real uvicorn server on a local port, and 50 threads send their
requests at once through a barrier, so the requests really are in flight together.
"""
import random
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from conftest import PASSWORD, fixture, future_date, restaurant

DAY = future_date()
WORKERS = 50
TABLES = [{"id": f"t{i}", "label": str(i), "capacity": 2 + i % 3} for i in range(1, 7)]
PAIRS = [["t1", "t2"], ["t2", "t3"], ["t3", "t4"], ["t4", "t5"], ["t5", "t6"], ["t6", "t1"]]
STARTS = ["19:00", "19:30", "20:00", "20:30"]     # 90-minute bookings, so most overlap
USERS = [f"user{i}" for i in range(5)]


@pytest.fixture
def base_url(live_url):
    return live_url


def setup(base_url) -> list[str]:
    body = fixture(users=USERS, restaurants=[restaurant(tables=TABLES, combinable=PAIRS)])
    with httpx.Client(base_url=base_url, timeout=10) as http:
        assert http.post("/_test/reset", json=body).status_code == 204
        return [http.post("/auth/login", json={"email": f"{u}@test.example",
                                               "password": PASSWORD}).json()["token"]
                for u in USERS]


def fire(base_url, requests) -> list[httpx.Response]:
    """Send every (method, path, token, key, body) at once from its own thread."""
    barrier = threading.Barrier(len(requests))

    def send(item):
        method, path, token, key, body = item
        headers = {"Authorization": f"Bearer {token}"}
        if key:
            headers["Idempotency-Key"] = key
        with httpx.Client(base_url=base_url, timeout=5) as http:
            barrier.wait()
            return http.request(method, path, json=body, headers=headers)

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return list(pool.map(send, requests))


def booking_body(rng) -> dict:
    if rng.random() < 0.5:
        tables = rng.choice(PAIRS)[:]
        rng.shuffle(tables)
    else:
        tables = [rng.choice(TABLES)["id"]]
    shape = {"table_ids": tables} if len(tables) > 1 or rng.random() < 0.5 \
        else {"table_id": tables[0]}
    return {"restaurant_id": "r_main", "starts_at_local": f"{DAY}T{rng.choice(STARTS)}",
            "party_size": 2, **shape}


def overlapping(a: dict, b: dict) -> bool:
    return a["starts_at"] < b["ends_at"] and b["starts_at"] < a["ends_at"]


def assert_no_double_booking(confirmed: list[dict]) -> None:
    for i, a in enumerate(confirmed):
        for b in confirmed[i + 1:]:
            if overlapping(a, b):
                shared = set(a["table_ids"]) & set(b["table_ids"])
                assert not shared, f"{a['reference']} and {b['reference']} share {shared}"


def all_confirmed(base_url, tokens) -> list[dict]:
    out = []
    with httpx.Client(base_url=base_url, timeout=5) as http:
        for token in tokens:
            mine = http.get("/reservations", headers={"Authorization": f"Bearer {token}"})
            out += [r for r in mine.json()["reservations"] if r["status"] == "confirmed"]
    return out


@pytest.mark.parametrize("seed", range(4))
def test_fifty_concurrent_pair_and_single_bookings(base_url, seed):
    rng = random.Random(seed)
    tokens = setup(base_url)
    requests = [("POST", "/reservations", rng.choice(tokens), f"c{seed}-{i}", booking_body(rng))
                for i in range(WORKERS)]
    responses = fire(base_url, requests)

    statuses = sorted({r.status_code for r in responses})
    assert set(statuses) <= {201, 409}, statuses
    assert all(r.json()["error"]["code"] == "table_unavailable"
               for r in responses if r.status_code == 409)
    created = [r.json() for r in responses if r.status_code == 201]
    assert created, "at least one booking must win"
    assert_no_double_booking(created)
    stored = all_confirmed(base_url, tokens)
    assert sorted(r["reference"] for r in stored) == sorted(r["reference"] for r in created)
    assert_no_double_booking(stored)


def test_fifty_concurrent_amendments_and_bookings(base_url):
    """PATCHes into pairs race new bookings and moves; the end state never overlaps."""
    rng = random.Random(99)
    tokens = setup(base_url)
    with httpx.Client(base_url=base_url, timeout=5) as http:
        existing = []
        for i, table in enumerate(["t1", "t3", "t5"]):
            token = tokens[i]
            resp = http.post("/reservations", headers={"Authorization": f"Bearer {token}",
                                                       "Idempotency-Key": f"pre-{i}"},
                             json={"restaurant_id": "r_main", "table_id": table,
                                   "starts_at_local": f"{DAY}T19:00", "party_size": 2})
            assert resp.status_code == 201
            existing.append((token, resp.json()["reference"]))
    requests = []
    for i in range(WORKERS):
        token, ref = rng.choice(existing)
        kind = i % 3
        if kind == 0:
            requests.append(("PATCH", f"/reservations/{ref}", token, None,
                             {"table_ids": rng.choice(PAIRS),
                              "starts_at_local": f"{DAY}T{rng.choice(STARTS)}"}))
        elif kind == 1:
            requests.append(("POST", "/reservation-moves", token, f"mv-{i}",
                             {"moves": [{"reference": ref, "table_ids": rng.choice(PAIRS)}]}))
        else:
            requests.append(("POST", "/reservations", rng.choice(tokens), f"new-{i}",
                             booking_body(rng)))
    responses = fire(base_url, requests)
    assert all(r.status_code < 500 for r in responses)
    assert {r.status_code for r in responses} <= {200, 201, 409}
    assert_no_double_booking(all_confirmed(base_url, tokens))


def test_concurrent_identical_pair_requests_book_once(base_url):
    tokens = setup(base_url)
    body = {"restaurant_id": "r_main", "table_ids": ["t1", "t2"],
            "starts_at_local": f"{DAY}T19:00", "party_size": 4}
    responses = fire(base_url, [("POST", "/reservations", tokens[0], "same-key", body)] * WORKERS)
    assert sorted(r.status_code for r in responses) == [200] * (WORKERS - 1) + [201]
    assert len({r.text for r in responses}) == 1
    assert len(all_confirmed(base_url, tokens)) == 1
