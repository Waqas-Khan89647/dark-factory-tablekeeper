"""Shared data and page actions for the browser tests."""
from __future__ import annotations

from conftest import future_date, tid

DAY = future_date(7)
PASSWORD = "correct horse"
ADA = {"id": "u_ada", "email": "ada@example.com", "password": PASSWORD, "display_name": "Ada"}
BOB = {"id": "u_bob", "email": "bob@example.com", "password": PASSWORD, "display_name": "Bob"}
WEEK = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SLOTS = ["18:00", "18:30", "19:00", "19:30", "20:00", "20:30", "21:00", "21:30"]


def anker(**overrides) -> dict:
    """Zum Anker: tables 1 (2 seats), 2 (4) and 3 (6); 1+2 and 2+3 may be joined."""
    base = {
        "id": "r_anker", "name": "Zum Anker", "timezone": "Europe/Berlin",
        "slot_minutes": 30, "reservation_duration_minutes": 90,
        "cancellation_cutoff_minutes": 120,
        "opening_hours": [{"weekday": d, "opens": "18:00", "closes": "23:00"} for d in WEEK],
        "tables": [{"id": "t_1", "label": "1", "capacity": 2},
                   {"id": "t_2", "label": "2", "capacity": 4},
                   {"id": "t_3", "label": "3", "capacity": 6}],
        "combinable": [["t_1", "t_2"], ["t_2", "t_3"]],
    }
    return {**base, **overrides}


def luna() -> dict:
    """A second restaurant whose table ids match Zum Anker's but whose labels differ."""
    return anker(id="r_luna", name="Trattoria Luna", combinable=[],
                 tables=[{"id": "t_1", "label": "Window", "capacity": 4},
                         {"id": "t_2", "label": "Terrace", "capacity": 4}])


def world(*, restaurants=None, reservations=()) -> dict:
    return {"users": [ADA, BOB],
            "restaurants": [anker()] if restaurants is None else restaurants,
            "reservations": list(reservations)}


def seed(live, fixture=None) -> None:
    resp = live.post("/_test/reset", json=world() if fixture is None else fixture)
    assert resp.status_code == 204, resp.text


def api_login(live, user=ADA) -> dict:
    """Authorization headers for `user`, obtained over the API."""
    resp = live.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def api_book(live, headers, *, key, at="19:00", party=4, **tables) -> dict:
    body = {"restaurant_id": "r_anker", "starts_at_local": f"{DAY}T{at}", "party_size": party,
            **(tables or {"table_id": "t_2"})}
    resp = live.post("/reservations", json=body, headers={**headers, "Idempotency-Key": key})
    assert resp.status_code == 201, resp.text
    return resp.json()


def confirmed(live, user=ADA) -> list[dict]:
    mine = live.get("/reservations", headers=api_login(live, user)).json()["reservations"]
    return [r for r in mine if r["status"] == "confirmed"]


def log_in(page, user=ADA) -> None:
    page.goto("/login")
    page.fill(tid("login-email"), user["email"])
    page.fill(tid("login-password"), user["password"])
    page.click(tid("login-submit"))
    page.wait_for_selector(tid("current-user"))


def search(page, *, party=4, restaurant="r_anker", date=DAY, goto=True) -> None:
    if goto:
        page.goto("/")
    page.select_option(tid("restaurant-select"), restaurant)
    page.fill(tid("date-input"), date)
    page.fill(tid("party-size-input"), str(party))
    page.click(tid("search-button"))
    page.wait_for_selector(f"{tid('availability-grid')}, {tid('no-slots')}")


def open_form(page, cell="slot-t_2-19:00", *, party=4) -> None:
    search(page, party=party)
    page.click(tid(cell))
    page.wait_for_selector(tid("booking-form"))


def text(page, name: str) -> str:
    return page.text_content(tid(name)).strip()


def present(page, name: str) -> bool:
    return page.query_selector(tid(name)) is not None


def no_horizontal_scroll(page) -> bool:
    return page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth")
