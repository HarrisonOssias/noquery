"""Picks a default chart from the shape of a result. Rule-based so it is predictable."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

MAX_SERIES = 6
MAX_BARS = 30

ChartSpec = Dict[str, Any]


def suggest_chart(columns: Sequence[Mapping[str, Any]], rows: Optional[Sequence[Mapping[str, Any]]] = None) -> ChartSpec:
    """Returns {"type": "kpi" | "line" | "bar" | "scatter" | "table", "x"?, "y": [...], "series"?}."""
    rows = rows or []
    measures = [c for c in columns if c["role"] == "measure"]
    time = next((c for c in columns if c["role"] == "time"), None)
    dims = [c for c in columns if c["role"] == "dimension"]
    y: List[str] = [m["key"] for m in measures]
    table: ChartSpec = {"type": "table", "y": y}

    if not measures:
        return table
    if not time and not dims:
        return {"type": "kpi", "y": y} if len(measures) == 1 else table
    if time and not dims:
        return {"type": "line", "x": time["key"], "y": y}
    if time and len(dims) == 1 and len(measures) == 1:
        series = dims[0]["key"]
        distinct = len({_hashable(r.get(series)) for r in rows})
        return {"type": "line", "x": time["key"], "y": y, "series": series} if distinct <= MAX_SERIES else table
    if not time and len(dims) == 1:
        if len(measures) == 2 and len(rows) > MAX_BARS:
            return {"type": "scatter", "x": measures[0]["key"], "y": [measures[1]["key"]]}
        return {"type": "bar", "x": dims[0]["key"], "y": y} if len(rows) <= MAX_BARS else table
    return table


def _hashable(value: Any) -> Any:
    try:
        hash(value)
        return value
    except TypeError:
        return repr(value)
