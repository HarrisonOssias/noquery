"""FastAPI + SQLAlchemy example: noquery embedded in your own app, with no sidecar.

Run it (from the repository root, after `pip install "./clients/python[sqlalchemy,fastapi,anthropic]" "psycopg[binary]" uvicorn`):

    DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/app NOQUERY_PLANNER=off \
        uvicorn --app-dir examples/fastapi-sqlalchemy app:app

On first start it creates three small tables (organizations, devices, trips) and fills them with
demo rows. Leave out NOQUERY_PLANNER=off, with ANTHROPIC_API_KEY set, to enable plain-English questions.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from fastapi import FastAPI, Header, HTTPException
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from noquery import Noquery, create_planner, draft_semantics, merge_semantics, postgres_dialect
from noquery.integrations.fastapi import create_router
from noquery.integrations.sqlalchemy import introspect_sqlalchemy, sqlalchemy_executor

ACME = uuid.UUID("00000000-0000-0000-0000-00000000000a")
BAYSIDE = uuid.UUID("00000000-0000-0000-0000-00000000000b")


# --- your models, as you already have them --------------------------------------------------


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    name: Mapped[str]
    region: Mapped[str]


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id"))
    serial: Mapped[str]
    status: Mapped[str] = mapped_column(sa.Enum("active", "inactive", "retired", name="device_status"))
    model: Mapped[str]


class Trip(Base):
    __tablename__ = "trips"
    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("organizations.id"))
    device_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("devices.id"))
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    distance_m: Mapped[float] = mapped_column(sa.Float, comment="Metres travelled")


# --- the semantic layer: what the data means and who may see it ---------------------------------

OVERLAY = {
    "entities": {
        "organizations": {"rowScope": [{"column": "id", "authKey": "orgId"}]},
        "devices": {
            "rowScope": [{"column": "org_id", "authKey": "orgId"}],
            "dimensions": {"serial": {"policy": "aggregate_only"}},  # can be counted, never listed
        },
        "trips": {
            "description": "One continuous movement of a device.",
            "synonyms": ["journeys", "rides"],
            "rowScope": [{"column": "org_id", "authKey": "orgId"}],
            "measures": {
                "avg_duration_min": {
                    "agg": "avg",
                    "sql": "extract(epoch from ${TABLE}.ended_at - ${TABLE}.started_at) / 60",
                    "label": "Average duration (min)",
                },
                "total_distance_km": {"agg": "sum", "sql": "${TABLE}.distance_m / 1000.0", "label": "Total distance (km)"},
            },
        },
    }
}


def build_noquery(engine: sa.Engine) -> Noquery:
    planner = None
    if os.environ.get("NOQUERY_PLANNER", "on") != "off":
        from noquery.integrations.anthropic import anthropic_provider

        planner = create_planner(anthropic_provider())
    return Noquery(
        model=merge_semantics(draft_semantics(introspect_sqlalchemy(Base)), OVERLAY),
        dialect=postgres_dialect(),
        executor=sqlalchemy_executor(engine),
        planner=planner,
        default_timezone=os.environ.get("NOQUERY_TIMEZONE", "UTC"),
        on_audit=lambda event: print("audit:", event["type"], event["outcome"], event.get("userId"), f'{event["durationMs"]}ms'),
    )


# --- who is asking ------------------------------------------------------------------------------


def get_auth(x_demo_org: str = Header(default="")):
    """Demo only. In a real app, depend on your own login and read the organization and roles
    from the verified session, never from a header or the request body."""
    orgs = {"acme": str(ACME), "bayside": str(BAYSIDE)}
    if x_demo_org not in orgs:
        raise HTTPException(status_code=401, detail="Send X-Demo-Org: acme or bayside")
    return {"userId": f"demo-{x_demo_org}", "orgId": orgs[x_demo_org], "roles": ["analyst"]}


# --- the app ------------------------------------------------------------------------------------


def seed(engine: sa.Engine) -> None:
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        if session.scalar(sa.select(sa.func.count()).select_from(Organization)):
            return
        session.add_all([Organization(id=ACME, name="Acme Care", region="east"), Organization(id=BAYSIDE, name="Bayside", region="west")])
        session.flush()
        devices = [
            Device(org_id=ACME, serial="SN-1", status="active", model="X1"),
            Device(org_id=ACME, serial="SN-2", status="retired", model="X2"),
            Device(org_id=BAYSIDE, serial="SN-3", status="active", model="X1"),
        ]
        session.add_all(devices)
        session.flush()
        now = datetime.now(timezone.utc)
        for i in range(30):
            device = devices[i % 3]
            start = now - timedelta(days=i % 10, hours=i)
            session.add(
                Trip(id=i + 1, org_id=device.org_id, device_id=device.id, started_at=start, ended_at=start + timedelta(minutes=10 + i), distance_m=500 + 100 * i)
            )
        session.commit()


def create_app(database_url: str) -> FastAPI:
    engine = sa.create_engine(database_url)
    seed(engine)
    app = FastAPI(title="noquery FastAPI example")
    app.include_router(create_router(build_noquery(engine), get_auth), prefix="/analytics")
    return app


if "DATABASE_URL" in os.environ:
    app = create_app(os.environ["DATABASE_URL"])
