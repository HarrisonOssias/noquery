"""noquery: natural-language analytics for any backend.

Two ways to use it from Python, with the same methods and result types:

- `Noquery`: the engine embedded in your own process, on your own database connection.
- `NoqueryClient`: an HTTP client for a `noquery-server` sidecar.
"""

from .catalog import build_catalog, catalog_to_text
from .chart import suggest_chart
from .client import NoqueryClient
from .draft import draft_semantics, humanize
from .engine import Noquery
from .errors import (
    AuthenticationError,
    NoqueryError,
    PlannerFailedError,
    PlannerRefusalError,
    PlannerUnavailableError,
    QueryValidationError,
)
from .executors import dbapi_catalog_query, dbapi_executor
from .explain import explain_query
from .ir import FILTER_OPS, TIME_GRAINS, TIME_PRESETS
from .planner import Planner, create_planner, planner_schema, to_planner_result
from .postgres import PostgresDialect, introspect_postgres, postgres_dialect
from .resolve import has_access, plan_joins, resolve_query
from .results import Column, Result, Validation
from .runtime import CompiledQuery, ExecuteResult
from .semantic import check_semantics, merge_semantics

__all__ = [
    "AuthenticationError",
    "Column",
    "CompiledQuery",
    "ExecuteResult",
    "FILTER_OPS",
    "Noquery",
    "NoqueryClient",
    "NoqueryError",
    "Planner",
    "PlannerFailedError",
    "PlannerRefusalError",
    "PlannerUnavailableError",
    "PostgresDialect",
    "QueryValidationError",
    "Result",
    "TIME_GRAINS",
    "TIME_PRESETS",
    "Validation",
    "build_catalog",
    "catalog_to_text",
    "check_semantics",
    "create_planner",
    "dbapi_catalog_query",
    "dbapi_executor",
    "draft_semantics",
    "explain_query",
    "has_access",
    "humanize",
    "introspect_postgres",
    "merge_semantics",
    "plan_joins",
    "planner_schema",
    "postgres_dialect",
    "resolve_query",
    "suggest_chart",
    "to_planner_result",
]
__version__ = "0.1.0"
