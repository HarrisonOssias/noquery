import datetime
import decimal
import uuid

from noquery import Noquery, PlannerFailedError, PlannerRefusalError, postgres_dialect
from noquery.http import handle, to_jsonable

from conftest import AUTH_A
from support import FakeDialect, FakeExecutor, ScriptedPlanner, query_plan


def engine(fleet, rows=None, planner=None, error=None):
    return Noquery(model=fleet, dialect=FakeDialect(), executor=FakeExecutor(rows=rows or [], error=error), planner=planner)


def test_catalog(fleet):
    status, payload = handle(engine(fleet), "catalog", None, AUTH_A)
    assert status == 200 and [e["name"] for e in payload["entities"]] == ["organizations", "devices", "trips"]


def test_run_returns_the_http_api_shape(fleet):
    rows = [{"trips.started_at__day": "2026-03-01", "trips.count": decimal.Decimal("3")}]
    ir = {"version": 1, "measures": ["trips.count"], "time": {"field": "trips.started_at", "grain": "day"}}
    status, payload = handle(engine(fleet, rows=rows), "run", {"ir": ir}, AUTH_A)
    assert status == 200
    assert set(payload) == {"ir", "sql", "params", "columns", "rows", "truncated", "explanation", "chart", "durationMs"}
    assert payload["rows"] == [{"trips.started_at__day": "2026-03-01", "trips.count": 3}]
    assert payload["columns"][0] == {"key": "trips.started_at__day", "ref": "trips.started_at", "role": "time", "type": "time", "label": "Trip started at", "grain": "day"}
    assert payload["chart"] == {"type": "line", "x": "trips.started_at__day", "y": ["trips.count"]}


def test_validate(fleet):
    nq = engine(fleet)
    assert handle(nq, "validate", {"ir": {"version": 1, "measures": ["trips.count"]}}, AUTH_A) == (200, {"ok": True})
    status, payload = handle(nq, "validate", {"ir": {"version": 1, "measures": ["trips.nope"]}}, AUTH_A)
    assert status == 200 and payload["ok"] is False and payload["errors"][0]["code"] == "unknown_field"


def test_invalid_queries_are_422(fleet):
    status, payload = handle(engine(fleet), "run", {"ir": {"version": 1, "measures": ["trips.nope"]}}, AUTH_A)
    assert status == 422
    assert payload["error"]["code"] == "invalid_query" and payload["error"]["message"] == 'Unknown field "trips.nope".'
    assert payload["error"]["errors"] == [{"code": "unknown_field", "message": 'Unknown field "trips.nope".', "path": "measures[0]"}]


def test_bad_requests_are_400(fleet):
    nq = engine(fleet, planner=ScriptedPlanner())
    for route, body in [("run", {}), ("run", {"ir": [1]}), ("validate", {"ir": "x"}), ("ask", {}), ("ask", {"question": "  "}), ("ask", {"question": 5}), ("run", [1]), ("run", "x")]:
        status, payload = handle(nq, route, body, AUTH_A)
        assert status == 400 and payload["error"]["code"] == "bad_request", (route, body)
    assert handle(nq, "nope", {}, AUTH_A)[0] == 404


def test_ask(fleet):
    planner = ScriptedPlanner(query_plan(measures=["trips.count"]), {"kind": "clarify", "question": "Which?", "options": ["A"]})
    nq = engine(fleet, rows=[{"trips.count": 4}], planner=planner)
    history = [{"question": "before", "result": {"kind": "unanswerable", "reason": "no"}}]
    status, payload = handle(nq, "ask", {"question": "How many trips?", "timezone": "Asia/Tokyo", "history": history}, AUTH_A)
    assert status == 200 and payload["type"] == "answer" and payload["rows"] == [{"trips.count": 4}]
    assert planner.calls[0]["timezone"] == "Asia/Tokyo" and planner.calls[0]["history"] == history
    status, payload = handle(nq, "ask", {"question": "Trips?", "timezone": 5, "history": "x"}, AUTH_A)
    assert (status, payload) == (200, {"type": "clarify", "question": "Which?", "options": ["A"]})
    assert planner.calls[1]["timezone"] == "UTC" and "history" not in planner.calls[1]


def test_ask_without_a_planner_is_501(fleet):
    status, payload = handle(engine(fleet), "ask", {"question": "How many trips?"}, AUTH_A)
    assert status == 501 and payload["error"]["code"] == "planner_not_configured"


def test_planner_failures_and_refusals(fleet):
    reported = []
    retryable = engine(fleet, planner=ScriptedPlanner(PlannerFailedError.from_reason("rate_limited", "Try again shortly.", True)))
    status, payload = handle(retryable, "ask", {"question": "q"}, AUTH_A, on_error=reported.append)
    assert status == 503
    assert payload == {"error": {"code": "planner_failed", "message": "Try again shortly.", "reason": "rate_limited", "retryable": True}}
    assert len(reported) == 1
    fatal = engine(fleet, planner=ScriptedPlanner(PlannerFailedError.from_reason("authentication", "Bad key.")))
    assert handle(fatal, "ask", {"question": "q"}, AUTH_A, on_error=reported.append)[0] == 502
    refused = engine(fleet, planner=ScriptedPlanner(PlannerRefusalError("cyber")))
    status, payload = handle(refused, "ask", {"question": "q"}, AUTH_A, on_error=reported.append)
    assert status == 422 and payload == {"error": {"code": "planner_refused", "message": "The model declined to plan this question (cyber)."}}
    assert len(reported) == 2


def test_unexpected_errors_are_500_without_details(fleet, caplog):
    reported = []
    nq = engine(fleet, error=RuntimeError("password=hunter2 connection refused"))
    body = {"ir": {"version": 1, "measures": ["trips.count"]}}
    status, payload = handle(nq, "run", body, AUTH_A, on_error=reported.append)
    assert (status, payload) == (500, {"error": {"code": "internal", "message": "Internal error."}})
    assert str(reported[0]) == "password=hunter2 connection refused"
    with caplog.at_level("ERROR", logger="noquery"):
        assert handle(nq, "run", body, AUTH_A)[0] == 500
    assert "noquery request failed" in caplog.text


def test_to_jsonable():
    value = {
        "when": datetime.datetime(2026, 3, 1, 4, 30, tzinfo=datetime.timezone.utc),
        "day": datetime.date(2026, 3, 1),
        "time": datetime.time(4, 30),
        "span": datetime.timedelta(minutes=5),
        "id": uuid.UUID("00000000-0000-0000-0000-00000000000a"),
        "whole": decimal.Decimal("3.00"),
        "part": decimal.Decimal("3.25"),
        "bytes": b"\x01\xff",
        "nested": [(1, 2.5, None, True, "x")],
        "other": object,
        5: "non-string key",
    }
    out = to_jsonable(value)
    assert out["when"] == "2026-03-01T04:30:00+00:00" and out["day"] == "2026-03-01" and out["time"] == "04:30:00"
    assert out["span"] == 300.0 and out["id"] == "00000000-0000-0000-0000-00000000000a"
    assert out["whole"] == 3 and isinstance(out["whole"], int) and out["part"] == 3.25
    assert out["bytes"] == "01ff" and out["nested"] == [[1, 2.5, None, True, "x"]]
    assert isinstance(out["other"], str) and out["5"] == "non-string key"
