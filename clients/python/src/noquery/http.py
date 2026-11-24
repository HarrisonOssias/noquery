"""Framework-neutral request handling shared by the FastAPI and Django integrations.

The payloads match the noquery HTTP API (see the README), so a front end written against one
works against the other.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import logging
import uuid
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .errors import NoqueryError, PlannerFailedError, QueryValidationError

logger = logging.getLogger("noquery")

ROUTES = ("catalog", "validate", "run", "ask")

Response = Tuple[int, Dict[str, Any]]


def error_payload(status: int, code: str, message: str, **extra: Any) -> Response:
    return status, {"error": {"code": code, "message": message, **extra}}


def handle(
    nq: Any,
    route: str,
    body: Any,
    auth: Mapping[str, Any],
    on_error: Optional[Callable[[BaseException], None]] = None,
) -> Response:
    """Runs one API call. Returns (HTTP status, JSON-ready payload); never raises.

    `auth` must come from your own verified session, never from the request body.
    """
    try:
        if body is None:
            body = {}
        if not isinstance(body, Mapping):
            return error_payload(400, "bad_request", "Request body must be a JSON object.")
        if route == "catalog":
            return 200, nq.catalog(auth)
        if route in ("validate", "run"):
            ir = body.get("ir")
            if not isinstance(ir, Mapping):
                return error_payload(400, "bad_request", "`ir` must be a query object with version: 1.")
            if route == "validate":
                validation = nq.validate(ir, auth)
                return 200, ({"ok": True} if validation.ok else {"ok": False, "errors": validation.errors})
            payload = nq.run(ir, auth).to_dict()
            payload.pop("type", None)
            return 200, to_jsonable(payload)
        if route == "ask":
            if not nq.has_planner:
                return error_payload(501, "planner_not_configured", "No planner is configured, so ask is unavailable. Use run.")
            question = body.get("question")
            if not isinstance(question, str) or question.strip() == "":
                return error_payload(400, "bad_request", "`question` must be a non-empty string.")
            timezone = body.get("timezone") if isinstance(body.get("timezone"), str) else None
            history = body.get("history") if isinstance(body.get("history"), list) else None
            return 200, to_jsonable(nq.ask(question, auth, timezone=timezone, history=history).to_dict())
        return error_payload(404, "not_found", f"No route {route}.")
    except QueryValidationError as error:
        return error_payload(422, "invalid_query", error.message, errors=error.errors)
    except PlannerFailedError as error:
        _report(on_error, error)
        return error_payload(error.status, "planner_failed", error.message, reason=error.reason, retryable=error.retryable)
    except NoqueryError as error:
        return error_payload(error.status, error.code, error.message)
    except Exception as error:  # details are logged, never returned
        _report(on_error, error)
        return error_payload(500, "internal", "Internal error.")


def _report(on_error: Optional[Callable[[BaseException], None]], error: BaseException) -> None:
    if on_error:
        on_error(error)
    else:
        logger.error("noquery request failed", exc_info=error)


def to_jsonable(value: Any) -> Any:
    """Converts database values (dates, decimals, UUIDs) into JSON-friendly ones."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)
