"""The Python engine must produce exactly what the TypeScript engine produces.

`fixtures/parity.json` is written by packages/postgres/test/parity.test.ts. To regenerate it after
an intended behavior change: NOQUERY_UPDATE_PARITY=1 pnpm test
"""

from datetime import datetime, timezone

import pytest

from noquery import (
    build_catalog,
    catalog_to_text,
    create_planner,
    draft_semantics,
    explain_query,
    merge_semantics,
    planner_schema,
    postgres_dialect,
    resolve_query,
    suggest_chart,
    to_planner_result,
)

from conftest import load_parity

PARITY = load_parity()

OPTION_NAMES = {"defaultTimezone": "default_timezone", "defaultLimit": "default_limit", "maxLimit": "max_limit"}


def resolve(case):
    options = {OPTION_NAMES[k]: v for k, v in case["options"].items()}
    return resolve_query(PARITY["model"], case["ir"], case["auth"], **options)


@pytest.mark.parametrize("case", [c for c in PARITY["queries"] if c["ok"]], ids=lambda c: c["name"])
def test_accepted_query_matches_typescript(case):
    resolved = resolve(case)
    assert resolved["ok"], resolved.get("errors")
    query = resolved["query"]
    compiled = postgres_dialect(paramstyle="numeric").compile(query)
    assert compiled.sql == case["sql"]
    assert compiled.params == case["params"]
    assert postgres_dialect(paramstyle="numeric", probe_truncation=False).compile(query).sql == case["sqlNoProbe"]
    assert query["columns"] == case["columns"]
    assert explain_query(query) == case["explanation"]
    assert suggest_chart(query["columns"]) == case["chart"]


@pytest.mark.parametrize("case", [c for c in PARITY["queries"] if not c["ok"]], ids=lambda c: c["name"])
def test_rejected_query_matches_typescript(case):
    resolved = resolve(case)
    assert not resolved["ok"]
    assert resolved["errors"] == case["errors"]


@pytest.mark.parametrize("case", [c for c in PARITY["queries"] if c["ok"]], ids=lambda c: c["name"])
def test_pyformat_sql_is_the_same_query(case):
    """The default paramstyle differs from the TypeScript SQL in how placeholders are written, and in
    binding each list value separately (`IN (...)`) where TypeScript binds one array (`= ANY(...)`)."""
    compiled = postgres_dialect().compile(resolve(case)["query"])
    flat = [v for p in case["params"] for v in (p if isinstance(p, list) else [p])]
    assert compiled.params == flat
    assert set(compiled.named_params()) == {f"p{i + 1}" for i in range(len(flat))}
    assert "ANY(" not in compiled.sql
    assert "$" not in compiled.sql.replace("${", "")
    if not any(isinstance(p, list) for p in case["params"]):
        sql = compiled.sql
        for i in range(len(compiled.params), 0, -1):
            sql = sql.replace(f"%(p{i})s", f"${i}")
        assert sql.replace("%%", "%") == case["sql"]


def test_pyformat_binds_list_values_one_by_one():
    case = next(c for c in PARITY["queries"] if c["name"] == "list-valued row scope")
    compiled = postgres_dialect().compile(resolve(case)["query"])
    assert '"t0"."org_id" IN (%(p1)s, %(p2)s)' in compiled.sql
    assert '"t1"."org_id" IN (%(p3)s, %(p4)s)' in compiled.sql
    case = next(c for c in PARITY["queries"] if c["name"] == "every dimension filter op")
    compiled = postgres_dialect().compile(resolve(case)["query"])
    assert '("t1"."model" IS NULL OR NOT ("t1"."model" IN (%(p' in compiled.sql


@pytest.mark.parametrize("case", PARITY["catalogs"], ids=lambda c: c["auth"].get("userId", "anonymous"))
def test_catalog_matches_typescript(case):
    catalog = build_catalog(PARITY["model"], case["auth"])
    assert catalog == case["catalog"]
    assert catalog_to_text(catalog) == case["text"]
    assert planner_schema(catalog) == case["schema"]


def test_draft_matches_typescript():
    draft = PARITY["draft"]
    assert draft_semantics(draft["graph"]) == draft["model"]
    assert draft_semantics(draft["graph"], exclude_tables=["trips"], time_preference=["archived_at"]) == draft["excluded"]


def test_draft_keeps_field_order():
    draft = PARITY["draft"]
    drafted = draft_semantics(draft["graph"])
    for name, entity in draft["model"]["entities"].items():
        assert list(drafted["entities"][name]["dimensions"]) == list(entity["dimensions"])
        assert list(drafted["entities"][name]["measures"]) == list(entity["measures"])


def test_merge_matches_typescript():
    merged = merge_semantics(PARITY["draft"]["model"], PARITY["merge"]["overlay"])
    assert merged == PARITY["merge"]["model"]


@pytest.mark.parametrize("case", PARITY["planner"]["results"], ids=lambda c: str(PARITY["planner"]["results"].index(c)))
def test_planner_result_matches_typescript(case):
    catalog = build_catalog(PARITY["model"], PARITY["planner"]["auth"])
    assert to_planner_result(case["raw"], catalog) == case["result"]


class Recorder:
    name = "recorder"

    def __init__(self):
        self.requests = []

    def generate_json(self, system, messages, schema):
        self.requests.append({"system": system, "messages": messages, "schema": schema})
        return {"kind": "unanswerable", "query": None, "clarification": None, "reason": "recorded"}


def test_planner_prompts_match_typescript():
    planner = PARITY["planner"]
    catalog = build_catalog(PARITY["model"], planner["auth"])
    now = datetime(2026, 3, 1, 4, 30, tzinfo=timezone.utc)
    assert planner["now"] == "2026-03-01T04:30:00.000Z"
    provider = Recorder()
    first = {"version": 1, "measures": ["trips.count"], "dimensions": ["organizations.region"]}

    create_planner(provider).plan(catalog=catalog, question="How many trips today?", timezone="America/Toronto", now=now)
    create_planner(
        provider,
        instructions="A trip is one continuous movement.",
        examples=[{"question": "Trips by region", "ir": first}],
    ).plan(
        catalog=catalog,
        question="Now only the east region",
        timezone="Asia/Tokyo",
        now=now,
        history=[
            {"question": "Trips by region", "result": {"kind": "query", "ir": first}},
            {"question": "And battery?", "result": {"kind": "unanswerable", "reason": "No battery data."}},
        ],
        repair={
            "previous": {"kind": "query", "ir": {"version": 1, "measures": ["trips.nope"]}},
            "errors": [{"code": "unknown_field", "message": 'Unknown field "trips.nope".', "path": "measures[0]"}],
        },
    )
    assert provider.requests == planner["requests"]
