import pytest

pytest.importorskip("fastapi", reason="needs the fastapi extra")
pytest.importorskip("httpx", reason="the FastAPI test client needs httpx")

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from noquery import Noquery
from noquery.integrations.fastapi import create_router

from conftest import ORG_A
from support import FakeDialect, FakeExecutor, ScriptedPlanner, query_plan


def get_auth(x_test_org: str = Header(default="")):
    if not x_test_org:
        raise HTTPException(status_code=401, detail="Not signed in")
    return {"userId": "u1", "orgId": x_test_org, "roles": ["analyst"]}


def make_client(fleet, rows=None, planner=None, error=None, errors=None):
    executor = FakeExecutor(rows=rows or [], error=error)
    dialect = FakeDialect()
    nq = Noquery(model=fleet, dialect=dialect, executor=executor, planner=planner)
    app = FastAPI()
    app.include_router(create_router(nq, get_auth, on_error=errors.append if errors is not None else None), prefix="/analytics")
    return TestClient(app), dialect, executor


HEADERS = {"X-Test-Org": ORG_A}


def test_requests_without_a_session_are_refused_by_the_auth_dependency(fleet):
    client, _, executor = make_client(fleet)
    assert client.get("/analytics/catalog").status_code == 401
    assert client.post("/analytics/run", json={"ir": {"version": 1, "measures": ["trips.count"]}}).status_code == 401
    assert executor.calls == []


def test_catalog(fleet):
    client, _, _ = make_client(fleet)
    response = client.get("/analytics/catalog", headers=HEADERS)
    assert response.status_code == 200
    assert [e["name"] for e in response.json()["entities"]] == ["organizations", "devices", "trips"]


def test_run_scopes_to_the_session_and_ignores_auth_in_the_body(fleet):
    client, dialect, _ = make_client(fleet, rows=[{"trips.count": 3}])
    body = {"ir": {"version": 1, "measures": ["trips.count"]}, "auth": {"orgId": "someone-else", "roles": ["clinical"]}}
    response = client.post("/analytics/run", json=body, headers=HEADERS)
    assert response.status_code == 200
    payload = response.json()
    assert payload["rows"] == [{"trips.count": 3}] and payload["explanation"] == "Trips." and payload["chart"]["type"] == "kpi"
    assert dialect.compiled[0]["where"] == [{"kind": "scope", "alias": "t0", "column": "org_id", "value": ORG_A}]


def test_validate_and_invalid_queries(fleet):
    client, _, _ = make_client(fleet)
    ok = client.post("/analytics/validate", json={"ir": {"version": 1, "measures": ["trips.count"]}}, headers=HEADERS)
    assert ok.json() == {"ok": True}
    bad = client.post("/analytics/run", json={"ir": {"version": 1, "measures": ["patients.count"]}}, headers=HEADERS)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_query"
    assert bad.json()["error"]["errors"][0]["code"] == "unknown_entity"
    missing = client.post("/analytics/run", json={}, headers=HEADERS)
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "bad_request"


def test_ask(fleet):
    planner = ScriptedPlanner(query_plan(measures=["trips.count"], dimensions=["devices.model"]))
    client, _, _ = make_client(fleet, rows=[{"devices.model": "X1", "trips.count": 2}], planner=planner)
    response = client.post("/analytics/ask", json={"question": "Trips by model?", "timezone": "America/Toronto"}, headers=HEADERS)
    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "answer" and payload["rows"] == [{"devices.model": "X1", "trips.count": 2}]
    assert planner.calls[0]["timezone"] == "America/Toronto"


def test_ask_without_a_planner(fleet):
    client, _, _ = make_client(fleet)
    response = client.post("/analytics/ask", json={"question": "Trips?"}, headers=HEADERS)
    assert response.status_code == 501 and response.json()["error"]["code"] == "planner_not_configured"


def test_internal_errors_do_not_leak(fleet):
    errors = []
    client, _, _ = make_client(fleet, error=RuntimeError("secret detail"), errors=errors)
    response = client.post("/analytics/run", json={"ir": {"version": 1, "measures": ["trips.count"]}}, headers=HEADERS)
    assert response.status_code == 500 and response.json() == {"error": {"code": "internal", "message": "Internal error."}}
    assert "secret detail" not in response.text and str(errors[0]) == "secret detail"


def test_endpoints_run_in_the_thread_pool(fleet):
    """Plain `def` endpoints keep blocking database and model calls off the event loop."""
    import inspect

    nq = Noquery(model=fleet, dialect=FakeDialect(), executor=FakeExecutor())
    router = create_router(nq, get_auth)
    assert sorted((r.path, sorted(r.methods)) for r in router.routes) == [
        ("/ask", ["POST"]),
        ("/catalog", ["GET"]),
        ("/run", ["POST"]),
        ("/validate", ["POST"]),
    ]
    assert not any(inspect.iscoroutinefunction(r.endpoint) for r in router.routes)


@pytest.mark.parametrize("body", ["[1, 2]", '"text"', "not json"])
def test_bodies_that_are_not_json_objects_are_rejected(fleet, body):
    client, _, executor = make_client(fleet)
    response = client.post("/analytics/run", content=body, headers={**HEADERS, "Content-Type": "application/json"})
    assert response.status_code == 422
    assert executor.calls == []
