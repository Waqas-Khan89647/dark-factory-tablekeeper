"""JSON request/response plumbing and the error envelope (§3.4, §5)."""
from __future__ import annotations

import json
import logging
import math

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from .errors import ApiError, malformed, unauthenticated

MEDIA_TYPE = "application/json; charset=utf-8"
log = logging.getLogger("tablekeeper")


def json_response(payload, status: int = 200) -> Response:
    # ASCII escapes keep the body valid UTF-8 even for strings holding lone surrogates.
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return Response(body, status_code=status, media_type=MEDIA_TYPE)


def no_content() -> Response:
    return Response(status_code=204)


def error_response(status: int, code: str, message: str) -> Response:
    return json_response({"error": {"code": code, "message": message}}, status)


# Deeper bodies are refused, so every stored body can be compared and exported safely.
MAX_BODY_DEPTH = 64
# An export wraps stored bodies a few levels down (state -> receipts -> [] -> body).
MAX_IMPORT_DEPTH = MAX_BODY_DEPTH + 8


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


def _finite_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):   # e.g. 1e99999 would re-serialise as Infinity
        raise ValueError(f"{text} is out of range")
    return number


def nesting_depth(value) -> int:
    """How deeply arrays and objects nest in a parsed JSON value, without recursion."""
    deepest, stack = 0, [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        deepest = max(deepest, depth)
        stack.extend((child, depth + 1) for child in children)
    return deepest


async def read_json(request: Request, *, max_depth: int = MAX_BODY_DEPTH):
    """The parsed body, or 400 `malformed_request` if it is not strict RFC 8259 JSON
    (NaN and Infinity are refused) or nests deeper than `max_depth`."""
    raw = await request.body()
    try:
        body = json.loads(raw, parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, RecursionError):
        raise malformed("request body is not valid JSON") from None
    if nesting_depth(body) > max_depth:
        raise malformed(f"request body nests deeper than {max_depth} levels")
    return body


async def read_json_object(request: Request) -> dict:
    body = await read_json(request)
    if not isinstance(body, dict):
        raise malformed("request body must be a JSON object")
    return body


def bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise unauthenticated("a bearer token is required")
    return token


# ---- exception handlers -------------------------------------------------------

async def api_error(request: Request, exc: ApiError) -> Response:
    return error_response(exc.status, exc.code, exc.message)


async def http_error(request: Request, exc: HTTPException) -> Response:
    if exc.status_code == 404:
        return error_response(404, "not_found", "no such route")
    if exc.status_code == 405:
        return error_response(405, "method_not_allowed", "method not allowed on this route")
    return error_response(exc.status_code, "http_error", str(exc.detail))


async def unexpected_error(request: Request, exc: Exception) -> Response:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return error_response(500, "internal_error", "unexpected server error")
