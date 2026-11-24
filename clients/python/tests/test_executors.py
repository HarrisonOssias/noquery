import contextlib

import pytest

from noquery import CompiledQuery, dbapi_catalog_query, dbapi_executor, introspect_postgres
from noquery.executors import timeout_statement
from noquery.runtime import finish


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self._rows = []

    def execute(self, sql, params=None):
        self.connection.log.append(("execute", sql, params))
        if self.connection.fail_on and self.connection.fail_on in sql:
            raise RuntimeError("boom")
        self.description = [(name,) for name in self.connection.columns]
        self._rows = list(self.connection.rows)

    def fetchall(self):
        return self._rows

    def close(self):
        self.connection.log.append(("close-cursor",))


class FakeConnection:
    def __init__(self, columns=(), rows=(), fail_on=None):
        self.columns, self.rows, self.fail_on = columns, rows, fail_on
        self.log = []

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.log.append(("rollback",))


def factory(connection):
    @contextlib.contextmanager
    def connect():
        connection.log.append(("checkout",))
        try:
            yield connection
        finally:
            connection.log.append(("checkin",))

    return connect


def test_finish_applies_the_row_cap():
    rows = [{"n": i} for i in range(3)]
    assert finish(rows, 3).truncated is False and len(finish(rows, 3).rows) == 3
    capped = finish(rows, 2)
    assert capped.truncated is True and capped.rows == [{"n": 0}, {"n": 1}]


def test_timeout_statement_is_always_a_positive_integer():
    assert timeout_statement(1500) == "SET LOCAL statement_timeout = 1500"
    assert timeout_statement(0) == "SET LOCAL statement_timeout = 1"
    assert timeout_statement(12.9) == "SET LOCAL statement_timeout = 12"
    with pytest.raises((TypeError, ValueError)):
        timeout_statement("1; DROP TABLE trips")


def test_dbapi_executor_runs_read_only_with_a_timeout_and_always_rolls_back():
    connection = FakeConnection(columns=("devices.model", "trips.count"), rows=[("X1", 2), ("X2", 1), ("X3", 1)])
    executor = dbapi_executor(factory(connection))
    result = executor.execute(CompiledQuery(sql="SELECT ... %(p1)s", params=["acme"]), timeout_ms=2500, max_rows=2)
    assert result.rows == [{"devices.model": "X1", "trips.count": 2}, {"devices.model": "X2", "trips.count": 1}]
    assert result.truncated is True
    assert connection.log == [
        ("checkout",),
        ("rollback",),
        ("execute", "SET TRANSACTION READ ONLY", None),
        ("execute", "SET LOCAL statement_timeout = 2500", None),
        ("execute", "SELECT ... %(p1)s", {"p1": "acme"}),
        ("close-cursor",),
        ("rollback",),
        ("checkin",),
    ]


def test_dbapi_executor_rolls_back_and_releases_when_the_query_fails():
    connection = FakeConnection(fail_on="SELECT")
    executor = dbapi_executor(factory(connection))
    with pytest.raises(RuntimeError, match="boom"):
        executor.execute(CompiledQuery(sql="SELECT 1", params=[]), timeout_ms=1000, max_rows=10)
    assert connection.log[-3:] == [("close-cursor",), ("rollback",), ("checkin",)]


def test_dbapi_executor_accepts_dict_rows():
    connection = FakeConnection(columns=("n",), rows=[{"n": 1}])
    assert dbapi_executor(factory(connection)).execute(CompiledQuery(sql="SELECT 1", params=[]), 1000, 10).rows == [{"n": 1}]


def test_dbapi_executor_refuses_numeric_placeholders():
    connection = FakeConnection()
    with pytest.raises(ValueError, match='dbapi_executor needs postgres_dialect\\(paramstyle="pyformat"\\)'):
        dbapi_executor(factory(connection)).execute(CompiledQuery(sql="SELECT $1", params=[1], paramstyle="numeric"), 1000, 10)
    assert connection.log == []


