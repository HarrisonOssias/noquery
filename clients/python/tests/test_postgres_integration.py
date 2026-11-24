"""Runs the engine against a real PostgreSQL through every executor.

Mirrors packages/postgres/test/pglite.test.ts: same tables, same rows, same expected results.
"""

import pytest

psycopg = pytest.importorskip("psycopg", reason="needs psycopg")
sa = pytest.importorskip("sqlalchemy", reason="needs the sqlalchemy extra")
pytest.importorskip("psycopg2", reason="needs psycopg2")
pytest.importorskip("django", reason="needs the django extra")

from django.db import connection as django_connection
from django.db import transaction

from noquery import (
    CompiledQuery,
    Noquery,
    QueryValidationError,
    dbapi_catalog_query,
    dbapi_executor,
    draft_semantics,
    introspect_postgres,
    merge_semantics,
    postgres_dialect,
)
from noquery.integrations.django import django_executor, introspect_django
from noquery.integrations.sqlalchemy import introspect_sqlalchemy, sqlalchemy_executor

from conftest import AUTH_A, ORG_A, ORG_B, load_parity

FLEET = load_parity()["model"]
MARCH = {"from": "2026-03-01", "to": "2026-04-01"}
MARCH_UTC = {"from": "2026-03-01T00:00:00Z", "to": "2026-04-01T00:00:00Z"}
IN_MARCH = {"field": "trips.started_at", "range": MARCH_UTC}

EXECUTORS = ["sqlalchemy-psycopg", "sqlalchemy-psycopg2", "django", "psycopg", "psycopg2"]


def sqlalchemy_url(database_url, driver):
    return database_url.replace("postgresql://", f"postgresql+{driver}://", 1)


@pytest.fixture(scope="module", params=EXECUTORS)
def executor(request, database_url):
    kind = request.param
    if kind.startswith("sqlalchemy-"):
        engine = sa.create_engine(sqlalchemy_url(database_url, kind.split("-")[1]))
        yield sqlalchemy_executor(engine)
        engine.dispose()
    elif kind == "django":
        yield django_executor()
        django_connection.close()
    elif kind == "psycopg":
        yield dbapi_executor(lambda: psycopg.connect(database_url))
    else:
        import contextlib

        import psycopg2

        @contextlib.contextmanager
        def connect():
            connection = psycopg2.connect(database_url)
            try:
                yield connection
            finally:
                connection.close()

        yield dbapi_executor(connect)


@pytest.fixture(scope="module")
def nq(executor):
    return Noquery(model=FLEET, dialect=postgres_dialect(), executor=executor, default_timezone="America/Toronto")


def rows(nq, auth=AUTH_A, **ir):
    return nq.run({"version": 1, **ir}, auth).rows


def test_counts_within_an_absolute_range_scoped_to_the_callers_organization(nq):
    result = nq.run({"version": 1, "measures": ["trips.count"], "time": IN_MARCH}, AUTH_A)
    assert result.rows == [{"trips.count": 3}]
    assert result.chart["type"] == "kpi"
    assert result.truncated is False
    assert result.params[0] == ORG_A


def test_reads_date_only_ranges_in_the_querys_time_zone(nq):
    # 2026-03-01T04:30Z is still Feb 28 in Toronto, so local March holds one trip fewer than UTC March.
    assert rows(nq, measures=["trips.count"], time={"field": "trips.started_at", "range": MARCH}) == [{"trips.count": 2}]
    assert rows(nq, measures=["trips.count"], time={"field": "trips.started_at", "range": MARCH, "tz": "UTC"}) == [{"trips.count": 3}]


def test_scopes_to_a_different_organization_or_to_several(nq):
    assert rows(nq, auth={"orgId": ORG_B}, measures=["trips.count"], time=IN_MARCH) == [{"trips.count": 1}]
    assert rows(nq, auth={"orgId": [ORG_A, ORG_B]}, measures=["trips.count"], time=IN_MARCH) == [{"trips.count": 4}]
    by_org = rows(nq, auth={"orgId": [ORG_A, ORG_B]}, measures=["trips.count"], dimensions=["organizations.name"], time=IN_MARCH)
    assert by_org == [{"organizations.name": "Acme Care", "trips.count": 3}, {"organizations.name": "Bayside", "trips.count": 1}]


