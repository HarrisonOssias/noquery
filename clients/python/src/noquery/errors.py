"""Errors raised by the embedded engine and by the HTTP client. Both use the same classes."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class NoqueryError(Exception):
    """Base class. `status` and `code` match the HTTP API, whether or not HTTP was involved."""

    def __init__(self, status: int, code: str, message: str, errors: Optional[List[Dict[str, Any]]] = None):
        super().__init__(f"{code}: {message}" if code else message)
        self.status = status
        self.code = code
        self.message = message
        self.errors = errors or []


class AuthenticationError(NoqueryError):
    """The service token was missing or wrong (HTTP 401)."""


class QueryValidationError(NoqueryError):
    """The query named unknown fields, broke a policy, or was otherwise invalid (HTTP 422)."""

    @classmethod
    def from_errors(cls, errors: List[Dict[str, Any]]) -> "QueryValidationError":
        return cls(422, "invalid_query", " ".join(str(e.get("message", "")) for e in errors), errors)


class PlannerUnavailableError(NoqueryError):
    """No planner is configured, so `ask` is unavailable (HTTP 501). Use `run`."""


class PlannerFailedError(NoqueryError):
    """The model call behind `ask` failed (HTTP 502 or 503), e.g. bad API key or rate limit.

    `reason` is a short tag such as "authentication" or "rate_limited"; `retryable` says whether
    trying again later may succeed. The message is shown to callers, so it never contains secrets.
    """

    def __init__(self, status: int, code: str, message: str, reason: str = "", retryable: bool = False):
        super().__init__(status, code, message)
        self.reason = reason
        self.retryable = retryable

    @classmethod
    def from_reason(cls, reason: str, message: str, retryable: bool = False) -> "PlannerFailedError":
        return cls(503 if retryable else 502, "planner_failed", message, reason, retryable)


class PlannerRefusalError(NoqueryError):
    """The model declined to plan the question (HTTP 422, code "planner_refused")."""

    def __init__(self, category: Optional[str] = None):
        suffix = f" ({category})" if category else ""
        super().__init__(422, "planner_refused", f"The model declined to plan this question{suffix}.")
        self.category = category
