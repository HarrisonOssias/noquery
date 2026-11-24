# noquery (Python)

Natural-language analytics for your own PostgreSQL database. Plug in SQLAlchemy or Django, describe what your data means, and let people ask questions in plain English, safely.

The model never writes SQL. It fills in a small typed query object that can only name fields your semantic layer defines. noquery validates it, applies your row-level and column policies, compiles it to SQL with bound parameters, and runs it read-only on your own connection.

The package gives you two classes with the same methods and results:

- `Noquery`: the engine, embedded in your Python process. No other service to run.
- `NoqueryClient`: a client for a `noquery-server` sidecar, for setups where the engine runs as a separate service.

Python 3.9 or newer. The core uses the standard library only; integrations are optional extras.

## Install

Nothing is published to PyPI yet. From the noquery repository root, pick the extras for your stack:

```bash
pip install "./clients/python[sqlalchemy,fastapi,anthropic]"
```

```bash
pip install "./clients/python[django,anthropic]"
```

| Extra | Adds | Needed for |
| --- | --- | --- |
| `sqlalchemy` | SQLAlchemy 2.0 or newer | `noquery.integrations.sqlalchemy` |
| `django` | Django 4.2 or newer | `noquery.integrations.django` |
| `fastapi` | FastAPI | `noquery.integrations.fastapi` |
| `anthropic` | The Anthropic SDK (Python 3.10 or newer) | `noquery.integrations.anthropic`, for plain-English questions |
| `pandas` | pandas | `Result.to_dataframe()` |
| `dev` | Everything above plus pytest, both psycopg drivers and pgserver | Running the tests |

You also need a Postgres driver, psycopg 3 or psycopg2, which a SQLAlchemy or Django app on PostgreSQL already has.

## Use with SQLAlchemy and FastAPI

```python
from fastapi import Depends, FastAPI
from noquery import Noquery, create_planner, draft_semantics, merge_semantics, postgres_dialect
from noquery.integrations.anthropic import anthropic_provider
from noquery.integrations.fastapi import create_router
from noquery.integrations.sqlalchemy import introspect_sqlalchemy, sqlalchemy_executor

from myapp.auth import get_current_user
from myapp.db import Base, engine

model = merge_semantics(
    draft_semantics(introspect_sqlalchemy(Base)),
    {"entities": {"trips": {"rowScope": [{"column": "org_id", "authKey": "orgId"}]}}},
)

nq = Noquery(
    model=model,
    dialect=postgres_dialect(),
    executor=sqlalchemy_executor(engine),
    planner=create_planner(anthropic_provider()),   # optional; needed only for ask()
    default_timezone="America/Toronto",
)


def get_auth(user=Depends(get_current_user)):
    return {"userId": str(user.id), "orgId": str(user.org_id), "roles": user.roles}


app = FastAPI()
app.include_router(create_router(nq, get_auth), prefix="/analytics")
```

Or call it directly:

```python
result = nq.ask("How many trips per day last week, by device model?", auth=auth, timezone="America/Toronto")
if result.type == "answer":
    print(result.explanation)
    for row in result.labeled_rows():
        print(row)
```

## Use with Django

```python
from noquery import Noquery, draft_semantics, merge_semantics, postgres_dialect
from noquery.integrations.django import django_executor, introspect_django

model = merge_semantics(
    draft_semantics(introspect_django(app_labels=["fleet"])),
    {"entities": {"fleet_trip": {"rowScope": [{"column": "org_id", "authKey": "orgId"}]}}},
)
nq = Noquery(model=model, dialect=postgres_dialect(), executor=django_executor())

result = nq.run({"version": 1, "measures": ["fleet_trip.count"]}, auth={"orgId": str(org_id)})
```

Build it after Django has loaded your apps, for example on first use.

## Use with a sidecar

```python
import os
from noquery import NoqueryClient

client = NoqueryClient("http://127.0.0.1:8417", token=os.environ["NOQUERY_SERVICE_TOKEN"])
result = client.ask("How many trips last week?", auth={"userId": "u-42", "orgId": "acme", "roles": ["analyst"]})
```

## Modules

| Module | Contents |
| --- | --- |
| `noquery` | `Noquery`, `NoqueryClient`, `Result`, errors, `draft_semantics`, `merge_semantics`, `check_semantics`, `postgres_dialect`, `introspect_postgres`, `dbapi_executor`, `create_planner` |
| `noquery.integrations.sqlalchemy` | `introspect_sqlalchemy`, `sqlalchemy_executor` |
| `noquery.integrations.django` | `introspect_django`, `django_executor`, `noquery_urls` |
| `noquery.integrations.fastapi` | `create_router` |
| `noquery.integrations.anthropic` | `anthropic_provider` |

## Tests

```bash
pip install -e "./clients/python[dev]"
```

```bash
pytest clients/python
```

The integration tests run every executor against a real PostgreSQL, started for you by pgserver, or the server named in `NOQUERY_TEST_DATABASE_URL`. `tests/test_parity.py` checks the Python engine against the TypeScript engine's recorded output.

## Full guide

The main [README](../../README.md#using-noquery-from-python) covers the semantic layer, the safety model, FastAPI and Django in detail, async applications, the Query IR, error handling, the sidecar and troubleshooting.
