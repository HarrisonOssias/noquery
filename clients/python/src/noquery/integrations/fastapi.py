"""FastAPI integration: expose the noquery API as a router.

Install with `pip install "noquery[fastapi]"`.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from ..http import handle


def create_router(
    nq: Any,
    get_auth: Callable[..., Any],
    on_error: Optional[Callable[[BaseException], None]] = None,
) -> APIRouter:
    """A router with `GET /catalog` and `POST /ask`, `/run` and `/validate`.

        app.include_router(create_router(nq, get_auth), prefix="/analytics")

    `get_auth` is a FastAPI dependency that returns the auth context for the logged-in user, for
    example `{"userId": ..., "orgId": ..., "roles": [...]}`. Build it from the verified session
    only; the request body never supplies it. Raise `HTTPException` there to refuse a request.

    The endpoints are plain `def` functions, so FastAPI runs them in its thread pool and the
    blocking database and model calls do not stall the event loop.
    """
    router = APIRouter()

    def respond(route: str, body: Any, auth: Mapping[str, Any]) -> JSONResponse:
        status, payload = handle(nq, route, body, auth, on_error=on_error)
        return JSONResponse(payload, status_code=status)

    @router.get("/catalog")
    def catalog(auth: Any = Depends(get_auth)) -> JSONResponse:
        return respond("catalog", {}, auth)

    @router.post("/ask")
    def ask(body: Dict[str, Any] = Body(...), auth: Any = Depends(get_auth)) -> JSONResponse:
        return respond("ask", body, auth)

    @router.post("/run")
    def run(body: Dict[str, Any] = Body(...), auth: Any = Depends(get_auth)) -> JSONResponse:
        return respond("run", body, auth)

    @router.post("/validate")
    def validate(body: Dict[str, Any] = Body(...), auth: Any = Depends(get_auth)) -> JSONResponse:
        return respond("validate", body, auth)

    return router
