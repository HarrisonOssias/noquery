from datetime import datetime, timezone
from decimal import Decimal

import pytest

from noquery import (
    Noquery,
    PlannerFailedError,
    PlannerUnavailableError,
    QueryValidationError,
    Result,
    Validation,
    postgres_dialect,
)
from noquery.engine import normalize_rows

from conftest import AUTH_A, ORG_A
from support import FakeDialect, FakeExecutor, ScriptedPlanner, query_plan

NOW = datetime(2026, 3, 1, 4, 30, tzinfo=timezone.utc)


def make(fleet, rows=None, planner=None, audit=None, **options):
    executor = FakeExecutor(rows=rows or [])
    nq = Noquery(model=fleet, dialect=FakeDialect(), executor=executor, planner=planner, on_audit=audit, now=lambda: NOW, **options)
    return nq, executor


def test_a_broken_model_is_rejected_at_construction():
    model = {"entities": {"trips": {"table": "trips", "dimensions": {"x": {"type": "string"}}, "measures": {}}}}
    with pytest.raises(ValueError, match="Semantic model has problems:\n- entities.trips.dimensions.x: Dimension needs a column or sql."):
        Noquery(model=model, dialect=FakeDialect(), executor=FakeExecutor())


def test_run_returns_rows_columns_explanation_and_chart(fleet):
    nq, executor = make(fleet, rows=[{"devices.model": "X1", "trips.count": Decimal("12")}, {"devices.model": "X2", "trips.count": "3"}])
    result = nq.run({"version": 1, "measures": ["trips.count"], "dimensions": ["devices.model"]}, AUTH_A)
    assert isinstance(result, Result) and result.ok and result.type == "answer"
    assert result.rows == [{"devices.model": "X1", "trips.count": 12}, {"devices.model": "X2", "trips.count": 3}]
    assert [type(r["trips.count"]) for r in result.rows] == [int, int]
    assert [c.key for c in result.columns] == ["devices.model", "trips.count"]
    assert result.columns[1].label == "Trips" and result.columns[1].role == "measure"
    assert result.explanation == "Trips by device model."
    assert result.chart == {"type": "bar", "x": "devices.model", "y": ["trips.count"]}
    assert result.labeled_rows()[0] == {"Device model": "X1", "Trips": 12}
    assert result.sql == "-- trips" and result.params == [] and result.truncated is False
    assert isinstance(result.duration_ms, int)
    assert executor.calls[0]["timeout_ms"] == 15000 and executor.calls[0]["max_rows"] == 1000


def test_run_passes_limits_and_timeout_to_the_executor(fleet):
    nq, executor = make(fleet, timeout_ms=2500, default_limit=20, max_limit=40)
    nq.run({"version": 1, "measures": ["trips.count"]}, AUTH_A)
    nq.run({"version": 1, "measures": ["trips.count"], "limit": 40}, AUTH_A)
    assert [(c["timeout_ms"], c["max_rows"]) for c in executor.calls] == [(2500, 20), (2500, 40)]
    with pytest.raises(QueryValidationError):
        nq.run({"version": 1, "measures": ["trips.count"], "limit": 41}, AUTH_A)


def test_run_reports_truncation(fleet):
    nq = Noquery(model=fleet, dialect=FakeDialect(), executor=FakeExecutor(rows=[{"trips.count": 1}], truncated=True))
    assert nq.run({"version": 1, "measures": ["trips.count"]}, AUTH_A).truncated is True


def test_invalid_queries_raise_and_never_reach_the_database(fleet):
    nq, executor = make(fleet)
    with pytest.raises(QueryValidationError) as raised:
        nq.run({"version": 1, "measures": ["trips.count"], "dimensions": ["devices.serial"]}, AUTH_A)
    error = raised.value
    assert error.status == 422 and error.code == "invalid_query"
    assert error.errors == [{"code": "aggregate_only", "message": '"devices.serial" can only be counted, not grouped, filtered or listed.', "path": "dimensions[0]"}]
    assert error.message == '"devices.serial" can only be counted, not grouped, filtered or listed.'
    assert executor.calls == []


def test_validate_and_compile(fleet):
    nq = Noquery(model=fleet, dialect=postgres_dialect(), executor=FakeExecutor())
    ok = nq.validate({"version": 1, "measures": ["trips.count"]}, AUTH_A)
    assert ok == Validation(ok=True, errors=[])
    bad = nq.validate({"version": 1, "measures": ["trips.nope"]}, AUTH_A)
    assert bad.ok is False and bad.errors[0]["code"] == "unknown_field"
    compiled = nq.compile({"version": 1, "measures": ["trips.count"]}, AUTH_A)
    assert compiled.sql == 'SELECT count(*) AS "trips.count"\nFROM "trips" AS "t0"\nWHERE "t0"."org_id" = %(p1)s\nORDER BY "trips.count" DESC NULLS LAST\nLIMIT 1001'
    assert compiled.params == [ORG_A]
    with pytest.raises(QueryValidationError):
        nq.compile({"version": 1, "measures": ["trips.nope"]}, AUTH_A)


