"""Validates a Query IR against the semantic model and the caller's auth context, and plans the query.

Every rule here is enforced in code; nothing depends on the model behaving.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .draft import humanize
from .ir import FILTER_OPS, TIME_GRAINS, TIME_PRESETS

AuthContext = Mapping[str, Any]
"""Who is asking, e.g. {"userId": "u1", "orgId": "acme", "roles": ["analyst"]}.

Supplied by the host app on every call; never by the model. Row scopes read their values
from here by key.
"""

QueryError = Dict[str, Any]
ResolvedQuery = Dict[str, Any]

QUERY_ERROR_CODES = (
    "invalid_ir",
    "empty_query",
    "unknown_entity",
    "unknown_field",
    "wrong_field_kind",
    "aggregate_only",
    "mixed_measure_entities",
    "no_join_path",
    "invalid_filter",
    "invalid_time",
    "invalid_timezone",
    "invalid_order_by",
    "invalid_limit",
    "missing_auth_attribute",
)

MAX_IN_VALUES = 1000

_FIELD_REF = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*")
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATE_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?(Z|[+-]\d{2}:\d{2})")

Fail = Callable[..., None]


def js_str(value: Any) -> str:
    """Formats a scalar the way the TypeScript packages do, so messages match across languages."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    return str(value)


