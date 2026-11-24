"""PostgreSQL support: the SQL dialect and catalog introspection."""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .resolve import is_date_only
from .runtime import CompiledQuery
from .schema import SchemaGraph

GRAIN_FORMAT = {
    "hour": "'YYYY-MM-DD\"T\"HH24:00'",
    "day": "'YYYY-MM-DD'",
    "week": "'YYYY-MM-DD'",
    "month": "'YYYY-MM-DD'",
    "quarter": "'YYYY-MM-DD'",
    "year": "'YYYY-MM-DD'",
}

PRESET_UNIT = {
    "week": ("week", "1 week"),
    "month": ("month", "1 month"),
    "quarter": ("quarter", "3 months"),
    "year": ("year", "1 year"),
}

COMPARE = {"eq": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
AGG_SQL = {"sum": "sum", "avg": "avg", "min": "min", "max": "max"}
PARAMSTYLES = ("pyformat", "numeric")

_LAST_DAYS = re.compile(r"last_(\d+)_days")


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


class PostgresDialect:
    """Compiles resolved queries to PostgreSQL. Create one with `postgres_dialect()`."""

    name = "postgres"

    def __init__(self, paramstyle: str = "pyformat", probe_truncation: bool = True):
        if paramstyle not in PARAMSTYLES:
            raise ValueError(f'paramstyle must be one of {", ".join(PARAMSTYLES)}.')
        self.paramstyle = paramstyle
        self.probe_truncation = probe_truncation

    def compile(self, query: Mapping[str, Any]) -> CompiledQuery:
        pyformat = self.paramstyle == "pyformat"
        params: List[Any] = []
        tz_ref: List[str] = []

        def param(value: Any) -> str:
            params.append(value)
            return f"%(p{len(params)})s" if pyformat else f"${len(params)}"

        def literal(sql: str) -> str:
            # With "pyformat" the driver reads % as a placeholder marker, so literal ones are doubled.
            return sql.replace("%", "%%") if pyformat else sql

        def ident(name: str) -> str:
            return literal(quote_ident(name))

        def tz() -> str:
            if not tz_ref:
                tz_ref.append(f'{param(query["timezone"])}::text')
            return tz_ref[0]

        def column(alias: str, name: str) -> str:
            return f"{ident(alias)}.{ident(name)}"

        def field_expr(alias: str, field: Mapping[str, Any]) -> str:
            if field.get("sql"):
                return "(" + literal(field["sql"]).replace("${TABLE}", ident(alias)) + ")"
            return column(alias, field["column"])

        def instant(alias: str, dim: Mapping[str, Any]) -> str:
            """The field as an absolute instant (timestamptz)."""
            e = field_expr(alias, dim)
            storage = dim.get("timeStorage") or "instant"
            if storage == "utc":
                return f"({e}::timestamp AT TIME ZONE 'UTC')"
            if storage == "local":
                return f"({e}::timestamp AT TIME ZONE {tz()})"
            return e

        def instant_value(value: str) -> str:
            """A user-supplied time value as an instant: dates are read in the query's time zone."""
            if is_date_only(value):
                p = param(value)
                return f"({p}::timestamp AT TIME ZONE {tz()})"
            return f"{param(value)}::timestamptz"

        def measure_expr(alias: str, m: Mapping[str, Any]) -> str:
            target = field_expr(alias, m) if (m.get("column") or m.get("sql")) else None
            agg = m.get("agg")
            if not agg:
                return field_expr(alias, m)
            if agg == "count":
                return f"count({target})" if target else "count(*)"
            if agg == "count_distinct":
                return f"count(DISTINCT {target})"
            if agg not in AGG_SQL:
                raise ValueError(f'Unsupported aggregation "{agg}".')
            return f"{AGG_SQL[agg]}({target})"

        def table(t: Mapping[str, Any]) -> str:
            name = f'{ident(t["schema"])}.{ident(t["table"])}' if t.get("schema") else ident(t["table"])
            return f'{name} AS {ident(t["alias"])}'

        def range_bounds(time_range: Any) -> Tuple[str, str]:
            if not isinstance(time_range, str):
                lo = instant_value(time_range["from"])
                hi = instant_value(time_range["to"])
                return lo, hi
            now_local = f"(now() AT TIME ZONE {tz()})"
            day = f"date_trunc('day', {now_local})"

            def at(local: str) -> str:
                return f"(({local}) AT TIME ZONE {tz()})"

            last_days = _LAST_DAYS.fullmatch(time_range)
            if last_days:
                n = int(last_days.group(1)) - 1
                return at(f"{day} - interval '{n} days'"), at(f"{day} + interval '1 day'")
            if time_range == "today":
                return at(day), at(f"{day} + interval '1 day'")
            if time_range == "yesterday":
                return at(f"{day} - interval '1 day'"), at(day)
            which, _, unit_name = time_range.partition("_")
            if unit_name not in PRESET_UNIT or which not in ("this", "last"):
                raise ValueError(f'Unsupported time range "{time_range}".')
            unit, step = PRESET_UNIT[unit_name]
            start = f"date_trunc('{unit}', {now_local})"
            if which == "this":
                return at(start), at(f"{start} + interval '{step}'")
            return at(f"{start} - interval '{step}'"), at(start)

        def any_of(expr: str, values: Sequence[Any]) -> str:
            # psycopg2 sends a list as an array of text, which Postgres will not compare with uuid
            # or enum columns. One placeholder per value lets the server infer each type instead.
            if pyformat:
                return f'{expr} IN ({", ".join(param(x) for x in values)})'
            return f"{expr} = ANY({param(list(values))})"

        def condition(c: Mapping[str, Any]) -> str:
            if c["kind"] == "scope":
                col = column(c["alias"], c["column"])
                if isinstance(c["value"], (list, tuple)):
                    return any_of(col, c["value"])
                return f'{col} = {param(c["value"])}'
            if c["kind"] == "time_range":
                lo, hi = range_bounds(c["range"])
                ts = instant(c["alias"], c["dimension"])
                return f"{ts} >= {lo} AND {ts} < {hi}"
            dim = c["dimension"]
            is_time = dim.get("type") == "time"
            e = instant(c["alias"], dim) if is_time else field_expr(c["alias"], dim)

            def v(value: Any) -> str:
                return instant_value(str(value)) if is_time else param(value)

            op = c["op"]
            value = c.get("value")
            if op in COMPARE:
                return f"{e} {COMPARE[op]} {v(value)}"
            if op == "neq":
                return f"{e} IS DISTINCT FROM {v(value)}"
            if op == "in":
                if is_time:
                    return f'{e} IN ({", ".join(v(x) for x in value)})'
                return any_of(e, value)
            if op == "not_in":
                if is_time:
                    return f'({e} IS NULL OR {e} NOT IN ({", ".join(v(x) for x in value)}))'
                return f"({e} IS NULL OR NOT ({any_of(e, value)}))"
            if op == "contains":
                return f'{e} ILIKE {param("%" + _escape_like(str(value)) + "%")}'
            if op == "starts_with":
                return f'{e} ILIKE {param(_escape_like(str(value)) + "%")}'
            if op == "is_null":
                return f"{e} IS NULL"
            if op == "is_not_null":
                return f"{e} IS NOT NULL"
            raise ValueError(f'Unsupported filter op "{op}".')

        select: List[str] = []
        group_count = 0
        time = query.get("time")
        if time:
            if time["grain"] not in GRAIN_FORMAT:
                raise ValueError(f'Unsupported grain "{time["grain"]}".')
            ts = instant(time["alias"], time["dimension"])
            local = f"({ts} AT TIME ZONE {tz()})"
            fmt = GRAIN_FORMAT[time["grain"]]
            select.append(f'to_char(date_trunc(\'{time["grain"]}\', {local}), {fmt}) AS {ident(time["key"])}')
            group_count += 1
        for d in query["dimensions"]:
            select.append(f'{field_expr(d["alias"], d["dimension"])} AS {ident(d["key"])}')
            group_count += 1
        for m in query["measures"]:
            select.append(f'{measure_expr(m["alias"], m["measure"])} AS {ident(m["key"])}')

        from_parts = [f'FROM {table(query["base"])}']
        for j in query["joins"]:
            on = " AND ".join(f'{column(j["parentAlias"], p)} = {column(j["alias"], c)}' for p, c in j["on"])
            from_parts.append(f"LEFT JOIN {table(j)} ON {on}")

        where = [condition(c) for c in query["where"]]
        having = []
        for h in query["having"]:
            op = "<>" if h["op"] == "neq" else COMPARE[h["op"]]
            having.append(f'{measure_expr(h["alias"], h["measure"])} {op} {param(h["value"])}')

        parts = [f'SELECT {", ".join(select)}', *from_parts]
        if where:
            parts.append(f'WHERE {" AND ".join(where)}')
        if group_count > 0:
            parts.append(f'GROUP BY {", ".join(str(i + 1) for i in range(group_count))}')
        if having:
            parts.append(f'HAVING {" AND ".join(having)}')
        if query["orderBy"]:
            order = ", ".join(
                f'{ident(o["key"])} {"DESC" if o["dir"] == "desc" else "ASC"} NULLS LAST' for o in query["orderBy"]
            )
            parts.append(f"ORDER BY {order}")
        parts.append(f'LIMIT {int(query["limit"]) + (1 if self.probe_truncation else 0)}')

        return CompiledQuery(sql="\n".join(parts), params=params, paramstyle=self.paramstyle)


def postgres_dialect(paramstyle: str = "pyformat", probe_truncation: bool = True) -> PostgresDialect:
    """PostgreSQL dialect.

    Every value is a bound parameter; identifiers come from the semantic model and are always
    quoted. Time buckets come back as local wall-clock strings (`YYYY-MM-DD`, or `YYYY-MM-DDTHH:00`
    for hours) in the query's time zone.

    Args:
        paramstyle: "pyformat" (default; `%(p1)s`, for psycopg, psycopg2, Django and SQLAlchemy)
            or "numeric" (`$1`, for asyncpg; identical to the TypeScript packages' SQL).
        probe_truncation: Fetch one row more than the query's limit so callers can tell the result
            was cut off. The executor slices the extra row away.
    """
    return PostgresDialect(paramstyle=paramstyle, probe_truncation=probe_truncation)


def _escape_like(value: str) -> str:
    return re.sub(r"[\\%_]", lambda m: "\\" + m.group(0), value)


# --- catalog introspection --------------------------------------------------------------------

CatalogQuery = Callable[[str, Dict[str, Any]], Sequence[Mapping[str, Any]]]
"""Runs a catalog query with "pyformat" parameters and returns rows as mappings keyed by column name."""

COLUMNS_SQL = """
SELECT c.table_schema, c.table_name, c.column_name, c.is_nullable, c.data_type, c.udt_name, c.udt_schema,
       t.table_type,
       col_description(format('%%I.%%I', c.table_schema, c.table_name)::regclass::oid, c.ordinal_position) AS column_comment,
       obj_description(format('%%I.%%I', c.table_schema, c.table_name)::regclass::oid, 'pg_class') AS table_comment
FROM information_schema.columns c
JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = ANY(%(schemas)s::text[]) AND t.table_type = ANY(%(table_types)s::text[])
ORDER BY c.table_schema, c.table_name, c.ordinal_position"""

CONSTRAINTS_SQL = """
SELECT n.nspname AS table_schema, cl.relname AS table_name, con.contype::text AS contype,
       array(SELECT a.attname::text FROM unnest(con.conkey) WITH ORDINALITY k(num, ord)
             JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.num ORDER BY k.ord) AS columns,
       fn.nspname AS ref_schema, fcl.relname AS ref_table,
       array(SELECT a.attname::text FROM unnest(con.confkey) WITH ORDINALITY k(num, ord)
             JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.num ORDER BY k.ord) AS ref_columns
FROM pg_constraint con
JOIN pg_class cl ON cl.oid = con.conrelid
JOIN pg_namespace n ON n.oid = cl.relnamespace
LEFT JOIN pg_class fcl ON fcl.oid = con.confrelid
LEFT JOIN pg_namespace fn ON fn.oid = fcl.relnamespace
WHERE con.contype IN ('p', 'f') AND n.nspname = ANY(%(schemas)s::text[])
ORDER BY con.conname"""

ENUMS_SQL = """
SELECT n.nspname AS enum_schema, t.typname AS enum_name,
       array_agg(e.enumlabel::text ORDER BY e.enumsortorder) AS labels
FROM pg_type t
JOIN pg_enum e ON e.enumtypid = t.oid
JOIN pg_namespace n ON n.oid = t.typnamespace
GROUP BY 1, 2"""

_KIND_BY_DATA_TYPE = {
    "smallint": "integer",
    "integer": "integer",
    "bigint": "integer",
    "numeric": "number",
    "real": "number",
    "double precision": "number",
    "money": "number",
    "boolean": "boolean",
    "text": "string",
    "character varying": "string",
    "character": "string",
    "citext": "string",
    "uuid": "uuid",
    "timestamp with time zone": "timestamp",
    "timestamp without time zone": "timestamp",
    "date": "date",
    "json": "json",
    "jsonb": "json",
}


def introspect_postgres(
    query: CatalogQuery,
    schemas: Sequence[str] = ("public",),
    include_views: bool = True,
) -> SchemaGraph:
    """Reads tables, views, columns, keys, enums and comments from the Postgres catalog.

    `query(sql, params)` runs one statement with "pyformat" parameters and returns rows as dicts.
    `noquery.executors.dbapi_catalog_query(connect)` builds one from a DB-API connection factory.
    """
    schema_list = list(schemas)
    table_types = ["BASE TABLE", "VIEW"] if include_views else ["BASE TABLE"]
    columns = query(COLUMNS_SQL, {"schemas": schema_list, "table_types": table_types})
    constraints = query(CONSTRAINTS_SQL, {"schemas": schema_list})
    enums = query(ENUMS_SQL, {})

    enum_values = {f'{e["enum_schema"]}.{e["enum_name"]}': _to_list(e["labels"]) for e in enums}

    tables: Dict[str, Dict[str, Any]] = {}
    for c in columns:
        key = f'{c["table_schema"]}.{c["table_name"]}'
        table = tables.get(key)
        if table is None:
            table = {"name": str(c["table_name"]), "schema": str(c["table_schema"]), "columns": [], "foreignKeys": []}
            if c.get("table_comment"):
                table["comment"] = str(c["table_comment"])
            tables[key] = table
        data_type = str(c["data_type"])
        enum_key = f'{c["udt_schema"]}.{c["udt_name"]}'
        kind = "enum" if enum_key in enum_values else _KIND_BY_DATA_TYPE.get(data_type, "unknown")
        column: Dict[str, Any] = {
            "name": str(c["column_name"]),
            "kind": kind,
            "dbType": str(c["udt_name"]) if data_type == "USER-DEFINED" else data_type,
            "nullable": c["is_nullable"] == "YES",
        }
        if kind == "enum":
            column["enumValues"] = enum_values[enum_key]
        if data_type == "timestamp with time zone":
            column["withTimezone"] = True
        if data_type == "timestamp without time zone":
            column["withTimezone"] = False
        if c.get("column_comment"):
            column["comment"] = str(c["column_comment"])
        table["columns"].append(column)

    for con in constraints:
        table = tables.get(f'{con["table_schema"]}.{con["table_name"]}')
        if table is None:
            continue
        cols = _to_list(con["columns"])
        if con["contype"] == "p":
            for col in table["columns"]:
                if col["name"] in cols:
                    col["primaryKey"] = True
        else:
            table["foreignKeys"].append(
                {
                    "columns": cols,
                    "referencedTable": str(con["ref_table"]),
                    "referencedSchema": str(con["ref_schema"]),
                    "referencedColumns": _to_list(con["ref_columns"]),
                }
            )

    return {"tables": list(tables.values())}


def _to_list(value: Any) -> List[str]:
    """Some drivers return Postgres arrays as `{a,b}` strings."""
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
        inner = value[1:-1]
        return [] if inner == "" else [s.strip('"') for s in inner.split(",")]
    return []
