import json

import pytest

pytest.importorskip("django", reason="needs the django extra")
pytest.importorskip("psycopg", reason="the Django PostgreSQL backend needs psycopg")

from django.test import Client

import django_urls
from noquery import CompiledQuery, Noquery, draft_semantics
from noquery.integrations.django import DjangoExecutor, django_executor, introspect_django

from conftest import ORG_A
from support import FakeDialect, FakeExecutor, ScriptedPlanner, query_plan


def table(graph, name):
    return next(t for t in graph["tables"] if t["name"] == name)


def columns(graph, name):
    return {c["name"]: c for c in table(graph, name)["columns"]}


# --- introspection -----------------------------------------------------------------------------


def test_reads_app_models_and_skips_djangos_own_tables_by_default():
    graph = introspect_django()
    assert sorted(t["name"] for t in graph["tables"]) == ["devices", "organizations", "patients", "tags", "trips", "trips_tags"]


def test_djangos_own_apps_are_read_only_when_named():
    names = [t["name"] for t in introspect_django(app_labels=["auth"])["tables"]]
    assert "auth_user" in names and "organizations" not in names
    assert introspect_django(app_labels=["nope"])["tables"] == []


def test_include_and_exclude_use_table_names():
    assert [t["name"] for t in introspect_django(include=["devices"])["tables"]] == ["devices"]
    assert "devices" not in [t["name"] for t in introspect_django(exclude=["devices"])["tables"]]


def test_maps_field_types():
    graph = introspect_django()
    trips = columns(graph, "trips")
    assert {name: c["kind"] for name, c in trips.items()} == {
        "id": "integer",
        "org_id": "uuid",
        "device_id": "uuid",
        "started_at": "timestamp",
        "ended_at": "timestamp",
        "distance_m": "number",
        "is_assisted": "boolean",
        "note": "string",
        "service_date": "date",
        "cost": "number",
        "payload": "json",
        "duration": "unknown",
    }
    assert trips["id"] == {"name": "id", "kind": "integer", "dbType": "bigint", "nullable": False, "primaryKey": True}
    assert trips["started_at"]["withTimezone"] is True and trips["started_at"]["dbType"] == "timestamp with time zone"
    assert trips["distance_m"] == {"name": "distance_m", "kind": "number", "dbType": "double precision", "nullable": True, "comment": "Metres travelled"}
    assert trips["cost"]["dbType"] == "numeric(10, 2)"
    assert "withTimezone" not in trips["service_date"]

    devices = columns(graph, "devices")
    assert devices["status"]["kind"] == "enum" and devices["status"]["enumValues"] == ["active", "inactive", "retired"]
    assert devices["id"]["kind"] == "uuid" and devices["id"]["primaryKey"] is True
    patients = columns(graph, "patients")
    assert patients["email"]["kind"] == "string" and patients["id"]["kind"] == "integer"
    assert columns(graph, "tags")["label"]["kind"] == "string"


def test_reads_foreign_keys_one_to_one_and_many_to_many_tables():
    graph = introspect_django()
    assert table(graph, "trips")["foreignKeys"] == [
        {"columns": ["org_id"], "referencedTable": "organizations", "referencedColumns": ["id"]},
        {"columns": ["device_id"], "referencedTable": "devices", "referencedColumns": ["id"]},
    ]
    assert table(graph, "patients")["foreignKeys"] == [{"columns": ["organization_id"], "referencedTable": "organizations", "referencedColumns": ["id"]}]
    through = table(graph, "trips_tags")
    assert sorted(fk["referencedTable"] for fk in through["foreignKeys"]) == ["tags", "trips"]


def test_proxy_models_do_not_duplicate_tables():
    names = [t["name"] for t in introspect_django()["tables"]]
    assert names.count("patients") == 1


def test_the_graph_drafts_a_sound_model():
    model = draft_semantics(introspect_django())
    assert model["entities"]["patients"]["dimensions"]["email"]["policy"] == "hidden"
    assert model["entities"]["trips"]["defaultTime"] == "started_at"
    assert model["entities"]["trips"]["dimensions"]["service_date"]["timeStorage"] == "local"
    assert model["entities"]["trips"]["dimensions"]["distance_m"]["description"] == "Metres travelled"
    assert "duration" not in model["entities"]["trips"]["dimensions"]
    assert {"from": "trips", "to": "devices", "on": [["device_id", "id"]], "relationship": "many_to_one"} in model["joins"]
    assert {"from": "devices", "to": "organizations", "on": [["org_id", "id"]], "relationship": "many_to_one"} in model["joins"]


