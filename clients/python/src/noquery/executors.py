"""Executors for plain DB-API drivers (psycopg, psycopg2). ORM executors live in `noquery.integrations`."""

from __future__ import annotations

from typing import Any, Callable, ContextManager, Dict, List, Mapping, Sequence

from .runtime import CompiledQuery, ExecuteResult, finish

READ_ONLY_SQL = "SET TRANSACTION READ ONLY"


def timeout_statement(timeout_ms: float) -> str:
    return f"SET LOCAL statement_timeout = {max(1, int(timeout_ms))}"


def require_pyformat(compiled: CompiledQuery, executor: str) -> Dict[str, Any]:
    if compiled.paramstyle != "pyformat":
        raise ValueError(f'{executor} needs postgres_dialect(paramstyle="pyformat"), got "{compiled.paramstyle}".')
    return compiled.named_params()


def rows_from_cursor(cursor: Any) -> List[Dict[str, Any]]:
    names = [d[0] for d in cursor.description or []]
    return [row if isinstance(row, dict) else dict(zip(names, row)) for row in cursor.fetchall()]


class DbapiExecutor:
    """Runs each query in its own read-only transaction with a statement timeout."""

    def __init__(self, connect: Callable[[], ContextManager[Any]]):
        self._connect = connect

    def execute(self, compiled: CompiledQuery, timeout_ms: int, max_rows: int) -> ExecuteResult:
        params = require_pyformat(compiled, "dbapi_executor")
        with self._connect() as connection:
            # Leave any transaction the connection was handed over in, so ours starts clean.
            connection.rollback()
            try:
                cursor = connection.cursor()
                try:
                    cursor.execute(READ_ONLY_SQL)
                    cursor.execute(timeout_statement(timeout_ms))
                    cursor.execute(compiled.sql, params)
                    rows = rows_from_cursor(cursor)
                finally:
                    cursor.close()
            finally:
                connection.rollback()
        return finish(rows, max_rows)


def dbapi_executor(connect: Callable[[], ContextManager[Any]]) -> DbapiExecutor:
    """Executor for a DB-API 2.0 Postgres driver with "pyformat" parameters (psycopg, psycopg2).

    `connect` returns a context manager that yields a connection and gives it back on exit.
    The connection must not be in autocommit mode.

        pool = psycopg_pool.ConnectionPool(dsn)
        executor = dbapi_executor(pool.connection)
    """
    return DbapiExecutor(connect)


def dbapi_catalog_query(connect: Callable[[], ContextManager[Any]]) -> Callable[[str, Dict[str, Any]], Sequence[Mapping[str, Any]]]:
    """A `query` function for `introspect_postgres`, built from the same connection factory."""

    def query(sql: str, params: Dict[str, Any]) -> Sequence[Mapping[str, Any]]:
        with connect() as connection:
            try:
                cursor = connection.cursor()
                try:
                    cursor.execute(sql, params)
                    return rows_from_cursor(cursor)
                finally:
                    cursor.close()
            finally:
                connection.rollback()

    return query
