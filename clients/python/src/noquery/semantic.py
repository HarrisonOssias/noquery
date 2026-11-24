"""The semantic layer says what the data means: entities, dimensions, measures, joins and policies.

It is drafted from a schema graph (see `draft_semantics`) and curated by people. Models and overlays
are plain dicts with the same keys as the TypeScript packages, so one model works in both:

    {
        "entities": {
            "trips": {
                "table": "trips", "schema": "public", "label": "Trip", "description": "...",
                "synonyms": ["journeys"], "defaultTime": "started_at", "roles": ["analyst"],
                "rowScope": [{"column": "org_id", "authKey": "orgId"}],
                "dimensions": {
                    "status": {"type": "string", "column": "status", "enumValues": ["planned", "done"],
                               "policy": "visible", "label": "...", "timeStorage": "instant"},
                },
                "measures": {
                    "count": {"agg": "count", "label": "Trips", "format": "integer"},
                    "avg_minutes": {"agg": "avg", "sql": "extract(epoch from ${TABLE}.ended_at - ${TABLE}.started_at) / 60"},
                },
            },
        },
        "joins": [{"from": "trips", "to": "devices", "on": [["device_id", "id"]], "relationship": "many_to_one"}],
    }

Dimension types: "string", "number", "boolean", "time".
Field policies: "visible" (default), "hidden" (invisible to the planner and rejected by the validator)
and "aggregate_only" (may feed measures, never grouped, filtered or listed).
Time storage: "instant" (default, e.g. timestamptz), "utc" (timestamp without time zone holding UTC)
and "local" (a wall-clock value read in the query's time zone, e.g. a date column).
Aggregations: "count", "count_distinct", "sum", "avg", "min", "max". Omit `agg` only when `sql`
is itself an aggregate expression. `sql` is trusted: it comes from your code, never from a caller.
Relationships: "many_to_one", "one_to_one", "one_to_many". Joins are only followed in directions
that cannot multiply rows.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Mapping, Optional

SemanticModel = Dict[str, Any]
SemanticOverlay = Mapping[str, Any]

DIMENSION_TYPES = ("string", "number", "boolean", "time")
FIELD_POLICIES = ("visible", "hidden", "aggregate_only")
TIME_STORAGE = ("instant", "utc", "local")
AGGREGATIONS = ("count", "count_distinct", "sum", "avg", "min", "max")
RELATIONSHIPS = ("many_to_one", "one_to_one", "one_to_many")


def merge_semantics(base: Mapping[str, Any], overlay: SemanticOverlay) -> SemanticModel:
    """Applies a curated overlay on top of a (usually auto-drafted) model.

    Fields merge shallowly; `None` removes an entity, dimension or measure. The overlay may also
    carry `joins` to add and `removeJoins` ([{"from", "to"}]) to drop.
    """
    entities: Dict[str, Dict[str, Any]] = {}
    for name, entity in base.get("entities", {}).items():
        entities[name] = {
            **entity,
            "dimensions": dict(entity.get("dimensions") or {}),
            "measures": dict(entity.get("measures") or {}),
        }

    for name, patch in (overlay.get("entities") or {}).items():
        if patch is None:
            entities.pop(name, None)
            continue
        rest = {k: v for k, v in patch.items() if k not in ("dimensions", "measures") and v is not None}
        current = entities.get(name)
        if current is None and not rest.get("table"):
            raise ValueError(f'Overlay adds entity "{name}" without a table.')
        merged: Dict[str, Any] = {**(current or {"table": rest["table"], "dimensions": {}, "measures": {}}), **rest}

        def new_dimension(value: Dict[str, Any], _name: str = name) -> Dict[str, Any]:
            if not value.get("type"):
                raise ValueError(f'New dimension in "{_name}" needs a type.')
            return value

        merged["dimensions"] = _merge_record((current or {}).get("dimensions") or {}, patch.get("dimensions"), new_dimension)
        merged["measures"] = _merge_record((current or {}).get("measures") or {}, patch.get("measures"), lambda m: m)
        entities[name] = merged

    removed = {f'{j["from"]}->{j["to"]}' for j in (overlay.get("removeJoins") or [])}
    joins: Dict[str, Dict[str, Any]] = {}
    for join in [*(base.get("joins") or []), *(overlay.get("joins") or [])]:
        key = f'{join["from"]}->{join["to"]}'
        if key in removed:
            continue
        joins[key] = join
    kept = [j for j in joins.values() if j["from"] in entities and j["to"] in entities]
    return {"entities": entities, "joins": kept}


def _merge_record(
    base: Mapping[str, Any],
    patch: Optional[Mapping[str, Any]],
    create: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (patch or {}).items():
        if value is None:
            out.pop(key, None)
            continue
        clean = {k: v for k, v in value.items() if v is not None}
        if out.get(key):
            out[key] = {**out[key], **clean}
        else:
            out[key] = create(clean)
    return out


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def check_semantics(model: Mapping[str, Any]) -> List[Dict[str, str]]:
    """Static checks on a semantic model. Returns [] when the model is sound, else [{"path", "message"}]."""
    issues: List[Dict[str, str]] = []

    def issue(path: str, message: str) -> None:
        issues.append({"path": path, "message": message})

    entities = model.get("entities") or {}
    for entity_name, entity in entities.items():
        path = f"entities.{entity_name}"
        if not _NAME.fullmatch(str(entity_name)):
            issue(path, "Entity names must be identifiers.")
        if not entity.get("table"):
            issue(path, "Entity has no table.")
        dimensions = entity.get("dimensions") or {}
        measures = entity.get("measures") or {}
        for dim_name, dim in dimensions.items():
            p = f"{path}.dimensions.{dim_name}"
            if not _NAME.fullmatch(str(dim_name)):
                issue(p, "Field names must be identifiers.")
            if not dim.get("column") and not dim.get("sql"):
                issue(p, "Dimension needs a column or sql.")
            if dim.get("type") not in DIMENSION_TYPES:
                issue(p, f'Dimension type must be one of {", ".join(DIMENSION_TYPES)}.')
        for measure_name, measure in measures.items():
            p = f"{path}.measures.{measure_name}"
            if not _NAME.fullmatch(str(measure_name)):
                issue(p, "Field names must be identifiers.")
            if measure_name in dimensions:
                issue(p, "A measure and a dimension share this name.")
            agg = measure.get("agg")
            if not agg and not measure.get("sql"):
                issue(p, "Measure needs an agg, or sql that is an aggregate.")
            if agg and agg not in AGGREGATIONS:
                issue(p, f'Unknown agg "{agg}".')
            elif agg and agg != "count" and not measure.get("column") and not measure.get("sql"):
                issue(p, f'"{agg}" needs a column or sql.')
        default_time = entity.get("defaultTime")
        if default_time:
            dim = dimensions.get(default_time)
            if not dim or dim.get("type") != "time":
                issue(f"{path}.defaultTime", "defaultTime must name a time dimension.")
    for i, join in enumerate(model.get("joins") or []):
        p = f"joins[{i}]"
        if join.get("from") not in entities:
            issue(p, f'Unknown entity "{join.get("from")}".')
        if join.get("to") not in entities:
            issue(p, f'Unknown entity "{join.get("to")}".')
        if len(join.get("on") or []) == 0:
            issue(p, "Join needs at least one column pair.")
        if join.get("relationship") not in RELATIONSHIPS:
            issue(p, f'Relationship must be one of {", ".join(RELATIONSHIPS)}.')
    return issues
