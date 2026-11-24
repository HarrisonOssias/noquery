"""The planner turns a question into a Query IR by asking a model for JSON that matches a schema.

A provider only has to implement one method:

    class MyProvider:
        name = "my-llm"
        def generate_json(self, system: str, messages: list, schema: dict) -> dict: ...

`messages` is [{"role": "user" | "assistant", "content": str}]. The return value must be the parsed
JSON answer. Raise `PlannerFailedError.from_reason(...)` when the model call itself fails.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .catalog import catalog_to_text
from .ir import FILTER_OPS, TIME_GRAINS, TIME_PRESETS

JsonSchema = Dict[str, Any]
PlannerResult = Dict[str, Any]
"""{"kind": "query", "ir": {...}}, {"kind": "clarify", "question", "options"} or {"kind": "unanswerable", "reason"}."""

INSTRUCTIONS = """You turn questions about a company's data into a query for an analytics engine. You never write SQL; the engine compiles and secures your query, and rejects any field that is not in the catalog below.

How to build a query:
- Use only field references that appear in the catalog, written exactly as "entity.field".
- All measures in one query must come from the same entity. Dimensions and filters may use that entity or the entities listed under "can be combined with".
- For trends or "per day/week/month" questions, set time.grain. For "last week", "this month" and similar, use time.range_preset. For explicit dates use range_from and range_to as YYYY-MM-DD; range_to is exclusive, so "in March 2026" is from 2026-03-01 to 2026-04-01.
- When a question mentions a period but no time field, use the entity's default time field.
- Filter values are always given as strings in "values": numbers as "42", yes/no as "true"/"false", dates as YYYY-MM-DD. Use "in" with several values for "any of". Use the listed values exactly when a dimension lists them.
- To filter on a total ("devices with more than 10 trips"), put a filter on the measure.
- For "top N" questions, sort by the measure descending and set limit to N.

When to do something other than query:
- kind "clarify": only when the question could reasonably mean two things that give materially different answers. Ask one short question and offer the likely options. Otherwise pick the most sensible reading.
- kind "unanswerable": when the catalog has no data for the question, or the question asks to change data. Say what is missing and name the closest field that does exist.