# --- executor (the real-database behavior is covered in test_postgres_integration.py) ----------


def test_executor_defaults_and_paramstyle_check():
    assert isinstance(django_executor(), DjangoExecutor) and django_executor().using == "default"
    assert django_executor("analytics").using == "analytics"
    with pytest.raises(ValueError, match="pyformat"):
        django_executor().execute(CompiledQuery(sql="SELECT $1", params=[1], paramstyle="numeric"), 1000, 10)


# --- URL patterns ------------------------------------------------------------------------------


@pytest.fixture()
def api(fleet):
    def configure(rows=None, planner=None, error=None):
        dialect = FakeDialect()
        executor = FakeExecutor(rows=rows or [], error=error)
        django_urls.state["nq"] = Noquery(model=fleet, dialect=dialect, executor=executor, planner=planner)
        del django_urls.state["errors"][:]
        return Client(headers={"X-Test-Org": ORG_A}), dialect, executor

    yield configure
    django_urls.state["nq"] = None


def post(client, route, body, **extra):
    return client.post(f"/analytics/{route}", data=json.dumps(body), content_type="application/json", **extra)


def test_requests_refused_by_get_auth_never_reach_the_engine(api):
    _, _, executor = api()
    anonymous = Client()
    assert anonymous.get("/analytics/catalog").status_code == 403
    assert post(anonymous, "run", {"ir": {"version": 1, "measures": ["trips.count"]}}).status_code == 403
    assert executor.calls == []


def test_catalog(api):
    client, _, _ = api()
    response = client.get("/analytics/catalog")
    assert response.status_code == 200
    assert [e["name"] for e in response.json()["entities"]] == ["organizations", "devices", "trips"]
    assert client.post("/analytics/catalog").status_code == 405


def test_run_scopes_to_the_session_and_ignores_auth_in_the_body(api):
    client, dialect, _ = api(rows=[{"trips.count": 3}])
    response = post(client, "run", {"ir": {"version": 1, "measures": ["trips.count"]}, "auth": {"orgId": "someone-else"}})
    assert response.status_code == 200
    assert response.json()["rows"] == [{"trips.count": 3}]
    assert dialect.compiled[0]["where"] == [{"kind": "scope", "alias": "t0", "column": "org_id", "value": ORG_A}]
    assert client.get("/analytics/run").status_code == 405


def test_validate_invalid_and_malformed_requests(api):
    client, _, _ = api()
    assert post(client, "validate", {"ir": {"version": 1, "measures": ["trips.count"]}}).json() == {"ok": True}
    bad = post(client, "run", {"ir": {"version": 1, "measures": ["patients.count"]}})
    assert bad.status_code == 422 and bad.json()["error"]["errors"][0]["code"] == "unknown_entity"
    malformed = client.post("/analytics/run", data="not json", content_type="application/json")
    assert malformed.status_code == 400 and malformed.json()["error"]["code"] == "bad_request"
    assert post(client, "run", [1, 2]).status_code == 400


def test_ask(api):
    planner = ScriptedPlanner(query_plan(measures=["trips.count"]))
    client, _, _ = api(rows=[{"trips.count": 9}], planner=planner)
    response = post(client, "ask", {"question": "How many trips?", "timezone": "America/Toronto"})
    assert response.status_code == 200 and response.json()["type"] == "answer" and response.json()["rows"] == [{"trips.count": 9}]
    assert planner.calls[0]["timezone"] == "America/Toronto"
    client, _, _ = api()
    assert post(client, "ask", {"question": "How many trips?"}).status_code == 501


def test_internal_errors_do_not_leak(api):
    client, _, _ = api(error=RuntimeError("secret detail"))
    response = post(client, "run", {"ir": {"version": 1, "measures": ["trips.count"]}})
    assert response.status_code == 500 and response.json() == {"error": {"code": "internal", "message": "Internal error."}}
    assert str(django_urls.state["errors"][0]) == "secret detail"


def test_csrf_protection_is_kept(api):
    """The views are not CSRF-exempt; a project with CsrfViewMiddleware protects them."""
    from django.urls import resolve

    api()
    for route in ("catalog", "ask", "run", "validate"):
        view = resolve(f"/analytics/{route}").func
        assert not getattr(view, "csrf_exempt", False)
