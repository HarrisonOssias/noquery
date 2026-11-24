"""The Query IR is the contract between the planner (an LLM or a person) and the database.

It can only name fields the semantic layer defines, as "entity.field":

    {
        "version": 1,
        "measures": ["trips.count"],
        "dimensions": ["devices.model"],
        "time": {"field": "trips.started_at", "grain": "day", "range": "last_7_days", "tz": "America/Toronto"},
        "filters": [{"field": "devices.status", "op": "eq", "value": "active"}],
        "orderBy": [{"field": "trips.count", "dir": "desc"}],
        "limit": 100,
    }

`time.range` is a preset or an absolute half-open range {"from": ..., "to": ...}, where each side is
YYYY-MM-DD (read in the query's time zone) or full ISO-8601 with an offset.
"""

from __future__ import annotations

from typing import Any, Dict

TIME_GRAINS = ("hour", "day", "week", "month", "quarter", "year")

TIME_PRESETS = (
    "today",
    "yesterday",
    "last_7_days",
    "last_14_days",
    "last_30_days",
    "last_90_days",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "this_quarter",
    "last_quarter",
    "this_year",
    "last_year",
)

FILTER_OPS = (
    "eq",
    "neq",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "contains",
    "starts_with",
    "is_null",
    "is_not_null",
)

QueryIR = Dict[str, Any]
