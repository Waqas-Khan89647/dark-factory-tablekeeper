"""Endpoint handlers. Each one parses and checks its input, then applies the rules in
`Service` while holding the state lock for anything that reads-then-writes."""
from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.routing import Route

from . import loader, passwords, validation
from .errors import ApiError, invalid, unauthenticated
from .http import (MAX_IMPORT_DEPTH, bearer_token, json_response, no_content, read_json,
                   read_json_object)
from .model import User, email_key
from .service import Service

service = Service()
MAX_MOVES = 8


def _user(request: Request) -> str:
    return service.user_for_token(bearer_token(request))


def _auth_payload(user: User, token: str) -> dict:
    return {"user_id": user.id, "display_name": user.display_name, "token": token}


# ---- health and test control ------------------------------------------------------

async def health(request: Request):
    return json_response({"status": "ok"})


async def reset(request: Request):
    users, fixture = loader.read_fixture(await read_json(request))
    hashes = await asyncio.gather(*(passwords.hash_async(pw) for *_, pw in users))
    state = loader.build_fixture(
        fixture, [(uid, email, name, h) for (uid, email, name, _), h in zip(users, hashes)])
    async with service.lock:
        service.state = state
    return no_content()


async def export(request: Request):
    async with service.lock:
        return json_response(loader.export_state(service.state))


async def import_(request: Request):
    state = loader.import_state(await read_json(request, max_depth=MAX_IMPORT_DEPTH))
    async with service.lock:
        service.state = state
    return no_content()


# ---- auth ---------------------------------------------------------------------------

async def signup(request: Request):
    body = await read_json_object(request)
    email = validation.required_string(body, "email")
    password = validation.required_string(body, "password")
    display_name = validation.required_string(body, "display_name")
    if not validation.EMAIL.match(email):
        raise invalid("email must be of the form local@domain")
    if len(password) < 8:
        raise invalid("password must be at least 8 characters")
    if email_key(email) in service.state.user_by_email:
        raise ApiError(409, "email_taken", "that email is already registered")
    password_hash = await passwords.hash_async(password)
    async with service.lock:
        state = service.state
        if email_key(email) in state.user_by_email:
            raise ApiError(409, "email_taken", "that email is already registered")
        user = User(service.new_user_id(), email, display_name, password_hash)
        state.users[user.id] = user
        state.user_by_email[email_key(email)] = user.id
        token = service.issue_token(user.id)
    return json_response(_auth_payload(user, token), 201)


async def login(request: Request):
    body = await read_json_object(request)
    email = validation.required_string(body, "email")
    password = validation.required_string(body, "password")
    state = service.state
    user = state.users.get(state.user_by_email.get(email_key(email), ""))
    if user is None or not await passwords.verify_async(password, user.password_hash):
        raise unauthenticated("wrong email or password")
    async with service.lock:
        if service.state is not state:   # a reset or import replaced the accounts meanwhile
            raise unauthenticated("wrong email or password")
        token = service.issue_token(user.id)
    return json_response(_auth_payload(user, token))


# ---- restaurants and availability ----------------------------------------------

async def list_restaurants(request: Request):
    return json_response({"restaurants": [
        r.summary_json() for r in service.state.restaurants.values()]})


async def get_restaurant(request: Request):
    return json_response(service.restaurant(request.path_params["restaurant_id"]).to_json())


async def availability(request: Request):
    params = request.query_params
    restaurant_id = params.get("restaurant_id")
    if not restaurant_id:
        raise invalid("restaurant_id is required")
    day = validation.query_date(params.get("date"))
    party = validation.query_positive_int(params.get("party_size"), "party_size")
    restaurant = service.restaurant(restaurant_id)
    return json_response({
        "restaurant_id": restaurant.id,
        "date": day.isoformat(),
        "timezone": restaurant.timezone,
        "slots": service.slots(restaurant, day, party),
    })


# ---- reservations ---------------------------------------------------------------

async def _idempotent_request(request: Request) -> tuple[str, dict, str]:
    """Auth, body and key, in the order the spec resolves them (§7)."""
    user_id = _user(request)
    body = await read_json_object(request)
    key = validation.idempotency_key(request.headers.get("idempotency-key"))
    return user_id, body, key


async def create_reservation(request: Request):
    user_id, body, key = await _idempotent_request(request)

    def action() -> dict:
        restaurant_id = validation.required_string(body, "restaurant_id")
        table_id = validation.required_string(body, "table_id")
        local_value = body.get("starts_at_local")
        validation.local_datetime(local_value)
        party = validation.party_size(body.get("party_size"))
        restaurant = service.restaurant(restaurant_id)
        placement = service.place(restaurant, table_id, local_value, party)
        return service.as_json(service.create(user_id, restaurant, placement))

    async with service.lock:
        status, payload = service.run_idempotent(
            user_id, "POST", "/reservations", key, body, action)
    return json_response(payload, status)


async def list_reservations(request: Request):
    user_id = _user(request)
    return json_response({"reservations": [
        service.as_json(b) for b in service.list_for(user_id)]})


async def get_reservation(request: Request):
    user_id = _user(request)
    return json_response(service.as_json(
        service.own(user_id, request.path_params["reference"])))


async def cancel_reservation(request: Request):
    user_id = _user(request)
    async with service.lock:
        booking = service.own(user_id, request.path_params["reference"])
        return json_response(service.as_json(service.cancel(booking)))


async def amend_reservation(request: Request):
    user_id = _user(request)
    changes = await read_json_object(request)
    async with service.lock:
        booking = service.own(user_id, request.path_params["reference"])
        return json_response(service.as_json(service.amend(booking, changes)))


def _moves(body: dict) -> list[dict]:
    moves = body.get("moves")
    if not isinstance(moves, list) or not 1 <= len(moves) <= MAX_MOVES:
        raise invalid(f"moves must be a list of 1 to {MAX_MOVES} objects")
    references = []
    for item in moves:
        if not isinstance(item, dict) or not isinstance(item.get("reference"), str):
            raise invalid("every move must be an object with a string reference")
        references.append(item["reference"])
    if len(set(references)) != len(references):
        raise invalid("move references must be distinct")
    return moves


async def create_moves(request: Request):
    user_id, body, key = await _idempotent_request(request)

    def action() -> dict:
        moved = service.move(user_id, _moves(body))
        return {"reservations": [service.as_json(b) for b in moved]}

    async with service.lock:
        status, payload = service.run_idempotent(
            user_id, "POST", "/reservation-moves", key, body, action)
    return json_response(payload, status)


routes = [
    Route("/health", health, methods=["GET"]),
    Route("/_test/reset", reset, methods=["POST"]),
    Route("/_test/export", export, methods=["GET"]),
    Route("/_test/import", import_, methods=["POST"]),
    Route("/auth/signup", signup, methods=["POST"]),
    Route("/auth/login", login, methods=["POST"]),
    Route("/restaurants", list_restaurants, methods=["GET"]),
    Route("/restaurants/{restaurant_id}", get_restaurant, methods=["GET"]),
    Route("/availability", availability, methods=["GET"]),
    Route("/reservations", create_reservation, methods=["POST"]),
    Route("/reservations", list_reservations, methods=["GET"]),
    Route("/reservations/{reference}", get_reservation, methods=["GET"]),
    Route("/reservations/{reference}", amend_reservation, methods=["PATCH"]),
    Route("/reservations/{reference}/cancel", cancel_reservation, methods=["POST"]),
    Route("/reservation-moves", create_moves, methods=["POST"]),
]
