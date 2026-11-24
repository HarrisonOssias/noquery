import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

import pytest

TESTS = Path(__file__).parent
FIXTURES = TESTS / "fixtures"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

ORG_A = "00000000-0000-0000-0000-00000000000a"
ORG_B = "00000000-0000-0000-0000-00000000000b"
D1 = "00000000-0000-0000-0000-0000000000d1"
D2 = "00000000-0000-0000-0000-0000000000d2"
D3 = "00000000-0000-0000-0000-0000000000d3"
AUTH_A = {"userId": "u1", "orgId": ORG_A, "roles": ["analyst"]}

# The same tables and rows as packages/postgres/test/pglite.test.ts.
FLEET_SQL = f"""
CREATE TYPE device_status AS ENUM ('active', 'inactive', 'retired');
CREATE TABLE organizations (id uuid PRIMARY KEY, name text NOT NULL, region text NOT NULL);
COMMENT ON TABLE organizations IS 'Customer organizations';
CREATE TABLE devices (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES organizations(id),
  serial text NOT NULL,
  status device_status NOT NULL,
  model text NOT NULL,
  installed_at timestamptz NOT NULL
);
CREATE TABLE trips (
  id bigserial PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES organizations(id),
  device_id uuid NOT NULL REFERENCES devices(id),
  started_at timestamptz NOT NULL,
  ended_at timestamptz NOT NULL,
  distance_m double precision,
  is_assisted boolean NOT NULL DEFAULT false,
  note text
);
COMMENT ON COLUMN trips.distance_m IS 'Metres travelled';
CREATE TABLE patients (id serial PRIMARY KEY, email text, ward text);

INSERT INTO organizations VALUES ('{ORG_A}', 'Acme Care', 'east'), ('{ORG_B}', 'Bayside', 'west');
INSERT INTO devices VALUES
  ('{D1}', '{ORG_A}', 'SN-1', 'active',  'X1', '2025-06-01T00:00:00Z'),
  ('{D2}', '{ORG_A}', 'SN-2', 'retired', 'X2', '2025-07-01T00:00:00Z'),
  ('{D3}', '{ORG_B}', 'SN-3', 'active',  'X1', '2025-08-01T00:00:00Z');
INSERT INTO trips (org_id, device_id, started_at, ended_at, distance_m, is_assisted, note) VALUES
  ('{ORG_A}', '{D1}', '2026-03-01T04:30:00Z', '2026-03-01T04:40:00Z', 1000, true,  '100% fine'),
  ('{ORG_A}', '{D1}', '2026-03-01T06:00:00Z', '2026-03-01T06:20:00Z', 2000, false, 'bumpy_ride'),
  ('{ORG_A}', '{D2}', '2026-03-02T15:00:00Z', '2026-03-02T15:30:00Z', 500,  false, NULL),
  ('{ORG_B}', '{D3}', '2026-03-01T12:00:00Z', '2026-03-01T13:00:00Z', 9999, false, NULL),
  ('{ORG_A}', '{D1}', now() - interval '1 day', now() - interval '1 day' + interval '5 minutes', 100, false, 'recent');
"""


def load_parity():
    return json.loads((FIXTURES / "parity.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def parity():
    return load_parity()


@pytest.fixture()
def fleet():
    """The fleet-telemetry semantic model shared with the TypeScript test suites."""
    return load_parity()["model"]


# --- a real PostgreSQL for the integration tests -------------------------------------------------
#
# Set NOQUERY_TEST_DATABASE_URL to a server you can create databases on, or install `pgserver`
# (part of the "dev" extra), which runs a private PostgreSQL from a temporary folder. With neither,
# the integration tests are skipped. The tests create their own database and drop it afterwards.

_state = {}


def _server_url():
    if "server" not in _state:
        url = os.environ.get("NOQUERY_TEST_DATABASE_URL")
        if not url:
            try:
                import tempfile

                import pgserver

                _state["pgserver"] = pgserver.get_server(tempfile.mkdtemp(prefix="noquery-pg-"), cleanup_mode="delete")
                url = _state["pgserver"].get_uri()
            except Exception:
                url = None
        _state["server"] = url
    return _state["server"]


def _with_database(url, name):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))


def test_database_url():
    """Creates the test database on first use. Returns None when no PostgreSQL is available."""
    if "database" in _state:
        return _state["database"]
    _state["database"] = None
    server = _server_url()
    if server:
        try:
            import psycopg

            name = f"noquery_test_{uuid.uuid4().hex[:12]}"
            with psycopg.connect(server, autocommit=True) as connection:
                connection.execute(f'CREATE DATABASE "{name}"')
            url = _with_database(server, name)
            with psycopg.connect(url, autocommit=True) as connection:
                connection.execute(FLEET_SQL)
            _state["database"] = url
            _state["database_name"] = name
        except Exception as error:  # pragma: no cover - depends on the environment
            _state["database_error"] = error
    return _state["database"]


test_database_url.__test__ = False


def django_database(url):
    """A Django DATABASES entry for a libpq URL, including socket-directory hosts."""
    if not url:
        return {"ENGINE": "django.db.backends.postgresql", "NAME": "noquery_unused"}
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parts.path.lstrip("/")),
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or (query.get("host") or [""])[0],
        "PORT": str(parts.port or (query.get("port") or [""])[0]),
    }


def pytest_configure(config):
    try:
        import django
        from django.conf import settings
    except ImportError:
        return
    if not settings.configured:
        settings.configure(
            SECRET_KEY="tests-only",
            INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth", "fleetapp"],
            DATABASES={"default": django_database(test_database_url())},
            ROOT_URLCONF="django_urls",
            DEFAULT_AUTO_FIELD="django.db.models.AutoField",
            USE_TZ=True,
            ALLOWED_HOSTS=["testserver"],
        )
        django.setup()


def pytest_unconfigure(config):
    name = _state.get("database_name")
    if name and _state.get("server"):
        try:
            import psycopg
            from django.db import connections

            connections.close_all()
            with psycopg.connect(_state["server"], autocommit=True) as connection:
                connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        except Exception:
            pass
    server = _state.get("pgserver")
    if server is not None:
        try:
            server.cleanup()
        except Exception:
            pass


@pytest.fixture(scope="session")
def database_url():
    url = test_database_url()
    if not url:
        reason = _state.get("database_error") or "set NOQUERY_TEST_DATABASE_URL or install pgserver"
        pytest.skip(f"No PostgreSQL available for integration tests: {reason}")
    return url
