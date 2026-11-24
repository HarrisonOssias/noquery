"""A plain-English sentence describing what a query actually does.

Generated from the resolved query, not by a model, so it always matches what ran.
"""

from __future__ import annotations

from typing import Any, List, Mapping

from .resolve import js_str

PRESET_TEXT = {
    "today": "today",
    "yesterday": "yesterday",
    "last_7_days": "in the last 7 days",
    "last_14_days": "in the last 14 days",
    "last_30_days": "in the last 30 days",
    "last_90_days": "in the last 90 days",
    "this_week": "this week",
    "last_week": "last week",
    "this_month": "this month",
    "last_month": "last month",
    "this_quarter": "this quarter",
    "last_quarter": "last quarter",
    "this_year": "this year",
    "last_year": "last year",
}

OP_TEXT = {
    "eq": "is",
    "neq": "is not",
    "gt": "is more than",
    "gte": "is at least",
    "lt": "is less than",
    "lte": "is at most",
    "in": "is one of",
    "not_in": "is not one of",
    "contains": "contains",
    "starts_with": "starts with",
    "is_null": "is empty",
    "is_not_null": "is not empty",
}


def explain_query(query: Mapping[str, Any]) -> str:
    ir = query["ir"]
    labels = query["labels"]
    columns = query["columns"]
    parts: List[str] = []

    measures = [c["label"] for c in columns if c["role"] == "measure"]
    dims = [_lower(c["label"]) for c in columns if c["role"] == "dimension"]
    time = next((c for c in columns if c["role"] == "time"), None)

    if measures:
        parts.append(_capitalize(_join_words([m if i == 0 else _lower(m) for i, m in enumerate(measures)])))
    else:
        parts.append(f"Distinct {_join_words(dims)}")
    if measures and dims:
        parts.append(f"by {_join_words(dims)}")
    if time:
        parts.append(f'per {time["grain"]}')

    ir_time = ir.get("time")
    time_range = ir_time.get("range") if ir_time else None
    if time_range:
        parts.append(_range_text(time_range))
    if ir_time and (time_range or time):
        parts.append(f'({query["timezone"]})')

    filters = ir.get("filters") or []
    if filters:
        clauses = []
        for f in filters:
            name = _lower(labels.get(f["field"], f["field"]))
            value = f.get("value")
            if value is None:
                text = ""
            elif isinstance(value, (list, tuple)):
                text = " " + ", ".join(js_str(v) for v in value)
            else:
                text = " " + js_str(value)
            clauses.append(f'{name} {OP_TEXT[f["op"]]}{text}')
        parts.append(f"where {_join_words(clauses)}")

    sentence = " ".join(parts) + "."
    order = query["orderBy"][0] if query["orderBy"] else None
    if order and ir.get("orderBy"):
        column = next((c for c in columns if c["key"] == order["key"]), None)
        ref = column["ref"] if column else order["key"]
        direction = "highest" if order["dir"] == "desc" else "lowest"
        sentence += f" Sorted by {_lower(labels.get(ref, ref))}, {direction} first."
    if ir.get("limit") is not None:
        sentence += f' Showing up to {query["limit"]} rows.'
    return sentence


def _range_text(time_range: Any) -> str:
    if isinstance(time_range, str):
        return PRESET_TEXT.get(time_range, time_range)
    return f'from {time_range["from"]} to {time_range["to"]}'


def _join_words(items: List[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f'{", ".join(items[:-1])} and {items[-1]}'


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


def _lower(text: str) -> str:
    return text[:1].lower() + text[1:]