def test_catalog_respects_the_caller(fleet):
    nq, _ = make(fleet)
    assert [e["name"] for e in nq.catalog(AUTH_A)["entities"]] == ["organizations", "devices", "trips"]
    assert "patients" in [e["name"] for e in nq.catalog({"roles": ["clinical"]})["entities"]]


def test_ask_needs_a_planner(fleet):
    nq, _ = make(fleet)
    assert nq.has_planner is False
    with pytest.raises(PlannerUnavailableError) as raised:
        nq.ask("How many trips?", AUTH_A)
    assert raised.value.status == 501 and raised.value.code == "planner_not_configured"


def test_ask_answers_and_hands_the_planner_the_callers_catalog(fleet):
    planner = ScriptedPlanner(query_plan(measures=["trips.count"]))
    nq, _ = make(fleet, rows=[{"trips.count": 7}], planner=planner, default_timezone="America/Toronto")
    result = nq.ask("How many trips?", AUTH_A)
    assert nq.has_planner is True
    assert result.type == "answer" and result.rows == [{"trips.count": 7}]
    assert result.ir == {"version": 1, "measures": ["trips.count"]}
    assert result.chart == {"type": "kpi", "y": ["trips.count"]}
    call = planner.calls[0]
    assert call["question"] == "How many trips?" and call["timezone"] == "America/Toronto" and call["now"] == NOW
    assert [e["name"] for e in call["catalog"]["entities"]] == ["organizations", "devices", "trips"]
    assert "history" not in call and "repair" not in call


def test_ask_applies_the_askers_time_zone_unless_the_plan_names_one(fleet):
    plan = query_plan(measures=["trips.count"], time={"field": "trips.started_at", "grain": "day"})
    pinned = query_plan(measures=["trips.count"], time={"field": "trips.started_at", "grain": "day", "tz": "UTC"})
    planner = ScriptedPlanner(plan, pinned, plan)
    dialect = FakeDialect()
    nq = Noquery(model=fleet, dialect=dialect, executor=FakeExecutor(), planner=planner, default_timezone="America/Toronto")
    first = nq.ask("Trips per day", AUTH_A, timezone="Asia/Tokyo")
    nq.ask("Trips per day", AUTH_A, timezone="Asia/Tokyo")
    nq.ask("Trips per day", AUTH_A)
    assert [q["timezone"] for q in dialect.compiled] == ["Asia/Tokyo", "UTC", "America/Toronto"]
    assert first.ir["time"]["tz"] == "Asia/Tokyo"
    assert planner.calls[0]["timezone"] == "Asia/Tokyo" and planner.calls[2]["timezone"] == "America/Toronto"
    assert "tz" not in plan["ir"]["time"]


def test_ask_passes_history_through(fleet):
    planner = ScriptedPlanner(query_plan(measures=["trips.count"]), query_plan(measures=["trips.count"]))
    nq, _ = make(fleet, planner=planner)
    first = nq.ask("Trips by region", AUTH_A)
    nq.ask("Now only east", AUTH_A, history=[first.to_turn("Trips by region")])
    assert planner.calls[1]["history"] == [{"question": "Trips by region", "result": {"kind": "query", "ir": {"version": 1, "measures": ["trips.count"]}}}]


def test_ask_returns_clarifications_and_refusals_without_running_anything(fleet):
    planner = ScriptedPlanner(
        {"kind": "clarify", "question": "Which trips?", "options": ["All", "Assisted"]},
        {"kind": "unanswerable", "reason": "No battery data."},
    )
    nq, executor = make(fleet, planner=planner)
    clarify = nq.ask("Trips?", AUTH_A)
    assert (clarify.type, clarify.question, clarify.options, clarify.ok) == ("clarify", "Which trips?", ["All", "Assisted"], False)
    unanswerable = nq.ask("Battery?", AUTH_A)
    assert (unanswerable.type, unanswerable.reason) == ("unanswerable", "No battery data.")
    assert executor.calls == []


def test_ask_lets_the_planner_repair_a_rejected_query(fleet):
    bad = query_plan(measures=["trips.nope"])
    planner = ScriptedPlanner(bad, query_plan(measures=["trips.count"]))
    nq, executor = make(fleet, rows=[{"trips.count": 1}], planner=planner)
    result = nq.ask("How many trips?", AUTH_A)
    assert result.type == "answer"
    repair = planner.calls[1]["repair"]
    assert repair["previous"] == bad
    assert repair["errors"] == [{"code": "unknown_field", "message": 'Unknown field "trips.nope".', "path": "measures[0]"}]
    assert planner.calls[1]["question"] == "How many trips?"
    assert len(executor.calls) == 1


