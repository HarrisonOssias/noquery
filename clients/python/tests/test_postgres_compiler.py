import pytest

from noquery import merge_semantics, postgres_dialect, resolve_query
from noquery.postgres import quote_ident

from conftest import AUTH_A, ORG_A


def compile_ir(model, ir, auth=AUTH_A, **dialect_options):
    resolved = resolve_query(model, {"version": 1, **ir}, auth)
    assert resolved["ok"], resolved.get("errors")
    return postgres_dialect(**dialect_options).compile(resolved["query"])


def test_quote_ident():
    assert quote_ident("trips") == '"trips"'
    assert quote_ident('we"ird') == '"we""ird"'


def test_unknown_paramstyle_is_rejected():
    with pytest.raises(ValueError, match="paramstyle must be one of pyformat, numeric"):
        postgres_dialect(paramstyle="qmark")


def test_default_output_uses_named_placeholders_and_binds_every_value(fleet):
    compiled = compile_ir(
        fleet,
        {
            "measures": ["trips.count"],
            "dimensions": ["devices.model"],
            "time": {"field": "trips.started_at", "grain": "week", "range": "last_30_days", "tz": "America/Toronto"},
            "filters": [{"field": "trips.note", "op": "contains", "value": "o'brien"}],
            "limit": 50,
        },
    )
    assert compiled.paramstyle == "pyformat"
    assert compiled.params == ["America/Toronto", ORG_A, ORG_A, "%o'brien%"]
    assert compiled.named_params() == {"p1": "America/Toronto", "p2": ORG_A, "p3": ORG_A, "p4": "%o'brien%"}
    assert "o'brien" not in compiled.sql
    assert ORG_A not in compiled.sql
    assert '"t0"."note" ILIKE %(p4)s' in compiled.sql
    assert compiled.sql.endswith("LIMIT 51")
    assert "GROUP BY 1, 2" in compiled.sql
    assert 'LEFT JOIN "devices" AS "t1" ON "t0"."device_id" = "t1"."id"' in compiled.sql


def test_literal_percent_signs_are_escaped_only_for_pyformat(fleet):
    model = merge_semantics(fleet, {"entities": {"trips": {"measures": {"odd": {"sql": "count(*) FILTER (WHERE ${TABLE}.id % 2 = 1)"}}}}})
    assert '"t0".id %% 2 = 1' in compile_ir(model, {"measures": ["trips.odd"]}).sql
    assert '"t0".id % 2 = 1' in compile_ir(model, {"measures": ["trips.odd"]}, paramstyle="numeric").sql


def test_identifiers_are_quoted_and_escaped():
    model = {
        "entities": {
            "odd": {
                "table": 'my "table" 100%',
                "schema": "sales data",
                "dimensions": {"label": {"type": "string", "column": 'the "label"'}},
                "measures": {"count": {"agg": "count"}},
            }
        }
    }
    numeric = compile_ir(model, {"measures": ["odd.count"], "dimensions": ["odd.label"]}, {}, paramstyle="numeric")
    assert 'FROM "sales data"."my ""table"" 100%" AS "t0"' in numeric.sql
    assert '"t0"."the ""label""" AS "odd.label"' in numeric.sql
    pyformat = compile_ir(model, {"measures": ["odd.count"], "dimensions": ["odd.label"]}, {})
    assert 'FROM "sales data"."my ""table"" 100%%" AS "t0"' in pyformat.sql


def test_time_storage_modes(fleet):
    model = merge_semantics(
        fleet,
        {
            "entities": {
                "trips": {
                    "dimensions": {
                        "created_at": {"type": "time", "column": "created_at", "timeStorage": "utc"},
                        "service_date": {"type": "time", "column": "service_date", "timeStorage": "local"},
                    }
                }
            }
        },
    )
    utc = compile_ir(model, {"measures": ["trips.count"], "time": {"field": "trips.created_at", "grain": "day"}}, paramstyle="numeric")
    assert """date_trunc('day', (("t0"."created_at"::timestamp AT TIME ZONE 'UTC') AT TIME ZONE $1::text))""" in utc.sql
    local = compile_ir(model, {"measures": ["trips.count"], "time": {"field": "trips.service_date", "range": {"from": "2026-03-01", "to": "2026-04-01"}}}, paramstyle="numeric")
    assert '("t0"."service_date"::timestamp AT TIME ZONE $3::text) >= ($2::timestamp AT TIME ZONE $3::text)' in local.sql
    assert local.params == [ORG_A, "2026-03-01", "UTC", "2026-04-01"]


def test_unsupported_shapes_raise_instead_of_emitting_sql(fleet):
    resolved = resolve_query(fleet, {"version": 1, "measures": ["trips.count"], "time": {"field": "trips.started_at", "grain": "day", "range": "today"}}, AUTH_A)
    dialect = postgres_dialect()

    def broken(**changes):
        query = {**resolved["query"], **changes}
        with pytest.raises(ValueError):
            dialect.compile(query)

    broken(measures=[{"key": "trips.count", "alias": "t0", "measure": {"agg": "median); DROP TABLE trips; --", "column": "id"}}])
    broken(time={**resolved["query"]["time"], "grain": "day'); DROP TABLE trips; --"})
    broken(where=[{"kind": "time_range", "alias": "t0", "dimension": {"type": "time", "column": "started_at"}, "range": "last_decade"}])
    broken(where=[{"kind": "dimension", "alias": "t0", "dimension": {"type": "string", "column": "note"}, "op": "regex", "value": "x"}])


def test_limit_is_always_an_integer_literal(fleet):
    resolved = resolve_query(fleet, {"version": 1, "measures": ["trips.count"]}, AUTH_A)
    with pytest.raises(ValueError):
        postgres_dialect().compile({**resolved["query"], "limit": "1; DROP TABLE trips"})
