"""HTTP client for a noquery server (the `noquery-server` sidecar)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Mapping, Optional, Sequence

from .errors import (
    AuthenticationError,
    NoqueryError,
    PlannerFailedError,
    PlannerRefusalError,
    PlannerUnavailableError,
    QueryValidationError,
)
from .results import Column, Result, Validation

__all__ = [
    "AuthenticationError",
    "Column",
    "NoqueryClient",
    "NoqueryError",
    "PlannerFailedError",
    "PlannerUnavailableError",
    "QueryValidationError",
    "Result",
    "Validation",
]

Auth = Mapping[str, Any]
"""Who is asking, e.g. {"userId": "u1", "orgId": "acme", "roles": ["analyst"]}.

Row scopes in the semantic model read their values from here by key, so pass the
values your own app has already verified for the logged-in user.
"""


class NoqueryClient:
    """Talks to a noquery server.

    Example:
        client = NoqueryClient("http://127.0.0.1:8417", token=os.environ["NOQUERY_SERVICE_TOKEN"])
        result = client.ask("How many trips last week?", auth={"orgId": org_id, "roles": ["analyst"]})
    """

    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def health(self) -> Dict[str, Any]:
        """Returns {"ok": True, "planner": bool}."""
        return self._request("GET", "/healthz")

    def catalog(self, auth: Auth) -> Dict[str, Any]:
        """The entities, measures and dimensions this caller may use."""
        return self._request("POST", "/v1/catalog", {"auth": dict(auth)})

    def ask(
        self,
        question: str,
        auth: Auth,
        timezone: Optional[str] = None,
        history: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Result:
        """Answer a plain-English question. Needs a server started with a planner."""
        body: Dict[str, Any] = {"auth": dict(auth), "question": question}
        if timezone:
            body["timezone"] = timezone
        if history:
            body["history"] = [dict(turn) for turn in history]
        return Result.from_dict(self._request("POST", "/v1/ask", body))

    def run(self, ir: Mapping[str, Any], auth: Auth) -> Result:
        """Run a query object directly, without the planner.

        Raises QueryValidationError if the query is invalid for this caller.
        """
        data = self._request("POST", "/v1/run", {"auth": dict(auth), "ir": dict(ir)})
        return Result.from_dict({"type": "answer", **data})

    def validate(self, ir: Mapping[str, Any], auth: Auth) -> Validation:
        """Check a query object without running it."""
        data = self._request("POST", "/v1/validate", {"auth": dict(auth), "ir": dict(ir)})
        return Validation(ok=bool(data.get("ok")), errors=list(data.get("errors", [])))

    def _request(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise _error_from_response(error) from None


def _error_from_response(error: urllib.error.HTTPError) -> NoqueryError:
    try:
        body = json.loads(error.read().decode("utf-8"))
    except ValueError:
        body = None
    payload = body.get("error") if isinstance(body, dict) else None
    if not isinstance(payload, dict):
        # Not a noquery error body; probably another service on this address.
        payload = {"message": f"Unexpected response from {error.url} (HTTP {error.code}). Is the noquery sidecar running at this address?"}
    code = payload.get("code", "")
    message = payload.get("message", error.reason or "Request failed")
    errors = payload.get("errors")
    if code == "unauthorized":
        return AuthenticationError(error.code, code, message, errors)
    if code == "invalid_query":
        return QueryValidationError(error.code, code, message, errors)
    if code == "planner_not_configured":
        return PlannerUnavailableError(error.code, code, message, errors)
    if code == "planner_refused":
        refusal = PlannerRefusalError()
        refusal.status, refusal.message, refusal.args = error.code, message, (f"{code}: {message}",)
        return refusal
    if code == "planner_failed":
        return PlannerFailedError(error.code, code, message, payload.get("reason", ""), bool(payload.get("retryable")))
    return NoqueryError(error.code, code, message, errors)