def test_buckets_by_local_day_in_the_querys_time_zone(nq):
    window = {"from": "2026-02-28", "to": "2026-03-03"}
    toronto = rows(nq, measures=["trips.count"], time={"field": "trips.started_at", "grain": "day", "range": window})
    assert toronto == [
        {"trips.started_at__day": "2026-02-28", "trips.count": 1},
        {"trips.started_at__day": "2026-03-01", "trips.count": 1},
        {"trips.started_at__day": "2026-03-02", "trips.count": 1},
    ]
    utc = rows(nq, measures=["trips.count"], time={"field": "trips.started_at", "grain": "day", "range": window, "tz": "UTC"})
    assert utc == [
        {"trips.started_at__day": "2026-03-01", "trips.count": 2},
        {"trips.started_at__day": "2026-03-02", "trips.count": 1},
    ]
    hourly = rows(nq, measures=["trips.count"], time={"field": "trips.started_at", "grain": "hour", "range": {"from": "2026-03-01", "to": "2026-03-02"}, "tz": "UTC"})
    assert [r["trips.started_at__hour"] for r in hourly] == ["2026-03-01T04:00", "2026-03-01T06:00"]


def test_joins_one_and_two_hops_and_applies_every_entitys_row_scope(nq):
    by_model = rows(nq, measures=["trips.count"], dimensions=["devices.model"], time=IN_MARCH)
    assert by_model == [{"devices.model": "X1", "trips.count": 2}, {"devices.model": "X2", "trips.count": 1}]
    by_region = rows(nq, measures=["trips.count"], dimensions=["organizations.region"], time=IN_MARCH)
    assert by_region == [{"organizations.region": "east", "trips.count": 3}]


def test_escapes_like_wildcards_in_contains_filters(nq):
    def count(op, value):
        return rows(nq, measures=["trips.count"], filters=[{"field": "trips.note", "op": op, "value": value}])

    assert count("contains", "100%") == [{"trips.count": 1}]
    assert count("contains", "_") == [{"trips.count": 1}]
    assert count("starts_with", "BUMPY") == [{"trips.count": 1}]


def test_keeps_empty_values_when_filtering_with_neq_and_not_in(nq):
    def count(flt):
        return rows(nq, measures=["trips.count"], time=IN_MARCH, filters=[flt])

    assert count({"field": "trips.note", "op": "neq", "value": "bumpy_ride"}) == [{"trips.count": 2}]
    assert count({"field": "trips.note", "op": "not_in", "value": ["bumpy_ride", "100% fine"]}) == [{"trips.count": 1}]
    assert count({"field": "trips.note", "op": "is_null"}) == [{"trips.count": 1}]
    assert count({"field": "trips.note", "op": "is_not_null"}) == [{"trips.count": 2}]


def test_filters_on_enums_booleans_numbers_lists_and_times(nq):
    def count(flt):
        return rows(nq, measures=["trips.count"], time=IN_MARCH, filters=[flt])

    assert count({"field": "devices.status", "op": "eq", "value": "retired"}) == [{"trips.count": 1}]
    assert count({"field": "devices.status", "op": "neq", "value": "retired"}) == [{"trips.count": 2}]
    assert count({"field": "devices.status", "op": "in", "value": ["retired", "inactive"]}) == [{"trips.count": 1}]
    assert count({"field": "devices.status", "op": "not_in", "value": ["retired"]}) == [{"trips.count": 2}]
    assert count({"field": "trips.is_assisted", "op": "eq", "value": True}) == [{"trips.count": 1}]
    assert count({"field": "trips.distance_m", "op": "gte", "value": 1000}) == [{"trips.count": 2}]
    assert count({"field": "trips.distance_m", "op": "lt", "value": 999.5}) == [{"trips.count": 1}]
    assert count({"field": "trips.distance_m", "op": "in", "value": [500, 2000]}) == [{"trips.count": 2}]
    assert count({"field": "devices.model", "op": "in", "value": ["X2", "X9"]}) == [{"trips.count": 1}]
    assert count({"field": "trips.started_at", "op": "gte", "value": "2026-03-02"}) == [{"trips.count": 1}]
    assert count({"field": "trips.started_at", "op": "in", "value": ["2026-03-01T06:00:00Z", "2026-03-02T10:00:00-05:00"]}) == [{"trips.count": 2}]
    assert count({"field": "trips.started_at", "op": "not_in", "value": ["2026-03-01T06:00:00Z"]}) == [{"trips.count": 2}]


def test_computes_custom_measures_and_filters_on_totals(nq):
    measures = rows(nq, measures=["trips.avg_duration_min", "trips.total_distance_km", "trips.unique_devices"], time=IN_MARCH)
    assert measures == [{"trips.avg_duration_min": 20, "trips.total_distance_km": 3.5, "trips.unique_devices": 2}]
    assert all(type(v) in (int, float) for v in measures[0].values())
    having = rows(nq, measures=["trips.count"], dimensions=["devices.model"], time=IN_MARCH, filters=[{"field": "trips.count", "op": "gt", "value": 1}])
    assert having == [{"devices.model": "X1", "trips.count": 2}]
    assert rows(nq, measures=["devices.active_count", "devices.count"]) == [{"devices.active_count": 1, "devices.count": 2}]