def is_number(value: Any) -> bool:
    """A finite int or float. Booleans are not numbers here, although Python says they are."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def is_scalar(value: Any) -> bool:
    return isinstance(value, (str, bool)) or is_number(value)


def has_access(roles: Optional[Sequence[str]], auth: AuthContext) -> bool:
    if not roles:
        return True
    mine = auth.get("roles") or []
    if not isinstance(mine, (list, tuple, set, frozenset)):
        return False
    return any(r in mine for r in roles)


def is_timezone(tz: Any) -> bool:
    if not isinstance(tz, str) or not tz:
        return False
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz)
        return True
    except Exception:
        return False


def is_date_only(value: str) -> bool:
    return isinstance(value, str) and _DATE_ONLY.fullmatch(value) is not None


def _parse_time(value: Any) -> Optional[Tuple[bool, datetime]]:
    """Returns (is_date_only, comparable datetime) or None when the value is not a valid time."""
    if not isinstance(value, str):
        return None
    try:
        if _DATE_ONLY.fullmatch(value):
            d = date.fromisoformat(value)
            return True, datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        m = _DATE_TIME.fullmatch(value)
        if not m:
            return None
        year, month, day, hour, minute = (int(m.group(i)) for i in range(1, 6))
        second = int(m.group(6) or 0)
        micro = int((m.group(7) or "0").ljust(6, "0"))
        offset = m.group(8)
        if offset == "Z":
            tz = timezone.utc
        else:
            sign = 1 if offset[0] == "+" else -1
            tz = timezone(sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6])))
        return False, datetime(year, month, day, hour, minute, second, micro, tzinfo=tz)
    except ValueError:
        return None


def resolve_query(
    model: Mapping[str, Any],
    ir: Any,
    auth: AuthContext,
    default_timezone: str = "UTC",
    default_limit: int = 1000,
    max_limit: int = 10000,
) -> Dict[str, Any]:
    """Returns {"ok": True, "query": ResolvedQuery} or {"ok": False, "errors": [QueryError]}."""
    errors: List[QueryError] = []

    def fail(code: str, message: str, path: Optional[str] = None) -> None:
        errors.append({"code": code, "message": message, "path": path} if path else {"code": code, "message": message})

    if not isinstance(ir, Mapping) or ir.get("version") != 1 or isinstance(ir.get("version"), bool):
        return {"ok": False, "errors": [{"code": "invalid_ir", "message": "IR must be an object with version: 1."}]}
    for key in ("measures", "dimensions", "filters", "orderBy"):
        if ir.get(key) is not None and not isinstance(ir[key], (list, tuple)):
            return {"ok": False, "errors": [{"code": "invalid_ir", "message": f"{key} must be a list.", "path": key}]}
    if ir.get("time") is not None and not isinstance(ir["time"], Mapping):
        return {"ok": False, "errors": [{"code": "invalid_ir", "message": "time must be an object.", "path": "time"}]}

    entities: Mapping[str, Any] = model.get("entities") or {}
    ir_time: Optional[Mapping[str, Any]] = ir.get("time")

    def lookup(ref: Any, path: str) -> Optional[Dict[str, Any]]:
        if not isinstance(ref, str) or not _FIELD_REF.fullmatch(ref):
            fail("unknown_field", f'"{js_str(ref)}" is not a field reference like "entity.field".', path)
            return None
        entity_name, field_name = ref.split(".")
        entity = entities.get(entity_name)
        if not entity or not has_access(entity.get("roles"), auth):
            fail("unknown_entity", f'Unknown entity "{entity_name}".', path)
            return None
        dimension = (entity.get("dimensions") or {}).get(field_name)
        if dimension and dimension.get("policy") != "hidden" and has_access(dimension.get("roles"), auth):
            return {"kind": "dimension", "entity": entity_name, "name": field_name, "dimension": dimension}
        measure = (entity.get("measures") or {}).get(field_name)
        if measure and has_access(measure.get("roles"), auth):
            return {"kind": "measure", "entity": entity_name, "name": field_name, "measure": measure}
        fail("unknown_field", f'Unknown field "{ref}".', path)
        return None

    def groupable(hit: Optional[Dict[str, Any]], path: str) -> Optional[Dict[str, Any]]:
        if not hit:
            return None
        ref = f'{hit["entity"]}.{hit["name"]}'
        if hit["kind"] != "dimension":
            fail("wrong_field_kind", f'"{ref}" is a measure, not a dimension.', path)
            return None
        if hit["dimension"].get("policy") == "aggregate_only":
            fail("aggregate_only", f'"{ref}" can only be counted, not grouped, filtered or listed.', path)
            return None
        return hit

    # --- measures -----------------------------------------------------------------------------
    measure_hits: List[Dict[str, Any]] = []
    for i, ref in enumerate(ir.get("measures") or []):
        hit = lookup(ref, f"measures[{i}]")
        if not hit:
            continue
        if hit["kind"] != "measure":
            fail("wrong_field_kind", f'"{ref}" is a dimension, not a measure.', f"measures[{i}]")
            continue
        measure_hits.append(hit)

    # --- dimensions ---------------------------------------------------------------------------
    dimension_hits: List[Dict[str, Any]] = []
    for i, ref in enumerate(ir.get("dimensions") or []):
        hit = groupable(lookup(ref, f"dimensions[{i}]"), f"dimensions[{i}]")
        if hit:
            dimension_hits.append(hit)

    # --- time ---------------------------------------------------------------------------------
    time_hit: Optional[Dict[str, Any]] = None
    tz = (ir_time or {}).get("tz")
    tz_name = tz if tz is not None else default_timezone
    if ir_time is not None:
        time_hit = groupable(lookup(ir_time.get("field"), "time.field"), "time.field")
        if time_hit and time_hit["dimension"].get("type") != "time":
            fail("invalid_time", f'"{ir_time.get("field")}" is not a time field.', "time.field")
            time_hit = None
        grain = ir_time.get("grain")
        if grain is not None and grain not in TIME_GRAINS:
            fail("invalid_time", f'Unknown grain "{js_str(grain)}".', "time.grain")
        if ir_time.get("range") is not None:
            _check_range(ir_time["range"], fail)
        if not is_timezone(tz_name):
            fail("invalid_timezone", f'Unknown time zone "{js_str(tz_name)}".', "time.tz")

    # --- filters ------------------------------------------------------------------------------
    dimension_filters: List[Dict[str, Any]] = []
    measure_filters: List[Dict[str, Any]] = []
    for i, flt in enumerate(ir.get("filters") or []):
        path = f"filters[{i}]"
        op = flt.get("op") if isinstance(flt, Mapping) else None
        if not isinstance(op, str) or op not in FILTER_OPS:
            shown = js_str(op) if isinstance(flt, Mapping) and "op" in flt else "undefined"
            fail("invalid_filter", f'Filter {i} has an unknown op "{shown}".', path)
            continue
        hit = lookup(flt.get("field"), f"{path}.field")
        if not hit:
            continue
        if hit["kind"] == "measure":
            value = _check_measure_filter(flt, fail, path)
            if value is not None:
                measure_filters.append({"hit": hit, "filter": flt, "value": value})
            continue
        dim = groupable(hit, f"{path}.field")
        if not dim:
            continue
        if _check_dimension_filter(dim["dimension"], flt, fail, path):
            dimension_filters.append({"hit": dim, "filter": flt})

    # --- base entity --------------------------------------------------------------------------
    measure_entities = _unique(h["entity"] for h in [*measure_hits, *(f["hit"] for f in measure_filters)])
    if len(measure_entities) > 1:
        fail(
            "mixed_measure_entities",
            f'Measures must come from one entity per query; got {", ".join(measure_entities)}.',
            "measures",
        )
    base_name: Optional[str] = None
    if measure_entities:
        base_name = measure_entities[0]
    elif dimension_hits:
        base_name = dimension_hits[0]["entity"]
    elif time_hit:
        base_name = time_hit["entity"]
    if not base_name:
        if not errors:
            fail("empty_query", "Select at least one measure, dimension or time field.")
        return {"ok": False, "errors": errors}

    # --- limit --------------------------------------------------------------------------------
    limit = ir.get("limit")
    if limit is None:
        limit = default_limit
    if isinstance(limit, float) and limit.is_integer():
        limit = int(limit)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > max_limit:
        fail("invalid_limit", f"limit must be an integer from 1 to {max_limit}.", "limit")

    # --- joins --------------------------------------------------------------------------------
    needed = _unique(
        [
            *(h["entity"] for h in dimension_hits),
            *(f["hit"]["entity"] for f in dimension_filters),
            *([time_hit["entity"]] if time_hit else []),
        ]
    )
    needed = [n for n in needed if n != base_name]
    plan = plan_joins(model, base_name, needed, auth)
    for missing in plan["unreachable"]:
        why = (
            f"joining it would repeat {base_name} rows and inflate totals"
            if missing in plan["fanOut"]
            else "there is no join between them"
        )
        fail("no_join_path", f"Cannot combine {base_name} with {missing}: {why}.")

    if errors:
        return {"ok": False, "errors": errors}

    # --- assemble -----------------------------------------------------------------------------
    base = _table_ref(base_name, entities[base_name], "t0")
    aliases: Dict[str, str] = {base_name: "t0"}
    joins: List[Dict[str, Any]] = []
    for i, step in enumerate(plan["steps"]):
        alias = f"t{i + 1}"
        aliases[step["entity"]] = alias
        joins.append(
            {
                **_table_ref(step["entity"], entities[step["entity"]], alias),
                "parentAlias": aliases[step["parent"]],
                "on": step["on"],
            }
        )

    columns: List[Dict[str, Any]] = []
    keys: Dict[str, str] = {}
    grain = (ir_time or {}).get("grain")

    time: Optional[Dict[str, Any]] = None
    if time_hit and grain:
        ref = f'{time_hit["entity"]}.{time_hit["name"]}'
        key = f"{ref}__{grain}"
        time = {"key": key, "alias": aliases[time_hit["entity"]], "dimension": time_hit["dimension"], "grain": grain}
        keys[ref] = key
        columns.append(
            {
                "key": key,
                "ref": ref,
                "role": "time",
                "type": "time",
                "grain": grain,
                "label": _label_of(model, time_hit["entity"], time_hit["name"], time_hit["dimension"].get("label")),
            }
        )

    dimensions: List[Dict[str, Any]] = []
    for hit in dimension_hits:
        ref = f'{hit["entity"]}.{hit["name"]}'
        if ref in keys:
            continue
        keys[ref] = ref
        dimensions.append({"key": ref, "alias": aliases[hit["entity"]], "dimension": hit["dimension"]})
        columns.append(
            {
                "key": ref,
                "ref": ref,
                "role": "dimension",
                "type": hit["dimension"]["type"],
                "label": _label_of(model, hit["entity"], hit["name"], hit["dimension"].get("label")),
            }
        )

    measures: List[Dict[str, Any]] = []
    for hit in measure_hits:
        ref = f'{hit["entity"]}.{hit["name"]}'
        if ref in keys:
            continue
        keys[ref] = ref
        measures.append({"key": ref, "alias": aliases[hit["entity"]], "measure": hit["measure"]})
        col: Dict[str, Any] = {
            "key": ref,
            "ref": ref,
            "role": "measure",
            "type": "number",
            "label": _label_of(model, hit["entity"], hit["name"], hit["measure"].get("label")),
        }
        if hit["measure"].get("format"):
            col["format"] = hit["measure"]["format"]
        columns.append(col)

    where: List[Dict[str, Any]] = []
    for name in [base_name, *(s["entity"] for s in plan["steps"])]:
        for scope in entities[name].get("rowScope") or []:
            value = auth.get(scope["authKey"])
            is_list = isinstance(value, (list, tuple)) and len(value) > 0 and all(is_scalar(v) for v in value)
            if is_scalar(value) or is_list:
                where.append(
                    {
                        "kind": "scope",
                        "alias": aliases[name],
                        "column": scope["column"],
                        "value": list(value) if is_list else value,
                    }
                )
            else:
                fail("missing_auth_attribute", f'Access to {name} requires "{scope["authKey"]}" in the auth context.')
    time_range = (ir_time or {}).get("range")
    if time_hit and time_range:
        where.append(
            {"kind": "time_range", "alias": aliases[time_hit["entity"]], "dimension": time_hit["dimension"], "range": time_range}
        )
    for item in dimension_filters:
        hit, flt = item["hit"], item["filter"]
        cond: Dict[str, Any] = {
            "kind": "dimension",
            "alias": aliases[hit["entity"]],
            "dimension": hit["dimension"],
            "op": flt["op"],
        }
        if flt.get("value") is not None:
            cond["value"] = flt["value"]
        where.append(cond)

    having = [
        {"alias": aliases[f["hit"]["entity"]], "measure": f["hit"]["measure"], "op": f["filter"]["op"], "value": f["value"]}
        for f in measure_filters
    ]

    order_by: List[Dict[str, str]] = []
    for i, order in enumerate(ir.get("orderBy") or []):
        field = order.get("field") if isinstance(order, Mapping) else None
        key = keys.get(field) if isinstance(field, str) else None
        if not key:
            shown = js_str(field) if isinstance(order, Mapping) and "field" in order else "undefined"
            fail("invalid_order_by", f'Can only sort by a selected field; "{shown}" is not selected.', f"orderBy[{i}]")
            continue
        if not isinstance(order, Mapping) or order.get("dir") not in ("asc", "desc"):
            fail("invalid_order_by", 'Sort direction must be "asc" or "desc".', f"orderBy[{i}].dir")
            continue
        order_by.append({"key": key, "dir": order["dir"]})
    if not order_by:
        if time:
            order_by.append({"key": time["key"], "dir": "asc"})
        elif measures:
            order_by.append({"key": measures[0]["key"], "dir": "desc"})
        elif dimensions:
            order_by.append({"key": dimensions[0]["key"], "dir": "asc"})

    if errors:
        return {"ok": False, "errors": errors}

    labels: Dict[str, str] = {c["ref"]: c["label"] for c in columns}
    for item in dimension_filters:
        hit = item["hit"]
        labels.setdefault(
            f'{hit["entity"]}.{hit["name"]}', _label_of(model, hit["entity"], hit["name"], hit["dimension"].get("label"))
        )
    for item in measure_filters:
        hit = item["hit"]
        labels.setdefault(
            f'{hit["entity"]}.{hit["name"]}', _label_of(model, hit["entity"], hit["name"], hit["measure"].get("label"))
        )

    query: ResolvedQuery = {
        "ir": ir,
        "timezone": tz_name,
        "base": base,
        "joins": joins,
        "dimensions": dimensions,
        "measures": measures,
        "where": where,
        "having": having,
        "orderBy": order_by,
        "limit": limit,
        "columns": columns,
        "labels": labels,
    }
    if time:
        query["time"] = time
    return {"ok": True, "query": query}


def _unique(items: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(items))


def _table_ref(entity: str, definition: Mapping[str, Any], alias: str) -> Dict[str, Any]:
    ref: Dict[str, Any] = {"entity": entity, "table": definition["table"], "alias": alias}
    if definition.get("schema"):
        ref["schema"] = definition["schema"]
    return ref


def _label_of(model: Mapping[str, Any], entity: str, field: str, label: Optional[str] = None) -> str:
    if label:
        return label
    entity_label = (model.get("entities") or {}).get(entity, {}).get("label") or humanize(entity)
    return f"{entity_label} {humanize(field).lower()}"


def plan_joins(model: Mapping[str, Any], base: str, needed: Iterable[str], auth: AuthContext) -> Dict[str, Any]:
    """Breadth-first search over joins that cannot multiply base rows (many-to-one and one-to-one).

    Returns {"steps": [{"entity", "parent", "on"}], "unreachable": [...], "fanOut": set(...)}.
    """
    entities = model.get("entities") or {}
    safe: Dict[str, List[Dict[str, Any]]] = {}
    unsafe: Dict[str, List[str]] = {}

    def visible(name: str) -> bool:
        entity = entities.get(name)
        return bool(entity) and has_access(entity.get("roles"), auth)

    for join in model.get("joins") or []:
        if not visible(join["from"]) or not visible(join["to"]):
            continue
        forward = [list(pair) for pair in join["on"]]
        backward = [[b, a] for a, b in join["on"]]
        relationship = join.get("relationship")
        if relationship == "many_to_one":
            safe.setdefault(join["from"], []).append({"to": join["to"], "on": forward})
            unsafe.setdefault(join["to"], []).append(join["from"])
        elif relationship == "one_to_many":
            safe.setdefault(join["to"], []).append({"to": join["from"], "on": backward})
            unsafe.setdefault(join["from"], []).append(join["to"])
        else:
            safe.setdefault(join["from"], []).append({"to": join["to"], "on": forward})
            safe.setdefault(join["to"], []).append({"to": join["from"], "on": backward})

    parent: Dict[str, Dict[str, Any]] = {}
    seen: Dict[str, None] = {base: None}
    queue = [base]
    while queue:
        current = queue.pop(0)
        for edge in safe.get(current, []):
            if edge["to"] in seen:
                continue
            seen[edge["to"]] = None
            parent[edge["to"]] = {"entity": edge["to"], "parent": current, "on": edge["on"]}
            queue.append(edge["to"])

    unreachable: List[str] = []
    include: Dict[str, None] = {}
    for target in needed:
        if target not in seen:
            unreachable.append(target)
            continue
        node: Optional[str] = target
        while node and node != base:
            include[node] = None
            node = parent[node]["parent"] if node in parent else None

    # Order steps so every parent is joined before its children.
    steps: List[Dict[str, Any]] = []
    placed: Set[str] = {base}
    while len(steps) < len(include):
        for entity in include:
            step = parent[entity]
            if entity not in placed and step["parent"] in placed:
                steps.append(step)
                placed.add(entity)

    # Which unreachable targets could have been reached through a fan-out join (for a clearer error).
    fan_out: Set[str] = set()
    if unreachable:
        reachable = set(seen)
        pending = list(seen)
        while pending:
            current = pending.pop(0)
            for nxt in [*(e["to"] for e in safe.get(current, [])), *unsafe.get(current, [])]:
                if nxt not in reachable:
                    reachable.add(nxt)
                    pending.append(nxt)
        fan_out = {u for u in unreachable if u in reachable}

    return {"steps": steps, "unreachable": unreachable, "fanOut": fan_out}


def _check_range(time_range: Any, fail: Fail) -> None:
    if isinstance(time_range, str):
        if time_range not in TIME_PRESETS:
            fail("invalid_time", f'Unknown time range "{time_range}".', "time.range")
        return
    if not isinstance(time_range, Mapping):
        fail("invalid_time", "time.range must be a preset or { from, to }.", "time.range")
        return
    parsed = {}
    for side in ("from", "to"):
        value = _parse_time(time_range.get(side))
        if value is None:
            fail("invalid_time", f"time.range.{side} must be YYYY-MM-DD or ISO-8601 with an offset.", f"time.range.{side}")
            return
        parsed[side] = value
    if parsed["from"][0] == parsed["to"][0] and parsed["from"][1] >= parsed["to"][1]:
        fail("invalid_time", "time.range.from must be before time.range.to.", "time.range")


def _check_dimension_filter(dim: Mapping[str, Any], flt: Mapping[str, Any], fail: Fail, path: str) -> bool:
    op = flt["op"]
    value = flt.get("value")

    def bad(message: str) -> bool:
        fail("invalid_filter", f'{flt.get("field")}: {message}', path)
        return False

    if op in ("is_null", "is_not_null"):
        return True if value is None else bad(f'"{op}" takes no value.')
    if op in ("in", "not_in"):
        if not isinstance(value, (list, tuple)) or len(value) == 0:
            return bad(f'"{op}" needs a non-empty list.')
        if len(value) > MAX_IN_VALUES:
            return bad(f'"{op}" accepts at most {MAX_IN_VALUES} values.')
        return all(_check_scalar(dim, v, bad) for v in value)
    if isinstance(value, (list, tuple)):
        return bad(f'"{op}" takes a single value.')
    if value is None:
        return bad(f'"{op}" needs a value.')
    if op in ("contains", "starts_with"):
        if dim.get("type") != "string":
            return bad(f'"{op}" only works on text fields.')
        return True if isinstance(value, str) else bad("value must be text.")
    if op in ("gt", "gte", "lt", "lte") and dim.get("type") == "boolean":
        return bad(f'"{op}" does not apply to yes/no fields.')
    return _check_scalar(dim, value, bad)


def _check_scalar(dim: Mapping[str, Any], value: Any, bad: Callable[[str], bool]) -> bool:
    kind = dim.get("type")
    if kind == "string":
        if not isinstance(value, str):
            return bad("value must be text.")
        enum_values = dim.get("enumValues")
        if enum_values and value not in enum_values:
            listed = ", ".join(f'"{v}"' for v in enum_values)
            return bad(f'"{value}" is not one of {listed}.')
        return True
    if kind == "number":
        return True if is_number(value) else bad("value must be a number.")
    if kind == "boolean":
        return True if isinstance(value, bool) else bad("value must be true or false.")
    if kind == "time":
        return True if _parse_time(value) is not None else bad("value must be YYYY-MM-DD or ISO-8601 with an offset.")
    return bad("field has an unknown type.")


def _check_measure_filter(flt: Mapping[str, Any], fail: Fail, path: str) -> Optional[float]:
    if flt["op"] not in ("eq", "neq", "gt", "gte", "lt", "lte"):
        fail("invalid_filter", f'{flt.get("field")}: totals can only be compared with eq, neq, gt, gte, lt or lte.', path)
        return None
    value = flt.get("value")
    if not is_number(value):
        fail("invalid_filter", f'{flt.get("field")}: value must be a number.', path)
        return None
    return value
