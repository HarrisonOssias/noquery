# noquery

Natural-language analytics for any backend. Plug in your ORM, describe what your data means, and let people ask questions in plain English, safely.

The model never writes SQL. It fills in a small typed **Query IR** that can only name fields your semantic layer defines. noquery validates it, applies your row-level and column policies, compiles it to SQL with bound parameters, and runs it read-only on your own connection.

```
question -> planner (LLM) -> Query IR -> validator + policies -> dialect SQL -> read-only executor -> rows + chart + explanation
```

Use it as a TypeScript package in a Node backend, or as a Python package in a FastAPI, Django or other Python backend. Both run the same engine rules and are tested against each other.

Status: v0.1, pre-release. APIs will change. Nothing is published to npm or PyPI yet; install from this repository.

## Contents

- [Packages](#packages)
- [Using noquery from Python](#using-noquery-from-python)
  - [Embedded in your app](#embedded-in-your-app): [SQLAlchemy](#quick-start-with-sqlalchemy), [FastAPI](#fastapi), [Django](#django), [other drivers](#other-postgres-drivers)
  - [Using the results](#using-the-results)
  - [Python reference](#python-reference)
  - [Sidecar](#sidecar-noquery-as-a-separate-service)
  - [Troubleshooting](#troubleshooting)
- [Using noquery from TypeScript](#using-noquery-from-typescript)
- [Semantic layer](#semantic-layer)
- [Safety model](#safety-model)
- [HTTP API](#http-api)
- [Development](#development)

## Packages

| Package | What it does |
| --- | --- |
| `@noquery/core` | Schema graph, semantic layer (draft + merge), Query IR, validator and policy engine, catalog, planner, engine |
| `@noquery/postgres` | Postgres dialect, executors (`pg`, postgres.js, PGlite) and catalog introspection |
| `@noquery/drizzle` | Reads Drizzle `pgTable` definitions into a schema graph |
| `@noquery/anthropic` | Planner provider backed by Claude (structured outputs) |
| `@noquery/server` | HTTP service (`noquery-server`) that exposes an engine to other languages and services |
| `noquery` (Python, `clients/python`) | The engine as a Python package: validator, policy engine, Postgres dialect, planner, plus SQLAlchemy, Django, FastAPI and Claude integrations. Also the client for `noquery-server`. The core is standard library only |

## Using noquery from Python

### Two ways to run it

| | Embedded (recommended) | Sidecar |
| --- | --- | --- |
| What runs | The engine, inside your Python process | A Node.js service next to your app |
| Install | `pip install` only | Node.js, pnpm and this repository |
| Database access | Your own SQLAlchemy engine or Django connection | The sidecar's own connection |
| Class | `Noquery` | `NoqueryClient` |
| Choose it when | Your backend is Python and uses PostgreSQL | Several services or languages share one noquery, or you want it isolated from the app |

Both classes have the same methods (`ask`, `run`, `validate`, `catalog`) and return the same `Result`, so code written for one works with the other.

### Embedded in your app

#### How it works

```
 Your Python app (FastAPI, Django, Flask, a script, a notebook)
        |
        |  nq.ask(question, auth={...})          auth = the logged-in user, from your own session
        v
 noquery (a Python package, in the same process)
        |-- planner (Claude): question -> Query IR        [only for ask()]
        |-- validator + row scope + column policies
        |-- Postgres SQL, bound parameters
        v
 Your Postgres database, through your SQLAlchemy engine or Django connection (read-only transactions)
```

Who does what:

- **Your app** logs users in, as it does today. On every call it passes the verified user's identity as `auth`, for example `{"userId": "u-42", "orgId": "acme", "roles": ["analyst"]}`.
- **noquery** enforces everything: it only lets that user see rows for `orgId = "acme"`, hides restricted columns, and rejects anything the semantic layer does not define.
- **Your ORM** is only read for its table definitions, and lends noquery a connection. noquery does not go through ORM queries, so model managers, default filters and signals do not apply; access rules belong in the semantic layer.

#### Requirements

| Need | Version | Used for |
| --- | --- | --- |
| Python | 3.9 or newer (3.10 or newer for the Claude planner) | The package. The core has no third-party dependencies |
| PostgreSQL | any supported version | Your data. PostgreSQL is the only supported database |
| A Postgres driver | psycopg 3 or psycopg2 | You almost certainly have one already |
| SQLAlchemy | 2.0 or newer, optional | `noquery[sqlalchemy]` |
| Django | 4.2 or newer, optional | `noquery[django]` |
| FastAPI | optional | `noquery[fastapi]` |
| Anthropic API credentials | | Only for `ask()` (plain-English questions). `run()` works without them |
| pandas | optional | `Result.to_dataframe()` |

#### Install

Nothing is published to PyPI yet, so install from this repository. Pick the extras that match your stack.

FastAPI with SQLAlchemy:

```bash
pip install "./clients/python[sqlalchemy,fastapi,anthropic]"
```

Django:

```bash
pip install "./clients/python[django,anthropic]"
```

Core only (bring your own psycopg connection):

```bash
pip install ./clients/python
```

#### Quick start with SQLAlchemy

```python
import sqlalchemy as sa
from noquery import Noquery, draft_semantics, merge_semantics, postgres_dialect
from noquery.integrations.sqlalchemy import introspect_sqlalchemy, sqlalchemy_executor

from myapp.db import Base, engine   # your declarative base and your Engine

# 1. Read your models and draft a semantic model: one entity per table, a dimension per
#    useful column, count/sum/average measures, joins from foreign keys, sensitive columns hidden.
draft = draft_semantics(introspect_sqlalchemy(Base))

# 2. Say what the data means and who may see it. At minimum, add a rowScope to every table
#    that belongs to a tenant, user or organization.
model = merge_semantics(draft, {
    "entities": {
        "trips": {"rowScope": [{"column": "org_id", "authKey": "orgId"}]},
        "devices": {
            "rowScope": [{"column": "org_id", "authKey": "orgId"}],
            "dimensions": {"serial": {"policy": "aggregate_only"}},   # can be counted, never listed
        },
        "audit_log": None,                                            # hide a whole table
    },
})

# 3. Create the engine once, at startup, and reuse it.
nq = Noquery(
    model=model,
    dialect=postgres_dialect(),
    executor=sqlalchemy_executor(engine),
    default_timezone="America/Toronto",
)

# 4. Run a query for a user.
auth = {"userId": "u-42", "orgId": "acme", "roles": ["analyst"]}
result = nq.run(
    {
        "version": 1,
        "measures": ["trips.count"],
        "dimensions": ["devices.model"],
        "time": {"field": "trips.started_at", "range": "last_7_days"},
    },
    auth=auth,
)
print(result.explanation)
for row in result.labeled_rows():
    print(row)
```

`introspect_sqlalchemy` accepts a declarative base, a `MetaData`, or a list of tables and mapped classes, and takes `include=[...]` or `exclude=[...]` table names. To read the live database instead of your models, reflect it first:

```python
metadata = sa.MetaData()
metadata.reflect(engine)
draft = draft_semantics(introspect_sqlalchemy(metadata))
```

`sqlalchemy_executor` takes a synchronous `Engine` using the psycopg or psycopg2 driver. Every query checks out a pooled connection, runs in a `READ ONLY` transaction with a statement timeout, and is rolled back.

A complete app is in `examples/fastapi-sqlalchemy/app.py`.

#### FastAPI

`create_router` gives you the whole API in one line. You supply a dependency that returns the auth context for the logged-in user:

```python
from fastapi import Depends, FastAPI
from noquery.integrations.fastapi import create_router

from myapp.auth import get_current_user   # your existing auth dependency
from myapp.analytics import nq            # the Noquery instance from the quick start

app = FastAPI()


def get_auth(user=Depends(get_current_user)):
    # Build auth from the verified session only. Never accept orgId or roles from the request.
    return {"userId": str(user.id), "orgId": str(user.org_id), "roles": user.roles}


app.include_router(create_router(nq, get_auth), prefix="/analytics")
```

| Route | Body | Response |
| --- | --- | --- |
| `GET /analytics/catalog` | | The entities, measures and dimensions this user may use |
| `POST /analytics/ask` | `{question, timezone?, history?}` | `{type: "answer", rows, columns, ...}` or `{type: "clarify" or "unanswerable" or "invalid", ...}` |
| `POST /analytics/run` | `{ir}` | `{rows, columns, sql, params, explanation, chart, truncated, ...}` |
| `POST /analytics/validate` | `{ir}` | `{ok: true}` or `{ok: false, errors}` |

Errors use the same `{"error": {"code", "message", ...}}` body and status codes as the [HTTP API](#http-api). Unlike the sidecar, these routes take no `auth` in the body: it always comes from your dependency.

The endpoints are plain `def` functions, so FastAPI runs them in its thread pool and the database and model calls do not block the event loop.

To shape the response yourself, call `nq` directly from your own endpoint:

```python
@app.post("/reports/weekly")
def weekly(auth=Depends(get_auth)):
    result = nq.run(WEEKLY_TRIPS_IR, auth=auth)
    return {"summary": result.explanation, "rows": result.labeled_rows()}
```

#### Django

```python
# myapp/analytics.py
from noquery import Noquery, draft_semantics, merge_semantics, postgres_dialect
from noquery.integrations.django import django_executor, introspect_django

_nq = None


def get_noquery():
    """Built on first use, because models can only be read once Django has loaded the apps."""
    global _nq
    if _nq is None:
        model = merge_semantics(
            draft_semantics(introspect_django(app_labels=["fleet", "billing"])),
            {
                "entities": {
                    "fleet_trip": {"rowScope": [{"column": "org_id", "authKey": "orgId"}]},
                    "billing_invoice": {"roles": ["finance"]},
                },
            },
        )
        _nq = Noquery(model=model, dialect=postgres_dialect(), executor=django_executor(using="default"))
    return _nq
```

Entity names are database table names (`fleet_trip` for the `Trip` model in the `fleet` app), and field names are column names (`org_id`, not `org`).

`introspect_django` reads every installed app except Django's own (`django.contrib.*`: users, sessions, admin and so on). Pass `app_labels=[...]` to choose apps, including Django's if you want them, and `include=[...]` or `exclude=[...]` to choose tables. Fields with `choices` become dimensions with a fixed list of values, and `help_text` or `db_comment` becomes the field's description for the planner.

Call it from a view:

```python
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse

from myapp.analytics import get_noquery


@login_required
def trips_by_model(request):
    auth = {
        "userId": str(request.user.id),
        "orgId": str(request.user.profile.org_id),   # however your app stores the user's organization
        "roles": list(request.user.groups.values_list("name", flat=True)),
    }
    result = get_noquery().run(
        {"version": 1, "measures": ["fleet_trip.count"], "dimensions": ["fleet_device.model"]},
        auth=auth,
    )
    return JsonResponse({"explanation": result.explanation, "rows": result.labeled_rows(), "chart": result.chart})
```

Or mount the whole API. The routes and bodies are the same as in the FastAPI table above:

```python
# urls.py
from django.core.exceptions import PermissionDenied
from django.urls import include, path
from django.utils.functional import SimpleLazyObject
from noquery.integrations.django import noquery_urls

from myapp.analytics import get_noquery


def get_auth(request):
    if not request.user.is_authenticated:
        raise PermissionDenied
    return {"userId": str(request.user.id), "orgId": str(request.user.profile.org_id), "roles": ["analyst"]}


urlpatterns = [
    path("analytics/", include(noquery_urls(SimpleLazyObject(get_noquery), get_auth))),
]
```

These views keep Django's CSRF protection, so browser code must send the CSRF token with each `POST`.

`django_executor` needs the PostgreSQL database backend. Each query runs read-only with a statement timeout and is rolled back. Inside a transaction you already opened (for example with `ATOMIC_REQUESTS`) it uses a savepoint, so the query sees your uncommitted changes and your own writes carry on normally afterwards. To connect as a read-only role, add a second alias to `DATABASES` and pass `django_executor(using="analytics")`.

#### Other Postgres drivers

Without an ORM, give noquery a function that returns a connection as a context manager, and read the schema from the Postgres catalog:

```python
import psycopg_pool
from noquery import (
    Noquery, dbapi_catalog_query, dbapi_executor, draft_semantics,
    introspect_postgres, merge_semantics, postgres_dialect,
)

pool = psycopg_pool.ConnectionPool("postgresql://noquery_reader:secret@localhost:5432/app")

graph = introspect_postgres(dbapi_catalog_query(pool.connection), schemas=["public"])
nq = Noquery(
    model=merge_semantics(draft_semantics(graph), overlay),
    dialect=postgres_dialect(),
    executor=dbapi_executor(pool.connection),
)
```

This works with psycopg 3 and psycopg2. `introspect_postgres` also reads views, enum types and table and column comments, which model definitions do not always carry.

#### Plain-English questions

`ask()` needs a planner. The built-in one uses Claude:

```python
from noquery import create_planner
from noquery.integrations.anthropic import anthropic_provider

nq = Noquery(
    model=model,
    dialect=postgres_dialect(),
    executor=sqlalchemy_executor(engine),
    planner=create_planner(
        anthropic_provider(),                 # reads ANTHROPIC_API_KEY from the environment
        instructions="A trip is one continuous movement of a device.",
        examples=[{"question": "Trips by region", "ir": {"version": 1, "measures": ["trips.count"], "dimensions": ["organizations.region"]}}],
    ),
)

result = nq.ask("How many trips per day last week, by device model?", auth=auth, timezone="America/Toronto")
```

The model never writes SQL. It returns a Query IR that can only name fields from this user's catalog, and noquery validates it, applies the row scope and runs it exactly as it would for `run()`.

Each `ask()` makes one Claude API call (two if the first query fails validation and is repaired), which is billed to your Anthropic account. `run()`, `validate()` and `catalog()` never call the model.

`anthropic_provider(client=None, model="claude-opus-5-5", effort=None, max_tokens=16000, fallbacks=True)` accepts your own `anthropic.Anthropic` client. Set `fallbacks=False` on Amazon Bedrock, Google Vertex AI and Microsoft Foundry.

To use another model provider, pass any object with a `name` and a `generate_json(system, messages, schema)` method that returns the parsed JSON answer.

#### Async applications

`Noquery` is blocking and safe to share between threads. In async code, run it in a worker thread:

```python
result = await asyncio.to_thread(nq.ask, question, auth=auth)
```

`sqlalchemy_executor` needs a synchronous `Engine`. If your app uses an `AsyncEngine`, create a small synchronous engine for noquery that points at the same database:

```python
analytics_engine = sa.create_engine("postgresql+psycopg://noquery_reader:secret@localhost:5432/app", pool_size=5)
```

#### Database role and audit log

noquery runs every statement in a read-only transaction with a 15 second timeout (`timeout_ms`) and a row cap (`default_limit`, `max_limit`). A database role that can only read is a second layer worth having:

```sql
CREATE ROLE noquery_reader LOGIN PASSWORD 'change-me';
GRANT USAGE ON SCHEMA public TO noquery_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO noquery_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO noquery_reader;
```

Pass `on_audit` to record every call. The event is a dict with `type` (`run` or `ask`), `at`, `userId`, `roles`, `question`, `ir`, `sql`, `params`, `rowCount`, `durationMs`, `outcome` (`answer`, `clarify`, `unanswerable`, `invalid` or `error`) and, when relevant, `errors` or `error`:

```python
nq = Noquery(model=model, dialect=postgres_dialect(), executor=executor, on_audit=lambda event: logger.info("noquery %s", event))
```

### Using the results

The examples below use `nq`, a `Noquery` instance. A `NoqueryClient` has the same methods and returns the same objects.

#### Ask a question

```python
auth = {"userId": "u-42", "orgId": "acme", "roles": ["analyst"]}

result = nq.ask(
    "How many trips per day last week, by device model?",
    auth=auth,
    timezone="America/Toronto",
)

if result.type == "answer":
    print(result.explanation)   # plain-English description of exactly what ran
    print(result.chart)         # {"type": "line", "x": "...", "y": [...], "series": "..."}
    for row in result.rows:     # keyed by field, e.g. {"devices.model": "X1", "trips.count": 17}
        print(row)
    if result.truncated:
        print("More rows matched than the limit allowed.")
elif result.type == "clarify":
    print(result.question, result.options)   # ask the user, then ask again with their choice
elif result.type == "unanswerable":
    print(result.reason)                     # e.g. "There is no battery temperature field."
elif result.type == "invalid":
    print(result.errors)                     # the planner could not produce a valid query
```

`result.labeled_rows()` returns the same rows keyed by human labels (`{"Model": "X1", "Number of trips": 17}`), which is handy for display.

#### Ask a follow-up question

Pass earlier turns as `history` so the next question can build on the last answer:

```python
question = "Trips per week this quarter by region"
first = nq.ask(question, auth=auth)

follow_up = nq.ask(
    "Now only the east region",
    auth=auth,
    history=[first.to_turn(question)],
)
```

#### Run a query without the model

For fixed dashboards, scheduled reports and tests, send a Query IR directly. This never calls the LLM and gives the same result every time:

```python
result = nq.run(
    {
        "version": 1,
        "measures": ["trips.count", "trips.avg_duration_min"],
        "dimensions": ["organizations.region"],
        "time": {"field": "trips.started_at", "grain": "week", "range": "last_90_days"},
        "filters": [{"field": "devices.status", "op": "eq", "value": "active"}],
        "orderBy": [{"field": "trips.started_at", "dir": "asc"}],
        "limit": 500,
    },
    auth=auth,
)
```

A good workflow: let analysts find the right question with `ask()`, then save `result.ir` and replay it with `run()`.

#### Check a query before running it

```python
check = nq.validate({"version": 1, "measures": ["trips.count"], "dimensions": ["devices.serial"]}, auth=auth)
if not check.ok:
    for error in check.errors:
        print(error["code"], error["message"])
```

#### List what a user can ask about

```python
catalog = nq.catalog(auth)
for entity in catalog["entities"]:
    print(entity["name"], [m["ref"] for m in entity["measures"]], [d["ref"] for d in entity["dimensions"]])
```

Use this to build pickers in a UI, or to show users example questions.

#### Load results into pandas

```bash
pip install "./clients/python[pandas]"
```

```python
df = nq.run(ir, auth=auth).to_dataframe(labels=True)
df.to_csv("weekly_trips.csv", index=False)
```

#### Handle errors

```python
from noquery import (
    AuthenticationError,       # sidecar only: wrong or missing service token (401)
    NoqueryError,              # base class for every noquery error
    PlannerFailedError,        # the Claude call failed: bad API key, rate limit, network (502 or 503)
    PlannerRefusalError,       # the model declined to plan the question (422)
    PlannerUnavailableError,   # no planner configured, or sidecar running with NOQUERY_PLANNER=off (501)
    QueryValidationError,      # unknown field, blocked column, bad filter... (422)
)

try:
    result = nq.ask(question, auth=auth)
except QueryValidationError as error:
    for problem in error.errors:
        print(problem["code"], problem["message"])
except PlannerFailedError as error:
    print(error.reason, error.message)   # e.g. "authentication", "Claude rejected the API credentials (401)..."
    if error.retryable:
        pass                             # e.g. "rate_limited": try again shortly
except NoqueryError as error:
    print(error.status, error.code, error.message)   # status and code match the HTTP API in both modes
```

The same exception classes are raised by `Noquery` and by `NoqueryClient`. `error.status` and `error.code` carry the HTTP API's values in both cases, so one error handler serves both. Database errors (a lost connection, a statement timeout) are raised as your driver's own exceptions when embedded. With `NoqueryClient`, network failures raise the standard library's `urllib.error.URLError` (for example, when the sidecar is not running).

#### Example: scheduled report script

```python
"""Weekly trips report, run from cron. Uses run(), so no model calls and no surprises."""
from myapp.analytics import nq

for org_id in ["acme", "bayside"]:
    result = nq.run(
        {
            "version": 1,
            "measures": ["trips.count", "trips.total_distance_km"],
            "dimensions": ["devices.model"],
            "time": {"field": "trips.started_at", "range": "last_week"},
        },
        auth={"userId": "report-bot", "orgId": org_id, "roles": ["reports"]},
    )
    print(f"== {org_id}: {result.explanation}")
    for row in result.labeled_rows():
        print(row)
```

### Python reference

`Noquery(model, dialect, executor, planner=None, default_timezone="UTC", default_limit=1000, max_limit=10000, timeout_ms=15000, max_repairs=1, on_audit=None)` is the embedded engine. It raises `ValueError` at construction if the semantic model has problems, listing each one.

`NoqueryClient(base_url, token=None, timeout=60.0)` is the client for a sidecar.

| Method | Returns | Notes |
| --- | --- | --- |
| `ask(question, auth, timezone=None, history=None)` | `Result` | Plain-English question. Needs a planner |
| `run(ir, auth)` | `Result` | Runs a Query IR. Raises `QueryValidationError` if invalid |
| `validate(ir, auth)` | `Validation(ok, errors)` | Checks without running |
| `catalog(auth)` | `dict` | Entities, measures and dimensions this caller may use |
| `compile(ir, auth)` | `CompiledQuery(sql, params, paramstyle)` | `Noquery` only. Validates and compiles without running |
| `has_planner` | `bool` | `Noquery` only. Whether `ask` is available |
| `health()` | `dict` | `NoqueryClient` only. `{"ok": True, "planner": bool}`; needs no token |

`Result` fields:

| Field | Meaning |
| --- | --- |
| `type` | `"answer"`, `"clarify"`, `"unanswerable"` or `"invalid"` |
| `rows` | List of dicts keyed by column key (`"trips.count"`). Measures are `int` or `float`; time buckets are strings such as `"2026-03-01"` |
| `columns` | List of `Column(key, ref, role, type, label, grain, format)`; `role` is `time`, `dimension` or `measure` |
| `explanation` | One sentence describing what ran, generated from the query itself |
| `chart` | Suggested chart: `{"type": "kpi" or "line" or "bar" or "scatter" or "table", "x", "y", "series"}` |
| `ir`, `sql`, `params` | The query object, the SQL that ran and its bound parameters |
| `truncated` | `True` when more rows matched than `limit` |
| `duration_ms` | How long the call took |
| `question`, `options` | For `clarify` |
| `reason` | For `unanswerable` |
| `errors` | For `invalid` |
| `labeled_rows()`, `to_dataframe(labels=False)`, `to_turn(question)`, `to_dict()` | Helpers. `to_dict()` gives the JSON shape of the HTTP API |

Building blocks for the embedded engine:

| Function | Module | What it does |
| --- | --- | --- |
| `introspect_sqlalchemy(source, include=None, exclude=None)` | `noquery.integrations.sqlalchemy` | Reads a declarative base, `MetaData`, tables or mapped classes into a schema graph |
| `sqlalchemy_executor(engine)` | `noquery.integrations.sqlalchemy` | Runs queries on a synchronous SQLAlchemy `Engine` |
| `introspect_django(app_labels=None, include=None, exclude=None, using="default")` | `noquery.integrations.django` | Reads Django models into a schema graph |
| `django_executor(using="default")` | `noquery.integrations.django` | Runs queries on a Django database connection |
| `noquery_urls(nq, get_auth)` | `noquery.integrations.django` | URL patterns for the API |
| `create_router(nq, get_auth)` | `noquery.integrations.fastapi` | A FastAPI router for the API |
| `anthropic_provider(...)` | `noquery.integrations.anthropic` | Planner provider backed by Claude |
| `introspect_postgres(query, schemas=("public",), include_views=True)` | `noquery` | Reads the Postgres catalog into a schema graph |
| `dbapi_executor(connect)`, `dbapi_catalog_query(connect)` | `noquery` | Executor and catalog reader for psycopg and psycopg2 |
| `draft_semantics(graph, sensitive_patterns=None, exclude_tables=None, time_preference=None)` | `noquery` | Drafts a semantic model from a schema graph |
| `merge_semantics(base, overlay)` | `noquery` | Applies your overlay to a model |
| `check_semantics(model)` | `noquery` | Lists problems in a model; empty when sound |
| `postgres_dialect(paramstyle="pyformat", probe_truncation=True)` | `noquery` | The PostgreSQL dialect. `paramstyle="numeric"` writes `$1` placeholders, for use with `compile()` and drivers such as asyncpg |
| `create_planner(provider, examples=None, instructions=None)` | `noquery` | Wraps a model provider as a planner |

Each integration module imports its framework, so import only the ones you use.

### Query IR cheat sheet

```python
{
    "version": 1,
    "measures": ["trips.count"],                    # what to count or add up; all from one entity
    "dimensions": ["devices.model"],                # how to split it
    "time": {
        "field": "trips.started_at",
        "grain": "day",                             # hour, day, week, month, quarter, year (optional)
        "range": "last_7_days",                     # or {"from": "2026-03-01", "to": "2026-04-01"} (to is exclusive)
        "tz": "America/Toronto",                    # optional
    },
    "filters": [
        {"field": "devices.status", "op": "in", "value": ["active", "inactive"]},
        {"field": "trips.count", "op": "gt", "value": 10},   # a filter on a measure filters the totals
    ],
    "orderBy": [{"field": "trips.count", "dir": "desc"}],
    "limit": 100,
}
```

Range presets: `today`, `yesterday`, `last_7_days`, `last_14_days`, `last_30_days`, `last_90_days`, `this_week`, `last_week`, `this_month`, `last_month`, `this_quarter`, `last_quarter`, `this_year`, `last_year`.

Filter operators: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `contains`, `starts_with`, `is_null`, `is_not_null`. `neq` and `not_in` keep empty values.

### Sidecar: noquery as a separate service

Use this when you would rather not run the engine in your Python process, or when several services share it.

#### How the sidecar works

You run the TypeScript engine as a small HTTP service next to your application (a "sidecar"), and your Python code calls it with `NoqueryClient` from the same `noquery` Python package.

```
 Your Python app (script, FastAPI, Django, Flask, notebook)
        |
        |  NoqueryClient.ask(question, auth={...})
        |  HTTP + service token + the logged-in user's auth context
        v
 noquery-server (Node.js sidecar)
        |-- planner (Claude): question -> Query IR        [only for ask()]
        |-- validator + row scope + column policies
        |-- Postgres SQL, bound parameters
        v
 Your Postgres database (read-only transactions)
```

Who does what:

- **Your Python app** logs users in, as it does today. On every call it passes the verified user's identity as `auth`, for example `{"userId": "u-42", "orgId": "acme", "roles": ["analyst"]}`.
- **The sidecar** enforces everything: it only lets that user see rows for `orgId = "acme"`, hides restricted columns, and rejects anything the semantic layer does not define.
- **The service token** proves to the sidecar that the call comes from your backend. The sidecar trusts the `auth` it receives, so it must only be reachable by your backend, never directly by end users or browsers.

#### Sidecar requirements

| Need | Version | Used for |
| --- | --- | --- |
| Python | 3.9 or newer | The client. It has no third-party dependencies |
| Node.js | 20 or newer | Running the sidecar |
| pnpm | 12 (run `corepack enable` once and Node installs the pinned version) | Installing and building the sidecar from this repository |
| PostgreSQL | any supported version | Your data. Not needed for the demo |
| Anthropic API credentials | | Only for `ask()` (plain-English questions). `run()` works without them |
| pandas | optional | `Result.to_dataframe()` |

#### Step 1: Build noquery

From the root of this repository:

```bash
pnpm install
```

```bash
pnpm build
```

#### Step 2: Run the demo sidecar

The example sidecar in `examples/sidecar` ships with a small in-memory fleet-telemetry database, so you can try everything before connecting your own data.

Create a service token. Your Python code sends it to the sidecar, so keep it secret, like a password:

```bash
export NOQUERY_SERVICE_TOKEN=$(openssl rand -hex 24)
```

Start the sidecar. `NOQUERY_PLANNER=off` runs it without an LLM, so no API key is needed yet:

```bash
NOQUERY_PLANNER=off NOQUERY_TIMEZONE=America/Toronto pnpm --filter noquery-sidecar-example start
```

You should see:

```
noquery-server listening on http://127.0.0.1:8417 (planner: off)
```

#### Step 3: Install the Python client and run the example

In a second terminal, from the repository root, with the same `NOQUERY_SERVICE_TOKEN` exported:

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
pip install ./clients/python
```

```bash
python examples/sidecar/example.py
```

Expected output (numbers vary with the current date):

```
health: {'ok': True, 'planner': False}
devices: devices.count, devices.active_count
organizations: organizations.count
trips: trips.count, trips.total_distance_m, trips.avg_distance_m, trips.avg_duration_min, trips.total_distance_km

Number of trips and total distance (km) by model in the last 7 days (America/Toronto). Sorted by number of trips, highest first.
{'Model': 'X1', 'Number of trips': 17, 'Total distance (km)': 18.35}
{'Model': 'X2', 'Number of trips': 9, 'Total distance (km)': 9.225}
{'Model': 'X3', 'Number of trips': 8, 'Total distance (km)': 7.904}

rejected: ['"devices.serial" can only be counted, not grouped, filtered or listed.']

ask() skipped: the sidecar is running without a planner.
```

To try plain-English questions, stop the sidecar, make your Anthropic credentials available (`export ANTHROPIC_API_KEY=...`, or sign in once with `ant auth login`), and start it again without `NOQUERY_PLANNER=off`:

```bash
NOQUERY_TIMEZONE=America/Toronto pnpm --filter noquery-sidecar-example start
```

Each `ask()` makes one Claude API call (two if the first query fails validation and is repaired), which is billed to your Anthropic account. `run()`, `validate()` and `catalog()` never call the model.

For a quick one-off check of the whole path (Python client, sidecar, Claude, database), this script starts the demo sidecar with the planner on, asks one question, prints the planned query, the explanation and the rows, then stops the sidecar:

```bash
ANTHROPIC_API_KEY=your-key bash examples/sidecar/run-live-ask.sh "Which device model had the most trips last week?"
```

#### Step 4: Point the sidecar at your database

1. Copy `examples/sidecar` to a folder for your project, for example `examples/my-app`, and change `"name"` in its `package.json` to `my-app-noquery`. Keep it inside this repository for now so it can use the local packages.
2. Run `pnpm install` from the repository root so the new folder is linked.
3. Edit the `overlay` in `noquery.config.mjs` to describe your data (see [Semantic layer](#semantic-layer)). At minimum, add a `rowScope` to every table that belongs to a tenant, user or organization.
4. Start it with your connection string:

```bash
DATABASE_URL=postgres://noquery_reader:secret@localhost:5432/app NOQUERY_TIMEZONE=America/Toronto pnpm --filter my-app-noquery start
```

The sidecar reads your tables, columns, keys and enums from Postgres when it starts, drafts a semantic model, then applies your overlay. Restart it after schema changes.

Sidecar settings (environment variables):

| Variable | Default | Meaning |
| --- | --- | --- |
| `NOQUERY_SERVICE_TOKEN` | required | Shared secret, at least 16 characters. Callers send `Authorization: Bearer <token>` |
| `DATABASE_URL` | demo database | Postgres connection string. Prefer a role with read-only grants |
| `NOQUERY_SCHEMAS` | `public` | Comma-separated Postgres schemas to expose |
| `NOQUERY_TIMEZONE` | `UTC` | Default time zone for "today", "last week" and time buckets |
| `NOQUERY_PLANNER` | on | Set to `off` to disable `ask()` |
| `ANTHROPIC_API_KEY` | | Credentials for the planner |
| `PORT` | `8417` | Listening port |
| `HOST` | `127.0.0.1` | Listening address. Keep it private (localhost or an internal network) |

A read-only Postgres role for the sidecar:

```sql
CREATE ROLE noquery_reader LOGIN PASSWORD 'change-me';
GRANT USAGE ON SCHEMA public TO noquery_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO noquery_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO noquery_reader;
```

#### Step 5: Use it from your Python code


```python
import os
from noquery import NoqueryClient

client = NoqueryClient(
    "http://127.0.0.1:8417",
    token=os.environ["NOQUERY_SERVICE_TOKEN"],
    timeout=60,  # seconds; ask() includes a model call
)
```

Create one client and reuse it. It is safe to share across threads.

From here on it works exactly like the embedded engine: see [Using the results](#using-the-results), with `client` in place of `nq`.

#### Example: FastAPI endpoint that calls the sidecar

```python
import dataclasses
import os

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from noquery import NoqueryClient, QueryValidationError

from myapp.auth import get_current_user  # your existing auth dependency

app = FastAPI()
noquery = NoqueryClient("http://127.0.0.1:8417", token=os.environ["NOQUERY_SERVICE_TOKEN"])


class AskBody(BaseModel):
    question: str


@app.post("/analytics/ask")
def ask(body: AskBody, user=Depends(get_current_user)):
    # Build auth from the verified session only. Never accept orgId or roles from the request body.
    auth = {"userId": str(user.id), "orgId": user.org_id, "roles": user.roles}
    try:
        result = noquery.ask(body.question, auth=auth, timezone=user.timezone)
    except QueryValidationError as error:
        raise HTTPException(status_code=422, detail=error.errors)

    if result.type != "answer":
        return {"type": result.type, "question": result.question, "options": result.options, "reason": result.reason}
    return {
        "type": "answer",
        "explanation": result.explanation,
        "columns": [dataclasses.asdict(c) for c in result.columns],
        "rows": result.rows,
        "chart": result.chart,
        "truncated": result.truncated,
    }
```

The endpoint is a plain `def` because the client is blocking; FastAPI runs it in a worker thread.

#### Example: Django view that calls the sidecar

```python
import json
import os

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from noquery import NoqueryClient

noquery = NoqueryClient("http://127.0.0.1:8417", token=os.environ["NOQUERY_SERVICE_TOKEN"])


@login_required
@require_POST
def ask(request):
    question = json.loads(request.body)["question"]
    auth = {
        "userId": str(request.user.id),
        "orgId": request.user.profile.org_id,  # however your app stores the user's organization
        "roles": list(request.user.groups.values_list("name", flat=True)),
    }
    result = noquery.ask(question, auth=auth, timezone="America/Toronto")
    if result.type == "answer":
        return JsonResponse({"explanation": result.explanation, "rows": result.labeled_rows(), "chart": result.chart})
    return JsonResponse({"type": result.type, "question": result.question, "options": result.options, "reason": result.reason})
```

#### Deploying the sidecar

- Run it on the same host or private network as your Python app. Keep `HOST=127.0.0.1` when they share a host.
- Never expose it to the internet or to browsers: it trusts the `auth` your backend sends.
- Store `NOQUERY_SERVICE_TOKEN` in your secret manager and rotate it like any other credential.
- Connect with a read-only database role. noquery also runs every statement in a `READ ONLY` transaction with a 15 second timeout.
- `GET /healthz` is unauthenticated and suitable for container health checks.
- The sidecar logs one audit line per request (user, outcome, duration). Ship them to your log system.

### Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `ValueError: Semantic model has problems` when creating `Noquery` | The message lists each problem with its path. Usually an overlay names a field without a `type`, or a measure without `agg` or `sql` |
| `ValueError: Overlay adds entity "x" without a table` or `New dimension in "x" needs a type` | The overlay names a table or column the draft does not have. With Django, use the database table and column names (`fleet_trip`, `org_id`) |
| `ValueError: ... needs postgres_dialect(paramstyle="pyformat")` | The Python executors need the default `postgres_dialect()`. `paramstyle="numeric"` is for `compile()` only |
| `TypeError: sqlalchemy_executor() needs a synchronous Engine` | You passed an `AsyncEngine`. See [Async applications](#async-applications) |
| `RuntimeError: django_executor needs a PostgreSQL database` | The Django database alias uses another backend. PostgreSQL is the only supported database |
| `PlannerFailedError` with reason `authentication` from `Noquery.ask()` | No Anthropic key was found, or Claude rejected it. Set `ANTHROPIC_API_KEY` in the environment of the process that runs noquery |
| `PlannerUnavailableError` from `Noquery.ask()` | No `planner` was passed to `Noquery`. See [Plain-English questions](#plain-english-questions) |
| A statement timeout error from your driver | The query ran longer than `timeout_ms` (15 seconds by default). Narrow the time range, or raise `timeout_ms` |
| `Port 8417 on 127.0.0.1 is already in use` | Another program has the port. Start the sidecar with `PORT=8418` and use `http://127.0.0.1:8418` in `NoqueryClient` |
| `NoqueryError: Unexpected response from ... Is the noquery sidecar running at this address?` | Something other than noquery answered on that URL. Check the port and that the sidecar printed `listening` |
| `URLError: Connection refused` | The sidecar is not running, or is listening on a different host or port |
| `AuthenticationError` (401) | The token in your Python process differs from the sidecar's `NOQUERY_SERVICE_TOKEN` |
| `QueryValidationError` with `missing_auth_attribute` | A table has a `rowScope` whose `authKey` (for example `orgId`) is missing from `auth` |
| `PlannerUnavailableError` (501) | The sidecar was started with `NOQUERY_PLANNER=off`. Use `run()`, or restart it with the planner on |
| `PlannerFailedError` with reason `authentication` | The sidecar has no valid Anthropic key. Set `ANTHROPIC_API_KEY` in the sidecar's environment, not the Python process's |
| `PlannerFailedError` with reason `rate_limited` or `unreachable` | Temporary. `error.retryable` is true; try again shortly, or check the sidecar's network access |
| Sidecar exits with `Overlay adds entity "x" without a table` or `New dimension in "x" needs a type` | The overlay names a table or column the sidecar did not find. Check spelling and `NOQUERY_SCHEMAS` |

## Using noquery from TypeScript

In a Node backend you can embed the engine directly, with no sidecar:

```ts
import { createEngine, createPlanner, draftSemantics, mergeSemantics } from "@noquery/core";
import { introspectDrizzle } from "@noquery/drizzle";
import { pgExecutor, postgresDialect } from "@noquery/postgres";
import { anthropicProvider } from "@noquery/anthropic";
import * as schema from "./db/schema";
import { pool } from "./db";

const model = mergeSemantics(draftSemantics(introspectDrizzle(schema)), {
  entities: {
    trips: { rowScope: [{ column: "org_id", authKey: "orgId" }] },
    devices: { rowScope: [{ column: "org_id", authKey: "orgId" }], dimensions: { serial: { policy: "aggregate_only" } } },
  },
});

const engine = createEngine({
  model,
  dialect: postgresDialect(),
  executor: pgExecutor(pool),
  planner: createPlanner(anthropicProvider()),
  defaultTimezone: "America/Toronto",
  onAudit: (event) => logger.info(event),
});

const result = await engine.ask("How many trips per day last week, by device model?", {
  auth: { userId: user.id, orgId: user.orgId, roles: user.roles },
});
```

`engine.run(ir, { auth })`, `engine.validate(ir, auth)` and `engine.catalog(auth)` mirror the Python package.

## Semantic layer

noquery drafts a model from your tables (read from your ORM models or from the database): one entity per table, a dimension per useful column, `count` plus sum and average measures for numeric columns, joins from foreign keys, and likely-sensitive columns (emails, phone numbers, names, addresses) hidden. The overlay is where you correct and enrich it: the second argument of `merge_semantics` in Python and `mergeSemantics` in TypeScript, or `overlay` in the sidecar's `noquery.config.mjs`. Names in the overlay must match tables and columns the draft found; a typo stops noquery at startup with an error rather than being ignored. The example below assumes tables called `trips`, `devices`, `billing_events` and `audit_log`:

```js
overlay = {
  entities: {
    trips: {
      description: "One continuous movement of a device.",   // helps the planner
      synonyms: ["journeys", "rides"],
      rowScope: [{ column: "org_id", authKey: "orgId" }],    // WHERE trips.org_id = auth.orgId
      dimensions: {
        internal_notes: null,                                 // remove a field entirely
        status: { enumValues: ["planned", "done"] },          // tighten filters
      },
      measures: {
        avg_duration_min: {
          agg: "avg",
          sql: "extract(epoch from ${TABLE}.ended_at - ${TABLE}.started_at) / 60",
          label: "Average duration (min)",
        },
      },
    },
    devices: {
      rowScope: [{ column: "org_id", authKey: "orgId" }],
      dimensions: { serial: { policy: "aggregate_only" } },  // can be counted, never listed
    },
    billing_events: { roles: ["finance"] },                   // only callers with the finance role
    audit_log: null,                                          // hide a whole table
  },
};
```

Each `authKey` names a key the caller must send in `auth`. If it is missing, the query is refused. An `auth` value may be a list (`{"orgId": ["acme", "bayside"]}`) for users who belong to several tenants.

The overlay has the same shape in every language. In Python it is a dict, with `None` where JavaScript uses `null`:

```python
overlay = {
    "entities": {
        "trips": {
            "description": "One continuous movement of a device.",
            "synonyms": ["journeys", "rides"],
            "rowScope": [{"column": "org_id", "authKey": "orgId"}],
            "dimensions": {"internal_notes": None, "status": {"enumValues": ["planned", "done"]}},
            "measures": {
                "avg_duration_min": {
                    "agg": "avg",
                    "sql": "extract(epoch from ${TABLE}.ended_at - ${TABLE}.started_at) / 60",
                    "label": "Average duration (min)",
                },
            },
        },
        "billing_events": {"roles": ["finance"]},
        "audit_log": None,
    },
}
```

Overlay reference:

| Key | On | Meaning |
| --- | --- | --- |
| `rowScope` | entity | List of `{column, authKey}`. Adds `column = auth[authKey]` to every query that touches the entity |
| `roles` | entity, dimension, measure | Only callers with one of these roles can see it |
| `policy` | dimension | `visible` (default), `hidden`, or `aggregate_only` (can be counted, never grouped, filtered or listed) |
| `label`, `description`, `synonyms` | entity, dimension, measure | Shown to users and to the planner |
| `enumValues` | dimension | Allowed values; filters are checked against them |
| `type`, `column` or `sql` | dimension | `type` is `string`, `number`, `boolean` or `time`. `sql` is a trusted expression; `${TABLE}` is the table alias |
| `timeStorage` | time dimension | `instant` (default, `timestamptz`), `utc` (timestamp without time zone holding UTC) or `local` (a wall-clock value such as a `date`) |
| `agg`, `column` or `sql`, `format` | measure | `agg` is `count`, `count_distinct`, `sum`, `avg`, `min` or `max`. Omit it when `sql` is itself an aggregate |
| `defaultTime` | entity | The time dimension used when a question gives a period but no field |
| `joins`, `removeJoins` | overlay root | Add `{from, to, on: [[fromColumn, toColumn]], relationship}` or drop a drafted join by `{from, to}` |

The `sql` in a dimension or measure is inserted into the query as written. It must come from your own code, never from a user.

## Safety model

Everything is enforced in code; nothing depends on the model behaving.

- **Typed IR.** The planner's output schema lists your fields as enums. Anything else fails validation.
- **Row scope.** `rowScope` adds `alias.column = $n` from the auth context to every entity a query touches, joined ones included. A missing auth value fails closed.
- **Column policies.** `hidden` fields are invisible; `aggregate_only` fields can be counted but never grouped, filtered or listed; `roles` gate entities and fields.
- **No fan-out.** Joins are only followed many-to-one, so totals cannot be silently inflated.
- **Execution.** Bound parameters only, quoted identifiers, `READ ONLY` transactions, statement timeouts, row caps with truncation reporting.
- **Audit.** `onAudit` (`on_audit` in Python) receives question, IR, SQL, params, row count and outcome for every call.

## HTTP API

This is the sidecar's API. The FastAPI router and the Django URL patterns in the Python package serve the same payloads and error bodies, with two differences: `auth` comes from your own session instead of the request body, and the catalog is `GET /catalog`.

Any language can call the sidecar. Every `/v1` endpoint is `POST` with a JSON body, `Content-Type: application/json`, and `Authorization: Bearer <NOQUERY_SERVICE_TOKEN>`. Every body includes `auth`.

| Endpoint | Body | Response |
| --- | --- | --- |
| `GET /healthz` | | `{"ok": true, "planner": true}` |
| `POST /v1/catalog` | `{auth}` | Catalog |
| `POST /v1/ask` | `{auth, question, timezone?, history?}` | `{type: "answer", rows, columns, ...}` or `{type: "clarify" or "unanswerable" or "invalid", ...}` |
| `POST /v1/run` | `{auth, ir}` | `{rows, columns, sql, params, explanation, chart, truncated, ...}` |
| `POST /v1/validate` | `{auth, ir}` | `{ok: true, query}` or `{ok: false, errors}` |

Errors return `{"error": {"code", "message", "errors"?}}` with status 400 (bad request), 401 (token), 404, 405, 413 (body over 1 MB), 415, 422 (`invalid_query` or `planner_refused`), 501 (`planner_not_configured`), 502 or 503 (`planner_failed`, with `reason` and `retryable`; 503 means retrying may help) or 500 (`internal`; details are logged, never returned).

```bash
curl -s http://127.0.0.1:8417/v1/run -H "Authorization: Bearer $NOQUERY_SERVICE_TOKEN" -H "Content-Type: application/json" -d '{"auth": {"orgId": "acme"}, "ir": {"version": 1, "measures": ["trips.count"]}}'
```

## Development

```bash
pnpm install
```

```bash
pnpm typecheck
```

```bash
pnpm test
```

The TypeScript Postgres tests run against [PGlite](https://pglite.dev) in-process; no database needed.

Python tests:

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
pip install -e "./clients/python[dev]"
```

```bash
pytest clients/python
```

| Python test file | Covers |
| --- | --- |
| `test_parity.py` | The Python engine against the TypeScript engine: validation, errors, SQL, parameters, explanations, charts, catalog, planner prompts, drafting and merging |
| `test_semantic.py`, `test_resolve.py`, `test_postgres_compiler.py`, `test_engine.py`, `test_results.py` | Python-specific behavior of the core |
| `test_executors.py`, `test_sqlalchemy.py`, `test_django.py`, `test_fastapi.py`, `test_http.py`, `test_anthropic.py` | Each integration, with the database and the model API faked |
| `test_postgres_integration.py` | Every executor (SQLAlchemy with psycopg and psycopg2, Django, plain psycopg and psycopg2) and every introspection path against a real PostgreSQL |
| `test_client.py` | The sidecar client |

The integration tests need a real PostgreSQL. The `dev` extra installs [pgserver](https://pypi.org/project/pgserver/), which runs a private PostgreSQL from a temporary folder, so they work with no setup on macOS and Linux. To use your own server instead, set `NOQUERY_TEST_DATABASE_URL` to a role that may create databases; the tests create a database named `noquery_test_...` and drop it afterwards. With neither, the integration tests are skipped and the rest still run. Test modules for frameworks that are not installed are skipped too.

Keeping the two engines identical: `packages/postgres/test/parity.test.ts` records what the TypeScript engine produces for a fixed set of inputs in `clients/python/tests/fixtures/parity.json`, and `test_parity.py` requires the Python engine to produce the same. After an intended change to engine behavior, regenerate the file and port the change to Python:

```bash
NOQUERY_UPDATE_PARITY=1 pnpm test
```

To run the live Claude planner tests (billed to your Anthropic account): `NOQUERY_LIVE_ANTHROPIC=1 pnpm test` for TypeScript and `NOQUERY_LIVE_ANTHROPIC=1 pytest clients/python/tests/test_anthropic.py` for Python.

## License

Not yet chosen (Apache-2.0 proposed). Add a `LICENSE` file before publishing.