def test_resolves_relative_presets_against_the_database_clock(nq):
    assert rows(nq, measures=["trips.count"], time={"field": "trips.started_at", "range": "last_7_days"}) == [{"trips.count": 1}]
    assert rows(nq, measures=["trips.count"], time={"field": "trips.started_at", "range": "last_year"}) == [{"trips.count": 0}]
    for preset in ("today", "yesterday", "this_week", "last_week", "this_month", "last_month", "this_quarter", "last_quarter", "this_year"):
        result = rows(nq, measures=["trips.count"], time={"field": "trips.started_at", "grain": "month", "range": preset})
        assert isinstance(result, list)


def test_reports_truncation_when_more_rows_match_than_the_limit(nq):
    ir = {"version": 1, "measures": ["trips.count"], "dimensions": ["trips.note"], "orderBy": [{"field": "trips.note", "dir": "asc"}], "limit": 2}
    result = nq.run(ir, AUTH_A)
    assert len(result.rows) == 2
    assert result.truncated is True
    assert result.rows[0]["trips.note"] == "100% fine"


def test_trusted_sql_with_a_percent_sign_runs(executor):
    model = merge_semantics(FLEET, {"entities": {"trips": {"measures": {"even_metres": {"sql": "count(*) FILTER (WHERE ${TABLE}.distance_m::int % 2 = 0)"}}}}})
    engine = Noquery(model=model, dialect=postgres_dialect(), executor=executor)
    assert engine.run({"version": 1, "measures": ["trips.even_metres"], "time": IN_MARCH}, AUTH_A).rows == [{"trips.even_metres": 3}]
    unscoped = merge_semantics(model, {"entities": {"trips": {"rowScope": []}}})
    engine = Noquery(model=unscoped, dialect=postgres_dialect(), executor=executor)
    # No row scope and no filters: the statement has no parameters at all, and % must still work.
    assert engine.run({"version": 1, "measures": ["trips.even_metres"]}, {}).rows == [{"trips.even_metres": 4}]


def test_runs_every_query_read_only(executor, database_url):
    with pytest.raises(Exception, match="read-only"):
        executor.execute(CompiledQuery(sql="DELETE FROM trips", params=[]), timeout_ms=1000, max_rows=10)
    with psycopg.connect(database_url) as connection:
        assert connection.execute("SELECT count(*) FROM trips").fetchone()[0] == 5


def test_enforces_the_statement_timeout(executor):
    with pytest.raises(Exception, match="statement timeout"):
        executor.execute(CompiledQuery(sql="SELECT pg_sleep(5)", params=[]), timeout_ms=100, max_rows=10)
    # The connection is usable again afterwards.
    assert executor.execute(CompiledQuery(sql="SELECT 1 AS n", params=[]), timeout_ms=1000, max_rows=10).rows == [{"n": 1}]


def test_rejects_invalid_queries_before_touching_the_database(nq):
    with pytest.raises(QueryValidationError) as raised:
        nq.run({"version": 1, "measures": ["trips.count"], "dimensions": ["devices.serial"]}, AUTH_A)
    assert raised.value.errors[0]["code"] == "aggregate_only"


def test_numeric_paramstyle_is_refused_by_python_executors(executor, fleet):
    engine = Noquery(model=fleet, dialect=postgres_dialect(paramstyle="numeric"), executor=executor)
    with pytest.raises(ValueError, match="pyformat"):
        engine.run({"version": 1, "measures": ["trips.count"]}, AUTH_A)


# --- Django specifics --------------------------------------------------------------------------


def test_django_executor_inside_an_open_transaction_leaves_it_writable(database_url):
    executor = django_executor()
    engine = Noquery(model=FLEET, dialect=postgres_dialect(), executor=executor)
    try:
        with transaction.atomic():
            with django_connection.cursor() as cursor:
                cursor.execute("UPDATE trips SET note = 'edited' WHERE note = 'recent'")
            # The query sees the caller's uncommitted write, and cannot write itself.
            seen = engine.run({"version": 1, "measures": ["trips.count"], "filters": [{"field": "trips.note", "op": "eq", "value": "edited"}]}, AUTH_A)
            assert seen.rows == [{"trips.count": 1}]
            with pytest.raises(Exception, match="read-only"):
                executor.execute(CompiledQuery(sql="DELETE FROM trips", params=[]), timeout_ms=1000, max_rows=10)
            with django_connection.cursor() as cursor:
                # The read-only mode and the timeout ended with the query's savepoint.
                cursor.execute("SHOW transaction_read_only")
                assert cursor.fetchone()[0] == "off"
                cursor.execute("SHOW statement_timeout")
                assert cursor.fetchone()[0] == "0"
                cursor.execute("UPDATE trips SET note = 'edited twice' WHERE note = 'edited'")
                assert cursor.rowcount == 1
            transaction.set_rollback(True)
    finally:
        django_connection.close()
    with psycopg.connect(database_url) as connection:
        assert connection.execute("SELECT count(*) FROM trips WHERE note = 'recent'").fetchone()[0] == 1


