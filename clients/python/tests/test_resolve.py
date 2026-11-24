"""Python-specific validation behavior. Cross-language behavior is covered by test_parity.py."""

import pytest

from noquery import has_access, plan_joins, resolve_query
from noquery.resolve import is_date_only, is_number, is_timezone, js_str

from conftest import AUTH_A, ORG_A


def errors(model, ir, auth=AUTH_A, **options):
    resolved = resolve_query(model, ir, auth, **options)
    assert not resolved["ok"]
    return [(e["code"], e.get("path")) for e in resolved["errors"]]


def test_helpers():
    assert is_number(3) and is_number(2.5)
    assert not is_number(True) and not is_number(float("nan")) and not is_number(float("inf")) and not is_number("3")
    assert is_timezone("America/Toronto") and is_timezone("UTC")
    assert not is_timezone("Mars/Olympus") and not is_timezone("") and not is_timezone(None) and not is_timezone("../etc/passwd")
    assert is_date_only("2026-03-01") and not is_date_only("2026-03-01T00:00:00Z") and not is_date_only("2026-03-01\n")
    assert js_str(True) == "true" and js_str(None) == "null" and js_str(3.0) == "3" and js_str(2.5) == "2.5"


def test_has_access():
    assert has_access(None, {}) and has_access([], {})
    assert has_access(["a", "b"], {"roles": ["b"]})
    assert not has_access(["a"], {"roles": ["b"]})
    assert not has_access(["a"], {})
    assert not has_access(["a"], {"roles": "a"})


def test_booleans_are_not_numbers_or_versions(fleet):
    assert errors(fleet, {"version": True, "measures": ["trips.count"]}) == [("invalid_ir", None)]
    assert errors(fleet, {"version": 1, "measures": ["trips.count"], "limit": True}) == [("invalid_limit", "limit")]
    flt = [{"field": "trips.distance_m", "op": "gt", "value": True}]
    assert errors(fleet, {"version": 1, "measures": ["trips.count"], "filters": flt}) == [("invalid_filter", "filters[0]")]
    having = [{"field": "trips.count", "op": "gt", "value": False}]
    assert errors(fleet, {"version": 1, "measures": ["trips.count"], "filters": having}) == [("invalid_filter", "filters[0]")]


def test_whole_number_floats_are_valid_limits(fleet):
    resolved = resolve_query(fleet, {"version": 1, "measures": ["trips.count"], "limit": 5.0}, AUTH_A)
    assert resolved["ok"] and resolved["query"]["limit"] == 5 and isinstance(resolved["query"]["limit"], int)


@pytest.mark.parametrize(
    "ir, path",
    [
        ({"version": 1, "measures": "trips.count"}, "measures"),
        ({"version": 1, "dimensions": {"a": 1}}, "dimensions"),
        ({"version": 1, "measures": ["trips.count"], "filters": "x"}, "filters"),
        ({"version": 1, "measures": ["trips.count"], "orderBy": {"field": "trips.count"}}, "orderBy"),
        ({"version": 1, "measures": ["trips.count"], "time": "last_week"}, "time"),
    ],
)
def test_wrongly_shaped_sections_are_rejected(fleet, ir, path):
    assert errors(fleet, ir) == [("invalid_ir", path)]


def test_non_mapping_ir_is_rejected(fleet):
    for ir in (None, [], "x", 1):
        assert errors(fleet, ir) == [("invalid_ir", None)]


def test_field_references_must_match_exactly(fleet):
    for ref in ("trips.count\n", " trips.count", "trips.count.extra", "trips.", ".count", 'trips."count"'):
        assert errors(fleet, {"version": 1, "measures": [ref]})[0] == ("unknown_field", "measures[0]")


def test_impossible_dates_are_rejected(fleet):
    bad = {"version": 1, "measures": ["trips.count"], "time": {"field": "trips.started_at", "range": {"from": "2026-02-30", "to": "2026-03-01"}}}
    assert errors(fleet, bad) == [("invalid_time", "time.range.from")]
    flt = [{"field": "trips.started_at", "op": "gte", "value": "2026-13-01"}]
    assert errors(fleet, {"version": 1, "measures": ["trips.count"], "filters": flt}) == [("invalid_filter", "filters[0]")]


def test_tuples_are_accepted_wherever_lists_are(fleet):
    ir = {"version": 1, "measures": ("trips.count",), "dimensions": ("devices.model",), "filters": ({"field": "devices.status", "op": "in", "value": ("active",)},)}
    resolved = resolve_query(fleet, ir, {"orgId": (ORG_A,)})
    assert resolved["ok"]
    scope = resolved["query"]["where"][0]
    assert scope == {"kind": "scope", "alias": "t0", "column": "org_id", "value": [ORG_A]}


def test_row_scope_fails_closed(fleet):
    for auth in ({}, {"orgId": None}, {"orgId": []}, {"orgId": {"id": ORG_A}}, {"orgId": [ORG_A, None]}, {"orgId": [[ORG_A]]}):
        assert errors(fleet, {"version": 1, "measures": ["trips.count"]}, auth) == [("missing_auth_attribute", None)]


def test_role_gated_fields(fleet):
    from noquery import merge_semantics

    model = merge_semantics(fleet, {"entities": {"trips": {"dimensions": {"note": {"roles": ["support"]}}, "measures": {"count": {"roles": ["analyst"]}}}}})
    ir = {"version": 1, "measures": ["trips.count"], "dimensions": ["trips.note"]}
    assert errors(model, ir) == [("unknown_field", "dimensions[0]")]
    assert errors(model, ir, {"orgId": ORG_A, "roles": ["support"]}) == [("unknown_field", "measures[0]")]
    assert resolve_query(model, ir, {"orgId": ORG_A, "roles": ["support", "analyst"]})["ok"]


def test_plan_joins_orders_parents_first_and_reports_fan_out(fleet):
    plan = plan_joins(fleet, "trips", ["organizations"], AUTH_A)
    assert [(s["entity"], s["parent"]) for s in plan["steps"]] == [("devices", "trips"), ("organizations", "devices")]
    assert plan["steps"][0]["on"] == [["device_id", "id"]]
    blocked = plan_joins(fleet, "organizations", ["trips", "patients"], {"roles": ["clinical"]})
    assert blocked["unreachable"] == ["trips", "patients"]
    assert blocked["fanOut"] == {"trips"}


def test_one_to_many_joins_are_followed_backwards_only():
    model = {
        "entities": {
            "orders": {"table": "orders", "dimensions": {"status": {"type": "string", "column": "status"}}, "measures": {"count": {"agg": "count"}}},
            "lines": {"table": "lines", "dimensions": {"sku": {"type": "string", "column": "sku"}}, "measures": {"count": {"agg": "count"}}},
        },
        "joins": [{"from": "orders", "to": "lines", "on": [["id", "order_id"]], "relationship": "one_to_many"}],
    }
    ok = resolve_query(model, {"version": 1, "measures": ["lines.count"], "dimensions": ["orders.status"]}, {})
    assert ok["ok"] and ok["query"]["joins"][0]["on"] == [["order_id", "id"]]
    assert errors(model, {"version": 1, "measures": ["orders.count"], "dimensions": ["lines.sku"]}, {}) == [("no_join_path", None)]


def test_resolving_does_not_mutate_the_ir(fleet):
    ir = {"version": 1, "measures": ["trips.count"], "time": {"field": "trips.started_at", "grain": "day"}}
    before = repr(ir)
    resolved = resolve_query(fleet, ir, AUTH_A, default_timezone="Europe/Paris")
    assert repr(ir) == before
    assert resolved["query"]["timezone"] == "Europe/Paris"
