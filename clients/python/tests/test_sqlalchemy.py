import enum

import pytest

sa = pytest.importorskip("sqlalchemy", reason="needs the sqlalchemy extra")

from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from noquery import CompiledQuery, draft_semantics
from noquery.integrations.sqlalchemy import introspect_sqlalchemy, sqlalchemy_executor


class Base(DeclarativeBase):
    pass


class Status(enum.Enum):
    active = "on"
    retired = "off"


class Cents(sa.types.TypeDecorator):
    impl = sa.Integer
    cache_ok = True


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = {"comment": "Customer organizations"}
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(80))


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = {"schema": "fleet"}
    id = mapped_column(sa.Uuid, primary_key=True)
    org_id = mapped_column(sa.ForeignKey("organizations.id"), nullable=False)
    status = mapped_column(sa.Enum(Status, name="device_status"), nullable=False)
    tier = mapped_column(sa.Enum("basic", "pro", name="tier"))
    legacy_id = mapped_column(postgresql.UUID(as_uuid=False))
    installed_at = mapped_column(sa.DateTime(timezone=True))
    created_at = mapped_column(sa.DateTime)
    service_date = mapped_column(sa.Date)
    weight = mapped_column(sa.Numeric(10, 2), comment="Kilograms")
    ratio = mapped_column(sa.Float)
    seats = mapped_column(sa.SmallInteger)
    odometer = mapped_column(sa.BigInteger)
    price = mapped_column(Cents)
    is_spare = mapped_column(sa.Boolean, nullable=False)
    notes = mapped_column(sa.Text)
    config = mapped_column(sa.JSON)
    settings = mapped_column(postgresql.JSONB)
    blob = mapped_column(sa.LargeBinary)
    span = mapped_column(sa.Interval)