# --- introspection to a working engine ---------------------------------------------------------

SCOPES = {
    "entities": {
        "trips": {"rowScope": [{"column": "org_id", "authKey": "orgId"}]},
        "devices": {"rowScope": [{"column": "org_id", "authKey": "orgId"}]},
        "organizations": {"rowScope": [{"column": "id", "authKey": "orgId"}]},
    }
}
DRAFTED_IR = {
    "version": 1,
    "measures": ["trips.count", "trips.total_distance_m"],
    "dimensions": ["organizations.region", "devices.status"],
    "time": IN_MARCH,
}
DRAFTED_ROWS = [
    {"organizations.region": "east", "devices.status": "active", "trips.count": 2, "trips.total_distance_m": 3000},
    {"organizations.region": "east", "devices.status": "retired", "trips.count": 1, "trips.total_distance_m": 500},
]


def table(graph, name):
    return next(t for t in graph["tables"] if t["name"] == name)


def column(graph, table_name, name):
    return next(c for c in table(graph, table_name)["columns"] if c["name"] == name)


def test_introspect_postgres_reads_the_catalog_and_drafts_a_working_model(database_url, executor):
    graph = introspect_postgres(dbapi_catalog_query(lambda: psycopg.connect(database_url)))
    assert sorted(t["name"] for t in graph["tables"]) == ["devices", "organizations", "patients", "trips"]
    fks = table(graph, "trips")["foreignKeys"]
    assert {"columns": ["device_id"], "referencedTable": "devices", "referencedSchema": "public", "referencedColumns": ["id"]} in fks
    assert {"columns": ["org_id"], "referencedTable": "organizations", "referencedSchema": "public", "referencedColumns": ["id"]} in fks
    assert column(graph, "trips", "id")["kind"] == "integer" and column(graph, "trips", "id")["primaryKey"] is True
    assert column(graph, "trips", "started_at")["withTimezone"] is True
    assert column(graph, "trips", "distance_m") == {"name": "distance_m", "kind": "number", "dbType": "double precision", "nullable": True, "comment": "Metres travelled"}
    status = column(graph, "devices", "status")
    assert status["kind"] == "enum" and status["enumValues"] == ["active", "inactive", "retired"] and status["dbType"] == "device_status"
    assert table(graph, "organizations")["comment"] == "Customer organizations"

    tables_only = introspect_postgres(dbapi_catalog_query(lambda: psycopg.connect(database_url)), schemas=["public"], include_views=False)
    assert len(tables_only["tables"]) == 4

    model = merge_semantics(draft_semantics(graph), SCOPES)
    assert model["entities"]["patients"]["dimensions"]["email"]["policy"] == "hidden"
    engine = Noquery(model=model, dialect=postgres_dialect(), executor=executor)
    assert engine.run(DRAFTED_IR, {"orgId": ORG_A}).rows == DRAFTED_ROWS


def test_reflected_sqlalchemy_metadata_drafts_a_working_model(database_url, executor):
    engine = sa.create_engine(sqlalchemy_url(database_url, "psycopg"))
    try:
        metadata = sa.MetaData()
        metadata.reflect(engine)
    finally:
        engine.dispose()
    graph = introspect_sqlalchemy(metadata)
    assert sorted(t["name"] for t in graph["tables"]) == ["devices", "organizations", "patients", "trips"]
    assert column(graph, "devices", "status")["enumValues"] == ["active", "inactive", "retired"]
    assert column(graph, "trips", "distance_m")["comment"] == "Metres travelled"
    model = merge_semantics(draft_semantics(graph), SCOPES)
    nq = Noquery(model=model, dialect=postgres_dialect(), executor=executor)
    assert nq.run(DRAFTED_IR, {"orgId": ORG_A}).rows == DRAFTED_ROWS


def test_django_models_draft_a_working_model(executor):
    graph = introspect_django(app_labels=["fleetapp"], include=["organizations", "devices", "trips", "patients"])
    # The Django models declare a few columns the test tables do not have; leave them out of the model.
    overlay = {
        "entities": {
            **SCOPES["entities"],
            "trips": {
                **SCOPES["entities"]["trips"],
                "dimensions": {"service_date": None, "cost": None},
                "measures": {"total_cost": None, "avg_cost": None},
            },
        }
    }
    model = merge_semantics(draft_semantics(graph), overlay)
    nq = Noquery(model=model, dialect=postgres_dialect(), executor=executor)
    assert nq.run(DRAFTED_IR, {"orgId": ORG_A}).rows == DRAFTED_ROWS
