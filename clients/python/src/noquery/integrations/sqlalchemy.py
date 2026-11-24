"""SQLAlchemy integration: read your models into a schema graph and run queries on your Engine.

Install with `pip install "noquery[sqlalchemy]"`. Works with SQLAlchemy 2.x and the
psycopg or psycopg2 driver.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import sqlalchemy as sa
from sqlalchemy import types as sat
from sqlalchemy.dialects import postgresql

from ..executors import READ_ONLY_SQL, require_pyformat, timeout_statement
from ..runtime import CompiledQuery, ExecuteResult, finish
from ..schema import SchemaGraph

_UUID_TYPES = tuple(t for t in (getattr(sat, "Uuid", None), postgresql.UUID) if t is not None)
_PG_DIALECT = postgresql.dialect()


def introspect_sqlalchemy(
    source: Any,
    include: Optional[Iterable[str]] = None,
    exclude: Optional[Iterable[str]] = None,
) -> SchemaGraph:
    """Reads SQLAlchemy table definitions into a schema graph.

    `source` is a declarative base class (`Base`), a `MetaData`, or a list of `Table` objects and
    mapped classes. To read the live database instead of your models, reflect it first:

        metadata = MetaData()
        metadata.reflect(engine)
        graph = introspect_sqlalchemy(metadata)

    `include` keeps only the named tables; `exclude` leaves the named tables out.
    """
    keep = set(include) if include is not None else None
    drop = set(exclude or [])
    tables: List[Dict[str, Any]] = []
    for table in _tables_of(source):
        if keep is not None and table.name not in keep:
            continue
        if table.name in drop:
            continue
        columns: List[Dict[str, Any]] = []
        for column in table.columns:
            kind, enum_values, with_timezone = _kind_of(column.type)
            info: Dict[str, Any] = {
                "name": column.name,
                "kind": kind,
                "dbType": _db_type(column.type),
                "nullable": bool(column.nullable),
            }
            if column.primary_key:
                info["primaryKey"] = True
            if enum_values:
                info["enumValues"] = enum_values
            if with_timezone is not None:
                info["withTimezone"] = with_timezone
            if column.comment:
                info["comment"] = column.comment
            columns.append(info)

        foreign_keys: List[Dict[str, Any]] = []
        for constraint in sorted(table.foreign_key_constraints, key=lambda c: [col.name for col in c.columns]):
            target = constraint.referred_table
            fk: Dict[str, Any] = {
                "columns": [c.name for c in constraint.columns],
                "referencedTable": target.name,
                "referencedColumns": [element.column.name for element in constraint.elements],
            }
            if target.schema:
                fk["referencedSchema"] = target.schema
            foreign_keys.append(fk)

        entry: Dict[str, Any] = {"name": table.name, "columns": columns, "foreignKeys": foreign_keys}
        if table.schema:
            entry["schema"] = table.schema
        if table.comment:
            entry["comment"] = table.comment
        tables.append(entry)
    return {"tables": tables}


def _tables_of(source: Any) -> List[sa.Table]:
    if isinstance(source, sa.MetaData):
        return list(source.tables.values())
    if isinstance(source, sa.Table):
        return [source]
    metadata = getattr(source, "metadata", None)
    if isinstance(metadata, sa.MetaData) and not hasattr(source, "__table__"):
        return list(metadata.tables.values())
    if hasattr(source, "__table__"):
        return [source.__table__]
    try:
        items = list(source)
    except TypeError:
        raise TypeError("introspect_sqlalchemy() needs a declarative base, a MetaData, or a list of tables and mapped classes.") from None
    seen: Dict[int, sa.Table] = {}
    for item in items:
        for table in _tables_of(item):
            seen.setdefault(id(table), table)
    return list(seen.values())


def _kind_of(column_type: Any):
    """Returns (kind, enum values or None, with_timezone or None)."""
    if isinstance(column_type, sat.Interval):
        # Interval is implemented on top of DateTime, but it is a duration, not a point in time.
        return "unknown", None, None
    if isinstance(column_type, sat.TypeDecorator):
        return _kind_of(column_type.impl_instance if hasattr(column_type, "impl_instance") else column_type.impl)
    if isinstance(column_type, sat.Enum):
        return "enum", [str(v) for v in column_type.enums], None
    if _UUID_TYPES and isinstance(column_type, _UUID_TYPES):
        return "uuid", None, None
    if isinstance(column_type, sat.Boolean):
        return "boolean", None, None
    if isinstance(column_type, sat.Integer):
        return "integer", None, None
    if isinstance(column_type, (sat.Numeric, sat.Float)):
        return "number", None, None
    if isinstance(column_type, sat.DateTime):
        return "timestamp", None, bool(column_type.timezone)
    if isinstance(column_type, sat.Date):
        return "date", None, None
    if isinstance(column_type, sat.String):
        return "string", None, None
    if isinstance(column_type, sat.JSON):
        return "json", None, None
    return "unknown", None, None


def _db_type(column_type: Any) -> str:
    try:
        return str(column_type.compile(dialect=_PG_DIALECT))
    except Exception:
        return type(column_type).__name__


class SqlalchemyExecutor:
    """Runs each query in its own read-only transaction with a statement timeout."""

    def __init__(self, engine: Any):
        if not hasattr(engine, "connect"):
            raise TypeError("sqlalchemy_executor() needs a SQLAlchemy Engine.")
        if hasattr(engine, "sync_engine"):
            raise TypeError(
                "sqlalchemy_executor() needs a synchronous Engine. Create one with create_engine() for noquery "
                "(it can point at the same database as your AsyncEngine) and call noquery with asyncio.to_thread()."
            )
        self.engine = engine

    def execute(self, compiled: CompiledQuery, timeout_ms: int, max_rows: int) -> ExecuteResult:
        params = require_pyformat(compiled, "sqlalchemy_executor")
        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.exec_driver_sql(READ_ONLY_SQL)
                connection.exec_driver_sql(timeout_statement(timeout_ms))
                result = connection.exec_driver_sql(compiled.sql, params)
                rows = [dict(row) for row in result.mappings().all()]
            finally:
                transaction.rollback()
        return finish(rows, max_rows)


def sqlalchemy_executor(engine: Any) -> SqlalchemyExecutor:
    """Executor for a synchronous SQLAlchemy `Engine` on PostgreSQL (psycopg or psycopg2).

    Each query checks a connection out of the engine's pool, runs in a `READ ONLY` transaction
    with a statement timeout, and is always rolled back. Prefer an engine that connects with a
    read-only database role.
    """
    return SqlalchemyExecutor(engine)
