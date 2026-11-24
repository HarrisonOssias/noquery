"""Drafts a first-pass semantic model from a schema graph."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Pattern, Sequence, Union

from .semantic import SemanticModel

DEFAULT_SENSITIVE: Sequence[Pattern[str]] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"e[-_]?mail",
        r"phone",
        r"password",
        r"passwd",
        r"secret",
        r"token",
        r"api[-_]?key",
        r"ssn",
        r"sin_number",
        r"date_of_birth|^dob$|birth_?date",
        r"^first_name$|^last_name$|^full_name$",
        r"address",
        r"health_card|insurance_number",
    )
)

DEFAULT_TIME_PREFERENCE = (
    "occurred_at",
    "started_at",
    "start_time",
    "timestamp",
    "ts",
    "event_time",
    "created_at",
    "inserted_at",
    "updated_at",
)

_IDENTIFIER_LIKE = re.compile(r"(^id$|_id$|^code$|_code$|_number$|^year$|^version$)", re.IGNORECASE)


def draft_semantics(
    graph: Mapping[str, Any],
    sensitive_patterns: Optional[Iterable[Union[str, Pattern[str]]]] = None,
    exclude_tables: Optional[Iterable[str]] = None,
    time_preference: Optional[Sequence[str]] = None,
) -> SemanticModel:
    """Builds a first-pass semantic model from a schema graph.

    One entity per table, a dimension per useful column, count and numeric measures, many-to-one
    joins from foreign keys, and likely-sensitive columns hidden.

    `sensitive_patterns` are regular expressions searched (case-insensitively when given as strings)
    in column names; matching columns start out hidden. `exclude_tables` leaves tables out.
    `time_preference` lists preferred names for an entity's default time dimension, in order.
    """
    if sensitive_patterns is None:
        sensitive: Sequence[Pattern[str]] = DEFAULT_SENSITIVE
    else:
        sensitive = [re.compile(p, re.IGNORECASE) if isinstance(p, str) else p for p in sensitive_patterns]
    exclude = set(exclude_tables or [])
    preference = list(time_preference) if time_preference is not None else list(DEFAULT_TIME_PREFERENCE)

    tables = [t for t in graph.get("tables", []) if t["name"] not in exclude]
    names = _entity_names(tables)
    entities: Dict[str, Dict[str, Any]] = {}
    joins: List[Dict[str, Any]] = []

    for table in tables:
        entity_name = names[_table_key(table["name"], table.get("schema"))]
        foreign_keys = table.get("foreignKeys") or []
        fk_columns = {c for fk in foreign_keys for c in fk["columns"]}
        pk = [c for c in table["columns"] if c.get("primaryKey")]

        dimensions: Dict[str, Dict[str, Any]] = {}
        measures: Dict[str, Dict[str, Any]] = {
            "count": {"agg": "count", "label": f"Number of {humanize(entity_name).lower()}", "format": "integer"},
        }

        for column in table["columns"]:
            name = column["name"]
            is_key = bool(column.get("primaryKey")) or name in fk_columns
            is_sensitive = any(p.search(name) for p in sensitive)
            dim = _dimension_for(column)
            if dim is not None and not is_key:
                if is_sensitive:
                    dim["policy"] = "hidden"
                dimensions[name] = dim
            if not is_key and not is_sensitive and column["kind"] in ("number", "integer"):
                if not _IDENTIFIER_LIKE.search(name):
                    measures[f"total_{name}"] = {"agg": "sum", "column": name, "label": f"Total {humanize(name)}"}
                    measures[f"avg_{name}"] = {"agg": "avg", "column": name, "label": f"Average {humanize(name)}"}

        if len(pk) == 1:
            key = pk[0]
            dimensions[key["name"]] = {
                "type": "number" if key["kind"] in ("integer", "number") else "string",
                "column": key["name"],
                "policy": "aggregate_only",
                "description": "Primary key",
            }
        for fk in foreign_keys:
            if len(fk["columns"]) != 1:
                continue
            col = next((c for c in table["columns"] if c["name"] == fk["columns"][0]), None)
            if col is None or col["name"] in dimensions:
                continue
            dimensions[col["name"]] = {
                "type": "number" if col["kind"] in ("integer", "number") else "string",
                "column": col["name"],
                "policy": "aggregate_only",
                "description": f'Foreign key to {fk["referencedTable"]}',
            }

        time_dims = [n for n, d in dimensions.items() if d["type"] == "time" and d.get("policy") != "hidden"]
        default_time = next((p for p in preference if p in time_dims), time_dims[0] if time_dims else None)

        entity: Dict[str, Any] = {
            "table": table["name"],
            "label": humanize(entity_name),
            "dimensions": dimensions,
            "measures": measures,
        }
        if table.get("schema"):
            entity["schema"] = table["schema"]
        if table.get("comment"):
            entity["description"] = table["comment"]
        if default_time:
            entity["defaultTime"] = default_time
        entities[entity_name] = entity

        for fk in foreign_keys:
            target = names.get(_table_key(fk["referencedTable"], fk.get("referencedSchema") or table.get("schema")))
            if target is None:
                target = names.get(_table_key(fk["referencedTable"], None))
            if target is None:
                continue
            joins.append(
                {
                    "from": entity_name,
                    "to": target,
                    "on": [[c, fk["referencedColumns"][i]] for i, c in enumerate(fk["columns"])],
                    "relationship": "many_to_one",
                }
            )

    return {"entities": entities, "joins": joins}


def _dimension_for(column: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    kind = column["kind"]
    dim: Dict[str, Any] = {"column": column["name"], "label": humanize(column["name"])}
    if kind in ("string", "uuid"):
        dim["type"] = "string"
    elif kind == "enum":
        dim["type"] = "string"
        if column.get("enumValues"):
            dim["enumValues"] = list(column["enumValues"])
    elif kind in ("integer", "number"):
        dim["type"] = "number"
    elif kind == "boolean":
        dim["type"] = "boolean"
    elif kind == "timestamp":
        dim["type"] = "time"
        if column.get("withTimezone") is False:
            dim["timeStorage"] = "utc"
    elif kind == "date":
        dim["type"] = "time"
        dim["timeStorage"] = "local"
    else:
        return None
    if column.get("comment"):
        dim["description"] = column["comment"]
    return dim


def _table_key(name: str, schema: Optional[str]) -> str:
    return f'{schema or ""}.{name}'


def _entity_names(tables: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """Entity names are table names, prefixed with the schema only when two tables share a name."""
    counts: Dict[str, int] = {}
    for t in tables:
        counts[t["name"]] = counts.get(t["name"], 0) + 1
    out: Dict[str, str] = {}
    for t in tables:
        clash = counts[t["name"]] > 1
        raw = f'{t["schema"]}_{t["name"]}' if clash and t.get("schema") else t["name"]
        out[_table_key(t["name"], t.get("schema"))] = re.sub(r"[^A-Za-z0-9_]", "_", raw)
    return out


def humanize(name: str) -> str:
    """`started_at` -> "Started at", `deviceModel` -> "Device model"."""
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    words = re.sub(r"[_-]+", " ", words).strip().lower()
    return words[:1].upper() + words[1:]