def test_dbapi_catalog_query():
    connection = FakeConnection(columns=("a", "b"), rows=[(1, 2)])
    query = dbapi_catalog_query(factory(connection))
    assert query("SELECT %(x)s", {"x": 1}) == [{"a": 1, "b": 2}]
    assert connection.log == [("checkout",), ("execute", "SELECT %(x)s", {"x": 1}), ("close-cursor",), ("rollback",), ("checkin",)]


def test_introspect_postgres_handles_drivers_that_return_arrays_as_text():
    calls = []

    def query(sql, params):
        calls.append(params)
        if "information_schema.columns" in sql:
            base = {"table_schema": "app", "table_type": "BASE TABLE", "udt_schema": "pg_catalog", "column_comment": None, "table_comment": None}
            return [
                {**base, "table_name": "orders", "column_name": "id", "is_nullable": "NO", "data_type": "bigint", "udt_name": "int8", "table_comment": "Customer orders"},
                {**base, "table_name": "orders", "column_name": "state", "is_nullable": "NO", "data_type": "USER-DEFINED", "udt_name": "order_state", "udt_schema": "app"},
                {**base, "table_name": "orders", "column_name": "placed_at", "is_nullable": "YES", "data_type": "timestamp without time zone", "udt_name": "timestamp"},
                {**base, "table_name": "orders", "column_name": "shape", "is_nullable": "YES", "data_type": "USER-DEFINED", "udt_name": "geometry", "udt_schema": "public"},
                {**base, "table_name": "lines", "column_name": "order_id", "is_nullable": "NO", "data_type": "bigint", "udt_name": "int8"},
                {**base, "table_name": "lines", "column_name": "line_no", "is_nullable": "NO", "data_type": "integer", "udt_name": "int4"},
            ]
        if "pg_constraint" in sql:
            return [
                {"table_schema": "app", "table_name": "orders", "contype": "p", "columns": "{id}", "ref_schema": None, "ref_table": None, "ref_columns": "{}"},
                {"table_schema": "app", "table_name": "lines", "contype": "p", "columns": '{order_id,"line_no"}', "ref_schema": None, "ref_table": None, "ref_columns": "{}"},
                {"table_schema": "app", "table_name": "lines", "contype": "f", "columns": ["order_id"], "ref_schema": "app", "ref_table": "orders", "ref_columns": ["id"]},
                {"table_schema": "app", "table_name": "elsewhere", "contype": "p", "columns": "{id}", "ref_schema": None, "ref_table": None, "ref_columns": "{}"},
            ]
        return [{"enum_schema": "app", "enum_name": "order_state", "labels": "{open,closed}"}]

    graph = introspect_postgres(query, schemas=["app"], include_views=False)
    assert calls == [{"schemas": ["app"], "table_types": ["BASE TABLE"]}, {"schemas": ["app"]}, {}]
    orders, lines = graph["tables"]
    assert orders["name"] == "orders" and orders["schema"] == "app" and orders["comment"] == "Customer orders"
    assert orders["columns"] == [
        {"name": "id", "kind": "integer", "dbType": "bigint", "nullable": False, "primaryKey": True},
        {"name": "state", "kind": "enum", "dbType": "order_state", "nullable": False, "enumValues": ["open", "closed"]},
        {"name": "placed_at", "kind": "timestamp", "dbType": "timestamp without time zone", "nullable": True, "withTimezone": False},
        {"name": "shape", "kind": "unknown", "dbType": "geometry", "nullable": True},
    ]
    assert [c.get("primaryKey") for c in lines["columns"]] == [True, True]
    assert lines["foreignKeys"] == [{"columns": ["order_id"], "referencedTable": "orders", "referencedSchema": "app", "referencedColumns": ["id"]}]


def test_introspect_postgres_defaults_to_public_tables_and_views():
    calls = []

    def query(sql, params):
        calls.append(params)
        return []

    assert introspect_postgres(query) == {"tables": []}
    assert calls[0] == {"schemas": ["public"], "table_types": ["BASE TABLE", "VIEW"]}