class Reading(Base):
    __tablename__ = "readings"
    device_id = mapped_column(sa.Uuid, primary_key=True)
    seq = mapped_column(sa.Integer, primary_key=True)
    org_id = mapped_column(sa.Integer)
    __table_args__ = (
        sa.ForeignKeyConstraint(["device_id"], ["fleet.devices.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
    )


def table(graph, name):
    return next(t for t in graph["tables"] if t["name"] == name)


def columns(graph, name):
    return {c["name"]: c for c in table(graph, name)["columns"]}


def test_reads_every_table_from_a_declarative_base():
    graph = introspect_sqlalchemy(Base)
    assert sorted(t["name"] for t in graph["tables"]) == ["devices", "organizations", "readings"]
    assert table(graph, "devices")["schema"] == "fleet"
    assert "schema" not in table(graph, "organizations")
    assert table(graph, "organizations")["comment"] == "Customer organizations"


def test_maps_column_types():
    devices = columns(introspect_sqlalchemy(Base), "devices")
    kinds = {name: c["kind"] for name, c in devices.items()}
    assert kinds == {
        "id": "uuid",
        "org_id": "integer",
        "status": "enum",
        "tier": "enum",
        "legacy_id": "uuid",
        "installed_at": "timestamp",
        "created_at": "timestamp",
        "service_date": "date",
        "weight": "number",
        "ratio": "number",
        "seats": "integer",
        "odometer": "integer",
        "price": "integer",
        "is_spare": "boolean",
        "notes": "string",
        "config": "json",
        "settings": "json",
        "blob": "unknown",
        "span": "unknown",
    }
    assert devices["id"] == {"name": "id", "kind": "uuid", "dbType": "UUID", "nullable": False, "primaryKey": True}
    assert devices["status"]["enumValues"] == ["active", "retired"]
    assert devices["tier"]["enumValues"] == ["basic", "pro"]
    assert devices["installed_at"]["withTimezone"] is True and devices["created_at"]["withTimezone"] is False
    assert devices["installed_at"]["dbType"] == "TIMESTAMP WITH TIME ZONE"
    assert devices["weight"]["comment"] == "Kilograms" and devices["weight"]["dbType"] == "NUMERIC(10, 2)"
    assert devices["is_spare"]["nullable"] is False and devices["notes"]["nullable"] is True
    assert "withTimezone" not in devices["service_date"] and "enumValues" not in devices["notes"]


def test_reads_foreign_keys_including_composite_keys_and_schemas():
    graph = introspect_sqlalchemy(Base)
    assert table(graph, "devices")["foreignKeys"] == [{"columns": ["org_id"], "referencedTable": "organizations", "referencedColumns": ["id"]}]
    assert table(graph, "readings")["foreignKeys"] == [
        {"columns": ["device_id"], "referencedTable": "devices", "referencedColumns": ["id"], "referencedSchema": "fleet"},
        {"columns": ["org_id"], "referencedTable": "organizations", "referencedColumns": ["id"]},
    ]
    assert [c.get("primaryKey") for c in table(graph, "readings")["columns"]] == [True, True, None]


def test_accepts_metadata_tables_and_mapped_classes():
    by_base = introspect_sqlalchemy(Base)
    assert introspect_sqlalchemy(Base.metadata) == by_base
    assert [t["name"] for t in introspect_sqlalchemy(Organization)["tables"]] == ["organizations"]
    assert [t["name"] for t in introspect_sqlalchemy(Device.__table__)["tables"]] == ["devices"]
    mixed = introspect_sqlalchemy([Organization, Device.__table__, Organization])
    assert [t["name"] for t in mixed["tables"]] == ["organizations", "devices"]
    with pytest.raises(TypeError, match="needs a declarative base"):
        introspect_sqlalchemy(42)


def test_include_and_exclude():
    assert [t["name"] for t in introspect_sqlalchemy(Base, include=["devices"])["tables"]] == ["devices"]
    assert sorted(t["name"] for t in introspect_sqlalchemy(Base, exclude=["devices"])["tables"]) == ["organizations", "readings"]
    assert introspect_sqlalchemy(Base, include=[])["tables"] == []


def test_the_graph_drafts_a_sound_model():
    model = draft_semantics(introspect_sqlalchemy(Base))
    devices = model["entities"]["devices"]
    assert devices["schema"] == "fleet"
    assert devices["dimensions"]["status"]["enumValues"] == ["active", "retired"]
    assert devices["dimensions"]["created_at"]["timeStorage"] == "utc"
    assert devices["dimensions"]["service_date"]["timeStorage"] == "local"
    assert devices["dimensions"]["org_id"]["policy"] == "aggregate_only"
    assert "blob" not in devices["dimensions"]
    assert {"from": "devices", "to": "organizations", "on": [["org_id", "id"]], "relationship": "many_to_one"} in model["joins"]
    assert {"from": "readings", "to": "devices", "on": [["device_id", "id"]], "relationship": "many_to_one"} in model["joins"]


# --- executor (the real-database behavior is covered in test_postgres_integration.py) ----------


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class FakeTransaction:
    def __init__(self, log):
        self.log = log

    def rollback(self):
        self.log.append("rollback")


class FakeConnection:
    def __init__(self, log, rows, fail):
        self.log, self.rows, self.fail = log, rows, fail

    def __enter__(self):
        self.log.append("checkout")
        return self

    def __exit__(self, *exc):
        self.log.append("checkin")
        return False

    def begin(self):
        self.log.append("begin")
        return FakeTransaction(self.log)

    def exec_driver_sql(self, sql, params=None):
        self.log.append((sql, params))
        if self.fail and sql.startswith("SELECT"):
            raise RuntimeError("boom")
        return FakeResult(self.rows)


class FakeEngine:
    def __init__(self, rows=(), fail=False):
        self.log, self.rows, self.fail = [], list(rows), fail

    def connect(self):
        return FakeConnection(self.log, self.rows, self.fail)


def test_executor_runs_read_only_with_a_timeout_and_always_rolls_back():
    engine = FakeEngine(rows=[{"n": 1}, {"n": 2}])
    result = sqlalchemy_executor(engine).execute(CompiledQuery(sql="SELECT %(p1)s", params=["x"]), timeout_ms=900, max_rows=1)
    assert result.rows == [{"n": 1}] and result.truncated is True
    assert engine.log == [
        "checkout",
        "begin",
        ("SET TRANSACTION READ ONLY", None),
        ("SET LOCAL statement_timeout = 900", None),
        ("SELECT %(p1)s", {"p1": "x"}),
        "rollback",
        "checkin",
    ]


def test_executor_rolls_back_when_the_query_fails():
    engine = FakeEngine(fail=True)
    with pytest.raises(RuntimeError, match="boom"):
        sqlalchemy_executor(engine).execute(CompiledQuery(sql="SELECT 1", params=[]), timeout_ms=900, max_rows=1)
    assert engine.log[-2:] == ["rollback", "checkin"]


def test_executor_rejects_unsuitable_engines_and_paramstyles():
    with pytest.raises(TypeError, match="needs a SQLAlchemy Engine"):
        sqlalchemy_executor("postgresql://localhost/app")

    class AsyncLike(FakeEngine):
        sync_engine = object()

    with pytest.raises(TypeError, match="needs a synchronous Engine"):
        sqlalchemy_executor(AsyncLike())
    engine = FakeEngine()
    with pytest.raises(ValueError, match="pyformat"):
        sqlalchemy_executor(engine).execute(CompiledQuery(sql="SELECT $1", params=[1], paramstyle="numeric"), 900, 1)
    assert engine.log == []
