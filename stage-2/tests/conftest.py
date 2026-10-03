"""Project tests: the app runs in-process behind Starlette's TestClient.

Concurrency and browser tests instead use `live_url`, a real uvicorn server on a
local port. Browser tests need Playwright with Chromium and skip without it.
"""
from __future__ import annotations

import datetime as dt
import itertools
import json
import pathlib
import socket
import sys
import threading
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest
import uvicorn
from starlette.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402

PASSWORD = "long enough"
_keys = itertools.count()


def new_key() -> str:
    return f"key-{next(_keys)}"


def restaurant(rid="r_main", *, timezone="Europe/Berlin", opens="18:00", closes="23:00",
               days=("mon", "tue", "wed", "thu", "fri", "sat", "sun"), slot=30,
               duration=90, cutoff=120, tables=None, combinable=None) -> dict:
    out = {
        "id": rid, "name": f"Restaurant {rid}", "timezone": timezone,
        "slot_minutes": slot, "reservation_duration_minutes": duration,
        "cancellation_cutoff_minutes": cutoff,
        "opening_hours": [{"weekday": d, "opens": opens, "closes": closes} for d in days],
        "tables": tables if tables is not None else [
            {"id": "a", "label": "A", "capacity": 2},
            {"id": "b", "label": "B", "capacity": 4},
            {"id": "c", "label": "C", "capacity": 6},
        ],
    }
    if combinable is not None:
        out["combinable"] = combinable
    return out


def user(uid: str) -> dict:
    return {"id": uid, "email": f"{uid}@test.example", "password": PASSWORD,
            "display_name": uid.title()}


def fixture(*, users=("alice", "bruno"), restaurants=None, reservations=()) -> dict:
    return {"users": [user(u) for u in users],
            "restaurants": restaurants if restaurants is not None else [restaurant()],
            "reservations": list(reservations)}


def future_date(days: int = 10, tz: str = "Europe/Berlin") -> str:
    return (dt.datetime.now(ZoneInfo(tz)).date() + dt.timedelta(days=days)).isoformat()


class Client:
    """A TestClient that carries a bearer token."""

    def __init__(self, http: TestClient, token: str | None = None):
        self.http, self.token = http, token

    def request(self, method, path, *, key=None, token=..., **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        token = self.token if token is ... else token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if key is not None:
            headers["Idempotency-Key"] = key
        if "json" in kwargs:   # ASCII-escaped, so odd strings such as lone surrogates travel
            kwargs["content"] = json.dumps(kwargs.pop("json")).encode()
            headers.setdefault("Content-Type", "application/json")
        return self.http.request(method, path, headers=headers, **kwargs)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def patch(self, path, **kw):
        return self.request("PATCH", path, **kw)

    def book(self, *, rid="r_main", table="b", at=None, party=4, key=None, **extra):
        body = {"restaurant_id": rid, "table_id": table,
                "starts_at_local": at or f"{future_date()}T19:00", "party_size": party}
        body.update(extra)
        return self.post("/reservations", json=body, key=key or new_key())


def error_code(resp) -> tuple[int, str]:
    return resp.status_code, resp.json()["error"]["code"]


@pytest.fixture
def http():
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def reset(http):
    def _reset(body=None, **kwargs):
        resp = http.post("/_test/reset", json=fixture(**kwargs) if body is None else body)
        assert resp.status_code == 204, resp.text
        return resp
    return _reset


@pytest.fixture
def login(http):
    def _login(uid: str) -> Client:
        resp = http.post("/auth/login", json={"email": f"{uid}@test.example",
                                              "password": PASSWORD})
        assert resp.status_code == 200, resp.text
        return Client(http, resp.json()["token"])
    return _login


@pytest.fixture
def world(reset, login):
    reset()
    return login("alice"), login("bruno")


# ---- a real server, and a browser pointed at it ---------------------------------

@pytest.fixture(scope="session")
def live_url():
    """The app behind uvicorn on a free local port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def live(live_url):
    """An HTTP client for seeding and inspecting the live server from a test."""
    with httpx.Client(base_url=live_url, timeout=10) as client:
        yield client


@pytest.fixture(scope="session")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as driver:
        try:
            instance = driver.chromium.launch()
        except sync_api.Error as exc:
            pytest.skip(f"Chromium is not installed for Playwright: {exc}")
        yield instance
        instance.close()


@pytest.fixture
def new_page(browser, live_url):
    """Open pages on the live server. At teardown every request any page made must
    have stayed on that server (nothing may load from another host), and no script
    may have thrown."""
    contexts, requested, script_errors = [], [], []

    def _open(*, width: int = 1280, height: int = 900):
        context = browser.new_context(base_url=live_url,
                                      viewport={"width": width, "height": height})
        context.set_default_timeout(10_000)
        contexts.append(context)
        page = context.new_page()
        page.on("request", lambda request: requested.append(request.url))
        page.on("pageerror", lambda error: script_errors.append(str(error)))
        return page

    yield _open
    for context in contexts:
        context.close()
    origin = urlsplit(live_url).netloc
    external = sorted({url for url in requested
                       if urlsplit(url).scheme in ("http", "https", "ws", "wss")
                       and urlsplit(url).netloc != origin})
    assert external == [], f"the page reached outside the container: {external}"
    assert script_errors == [], script_errors


@pytest.fixture
def page(new_page):
    return new_page()


def tid(name: str) -> str:
    """The selector for an element's exact `data-testid`."""
    return f"[data-testid='{name}']"
