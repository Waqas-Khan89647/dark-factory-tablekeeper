"""The API error type and the codes the spec names (§5)."""
from __future__ import annotations


class ApiError(Exception):
    """An error that maps directly onto the `{"error": {"code", "message"}}` envelope."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def malformed(message: str = "request body is malformed") -> ApiError:
    return ApiError(400, "malformed_request", message)


def invalid(message: str) -> ApiError:
    return ApiError(422, "validation_failed", message)


def not_found(message: str = "not found") -> ApiError:
    return ApiError(404, "not_found", message)


def unauthenticated(message: str = "missing or invalid credentials") -> ApiError:
    return ApiError(401, "unauthenticated", message)