Set the fields that do not apply to the chosen kind to null."""

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_NUMBER = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


def to_json(value: Any) -> str:
    """Compact JSON, formatted like JavaScript's JSON.stringify."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def planner_schema(catalog: Mapping[str, Any]) -> JsonSchema:
    """Builds the JSON Schema the model's answer must match, with field names as enums."""
    entities = catalog["entities"]
    measures = [m["ref"] for e in entities for m in e["measures"]]
    dimensions = [d["ref"] for e in entities for d in e["dimensions"]]
    times = [d["ref"] for e in entities for d in e["dimensions"] if d["type"] == "time"]

    def enum_of(values: Sequence[str]) -> JsonSchema:
        return {"type": "string", "enum": list(values)} if values else {"type": "string"}

    def nullable(schema: JsonSchema) -> JsonSchema:
        return {"anyOf": [schema, {"type": "null"}]}

    def obj(properties: Dict[str, JsonSchema]) -> JsonSchema:
        return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}

    query = obj(
        {
            "measures": {"type": "array", "items": enum_of(measures)},
            "dimensions": {"type": "array", "items": enum_of(dimensions)},
            "time": nullable(
                obj(
                    {
                        "field": enum_of(times),
                        "grain": nullable({"type": "string", "enum": list(TIME_GRAINS)}),
                        "range_preset": nullable({"type": "string", "enum": list(TIME_PRESETS)}),
                        "range_from": nullable({"type": "string"}),
                        "range_to": nullable({"type": "string"}),
                    }
                )
            ),
            "filters": {
                "type": "array",
                "items": obj(
                    {
                        "field": enum_of([*dimensions, *measures]),
                        "op": {"type": "string", "enum": list(FILTER_OPS)},
                        "values": {"type": "array", "items": {"type": "string"}},
                    }
                ),
            },
            "order_by": {
                "type": "array",
                "items": obj(
                    {
                        "field": enum_of([*dimensions, *measures]),
                        "dir": {"type": "string", "enum": ["asc", "desc"]},
                    }
                ),
            },
            "limit": nullable({"type": "integer"}),
        }
    )

    return obj(
        {
            "kind": {"type": "string", "enum": ["query", "clarify", "unanswerable"]},
            "query": nullable(query),
            "clarification": nullable(
                obj({"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}})
            ),
            "reason": nullable({"type": "string"}),
        }
    )


class Planner:
    """Asks a provider for a plan. Create one with `create_planner(provider)`."""

    def __init__(
        self,
        provider: Any,
        examples: Optional[Sequence[Mapping[str, Any]]] = None,
        instructions: Optional[str] = None,
    ):
        self.provider = provider
        self.examples = list(examples or [])
        self.instructions = instructions

    def plan(
        self,
        catalog: Mapping[str, Any],
        question: str,
        timezone: str,
        now: datetime,
        history: Optional[Sequence[Mapping[str, Any]]] = None,
        repair: Optional[Mapping[str, Any]] = None,
    ) -> PlannerResult:
        """`repair` is set on a retry: {"previous": <last answer>, "errors": [QueryError]}."""
        messages: List[Dict[str, str]] = []
        for turn in history or []:
            messages.append({"role": "user", "content": str(turn["question"])})
            messages.append({"role": "assistant", "content": to_json(turn["result"])})
        messages.append({"role": "user", "content": f"{date_line(now, timezone)}\n\nQuestion: {question}"})
        if repair:
            rejected = "\n".join(f'- {e["message"]}' for e in repair["errors"])
            messages.append({"role": "assistant", "content": to_json(repair["previous"])})
            messages.append(
                {
                    "role": "user",
                    "content": f"The engine rejected that query:\n{rejected}\nReturn a corrected answer to the same question.",
                }
            )
        raw = self.provider.generate_json(
            system=self.system_prompt(catalog), messages=messages, schema=planner_schema(catalog)
        )
        return to_planner_result(raw, catalog)

    def system_prompt(self, catalog: Mapping[str, Any]) -> str:
        parts = [INSTRUCTIONS]
        if self.instructions:
            parts.append(f"Guidance for this data:\n{self.instructions}")
        parts.append(f"# Catalog\n\n{catalog_to_text(catalog)}")
        if self.examples:
            examples = "\n\n".join(f'Question: {e["question"]}\nQuery: {to_json(e["ir"])}' for e in self.examples)
            parts.append(f"# Verified examples\n\n{examples}")
        return "\n\n".join(parts)


def create_planner(
    provider: Any,
    examples: Optional[Sequence[Mapping[str, Any]]] = None,
    instructions: Optional[str] = None,
) -> Planner:
    """`examples` are verified question-to-IR pairs ([{"question", "ir"}]) shown to the model.
    `instructions` is extra domain guidance appended to the built-in instructions."""
    return Planner(provider, examples=examples, instructions=instructions)


def date_line(now: datetime, timezone: str) -> str:
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo(timezone))
    return f"Today is {_WEEKDAYS[local.weekday()]}, {local.strftime('%Y-%m-%d')} in {timezone}."


def to_planner_result(raw: Any, catalog: Mapping[str, Any]) -> PlannerResult:
    """Converts the model's answer into a PlannerResult, coercing string filter values by field type."""
    if not isinstance(raw, Mapping):
        return {"kind": "unanswerable", "reason": "The planner returned no answer."}
    if raw.get("kind") == "clarify":
        clarification = raw.get("clarification") or {}
        return {
            "kind": "clarify",
            "question": clarification.get("question") or "Could you say more about what you need?",
            "options": list(clarification.get("options") or []),
        }
    q = raw.get("query")
    if raw.get("kind") == "unanswerable" or not isinstance(q, Mapping):
        return {
            "kind": "unanswerable",
            "reason": raw.get("reason") or "That question can't be answered from the available data.",
        }

    dims = {d["ref"]: d for e in catalog["entities"] for d in e["dimensions"]}

    ir: Dict[str, Any] = {"version": 1}
    if q.get("measures"):
        ir["measures"] = list(q["measures"])
    if q.get("dimensions"):
        ir["dimensions"] = list(q["dimensions"])
    time = q.get("time")
    if isinstance(time, Mapping):
        ir["time"] = {"field": time.get("field")}
        if time.get("grain"):
            ir["time"]["grain"] = time["grain"]
        if time.get("range_preset"):
            ir["time"]["range"] = time["range_preset"]
        elif time.get("range_from") and time.get("range_to"):
            ir["time"]["range"] = {"from": time["range_from"], "to": time["range_to"]}
    if q.get("filters"):
        filters = []
        for f in q["filters"]:
            dim = dims.get(f.get("field"))
            values = [str(v) for v in (f.get("values") or [])]
            op = f.get("op")
            if op in ("is_null", "is_not_null"):
                filters.append({"field": f.get("field"), "op": op})
            elif op in ("in", "not_in"):
                filters.append({"field": f.get("field"), "op": op, "value": [_coerce(v, dim) for v in values]})
            else:
                filters.append({"field": f.get("field"), "op": op, "value": _coerce(values[0] if values else "", dim)})
        ir["filters"] = filters
    if q.get("order_by"):
        ir["orderBy"] = [{"field": o.get("field"), "dir": o.get("dir")} for o in q["order_by"]]
    limit = q.get("limit")
    if isinstance(limit, (int, float)) and not isinstance(limit, bool):
        ir["limit"] = int(limit) if float(limit).is_integer() else limit
    return {"kind": "query", "ir": ir}


def _coerce(value: str, dim: Optional[Mapping[str, Any]]) -> Any:
    if dim is None:  # a measure: totals are numbers
        return _to_number(value)
    if dim["type"] == "number":
        return _to_number(value)
    if dim["type"] == "boolean":
        return True if value == "true" else False if value == "false" else value
    return value


def _to_number(value: str) -> Any:
    text = value.strip()
    if not _NUMBER.fullmatch(text):
        return value
    number = float(text)
    if not math.isfinite(number):
        return value
    return int(number) if number.is_integer() and abs(number) < 2**53 else number