def test_ask_gives_up_after_max_repairs(fleet):
    bad = query_plan(measures=["trips.nope"])
    planner = ScriptedPlanner(bad, bad, bad)
    nq, executor = make(fleet, planner=planner)
    result = nq.ask("How many trips?", AUTH_A)
    assert result.type == "invalid" and result.errors[0]["code"] == "unknown_field" and result.ir == bad["ir"]
    assert len(planner.calls) == 2 and executor.calls == []

    planner = ScriptedPlanner(bad, bad, bad)
    nq, _ = make(fleet, planner=planner, max_repairs=0)
    assert nq.ask("How many trips?", AUTH_A).type == "invalid"
    assert len(planner.calls) == 1

    planner = ScriptedPlanner(bad, bad, query_plan(measures=["trips.count"]))
    nq, _ = make(fleet, planner=planner, max_repairs=2)
    assert nq.ask("How many trips?", AUTH_A).type == "answer"


def test_the_planner_cannot_widen_access(fleet):
    """Whatever the model returns, validation and row scope still apply."""
    planner = ScriptedPlanner(query_plan(measures=["patients.count"]), query_plan(measures=["patients.count"]))
    nq, executor = make(fleet, planner=planner)
    result = nq.ask("How many patients?", AUTH_A)
    assert result.type == "invalid" and result.errors[0]["code"] == "unknown_entity"
    assert executor.calls == []


def test_planner_failures_propagate(fleet):
    failure = PlannerFailedError.from_reason("rate_limited", "Try again shortly.", True)
    nq, _ = make(fleet, planner=ScriptedPlanner(failure))
    with pytest.raises(PlannerFailedError) as raised:
        nq.ask("How many trips?", AUTH_A)
    assert (raised.value.status, raised.value.reason, raised.value.retryable) == (503, "rate_limited", True)
    assert PlannerFailedError.from_reason("authentication", "Bad key.").status == 502


def test_audit_events_for_every_outcome(fleet):
    events = []
    planner = ScriptedPlanner(
        query_plan(measures=["trips.count"]),
        {"kind": "clarify", "question": "?", "options": []},
        {"kind": "unanswerable", "reason": "no"},
        query_plan(measures=["trips.nope"]),
        query_plan(measures=["trips.nope"]),
    )
    nq, _ = make(fleet, rows=[{"trips.count": 2}], planner=planner, audit=events.append)
    nq.run({"version": 1, "measures": ["trips.count"]}, AUTH_A)
    with pytest.raises(QueryValidationError):
        nq.run({"version": 1, "measures": ["trips.nope"]}, AUTH_A)
    for question in ("a", "b", "c", "d"):
        nq.ask(question, AUTH_A)

    assert [(e["type"], e["outcome"]) for e in events] == [
        ("run", "answer"),
        ("run", "invalid"),
        ("ask", "answer"),
        ("ask", "clarify"),
        ("ask", "unanswerable"),
        ("ask", "invalid"),
    ]
    assert all(e["at"] == "2026-03-01T04:30:00.000Z" and e["userId"] == "u1" and e["roles"] == ["analyst"] for e in events)
    assert all(isinstance(e["durationMs"], int) for e in events)
    assert events[0]["sql"] == "-- trips" and events[0]["rowCount"] == 1 and events[0]["ir"] == {"version": 1, "measures": ["trips.count"]}
    assert events[1]["errors"][0]["code"] == "unknown_field"
    assert events[2]["question"] == "a" and events[2]["rowCount"] == 1
    assert events[5]["ir"] == {"version": 1, "measures": ["trips.nope"]}


def test_database_errors_are_audited_and_reraised(fleet):
    events = []
    executor = FakeExecutor(error=RuntimeError("connection lost"))
    planner = ScriptedPlanner(query_plan(measures=["trips.count"]))
    nq = Noquery(model=fleet, dialect=FakeDialect(), executor=executor, planner=planner, on_audit=events.append)
    with pytest.raises(RuntimeError, match="connection lost"):
        nq.run({"version": 1, "measures": ["trips.count"]}, {"orgId": ORG_A})
    with pytest.raises(RuntimeError, match="connection lost"):
        nq.ask("How many trips?", {"orgId": ORG_A})
    assert [(e["type"], e["outcome"], e["error"]) for e in events] == [("run", "error", "connection lost"), ("ask", "error", "connection lost")]
    assert "userId" not in events[0] and "roles" not in events[0]


def test_normalize_rows():
    columns = [{"key": "d", "role": "dimension"}, {"key": "m", "role": "measure"}]
    rows = [
        {"d": Decimal("1.50"), "m": Decimal("1.50")},
        {"d": "7", "m": "7"},
        {"d": "x", "m": Decimal("20.000")},
        {"d": None, "m": None},
        {"d": "", "m": " 2.5 "},
        {"d": "", "m": "n/a"},
        {"d": "", "m": 10**30},
        {"d": "", "m": True},
    ]
    out = normalize_rows(rows, columns)
    assert [r["m"] for r in out] == [1.5, 7, 20, None, 2.5, "n/a", 10**30, True]
    assert [type(r["m"]) for r in out[:3]] == [float, int, int]
    assert out[0]["d"] == Decimal("1.50") and out[1]["d"] == "7"
    assert rows[1]["m"] == "7"
    assert normalize_rows([{"d": "7"}], [{"key": "d", "role": "dimension"}]) == [{"d": "7"}]
