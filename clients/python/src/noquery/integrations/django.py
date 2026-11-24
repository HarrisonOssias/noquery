"""Django integration: read your models into a schema graph, run queries on a Django database
connection, and expose the API as URL patterns.

Install with `pip install "noquery[django]"`. Needs the PostgreSQL database backend.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from django.apps import apps
from django.db import connections, transaction
from django.http import HttpRequest, JsonResponse
from django.urls import path
from django.views.decorators.http import require_http_methods

from ..executors import require_pyformat, rows_from_cursor, timeout_statement
from ..http import handle
from ..runtime import CompiledQuery, ExecuteResult, finish
from ..schema import SchemaGraph

_KIND_BY_INTERNAL_TYPE = {
    "AutoField": "integer",
    "BigAutoField": "integer",
    "SmallAutoField": "integer",
    "IntegerField": "integer",
    "BigIntegerField": "integer",
    "SmallIntegerField": "integer",
    "PositiveIntegerField": "integer",
    "PositiveBigIntegerField": "integer",
    "PositiveSmallIntegerField": "integer",
    "FloatField": "number",
    "DecimalField": "number",
    "BooleanField": "boolean",
    "NullBooleanField": "boolean",
    "CharField": "string",
    "TextField": "string",
    "SlugField": "string",
    "EmailField": "string",
    "URLField": "string",
    "FilePathField": "string",
    "FileField": "string",
    "ImageField": "string",
    "GenericIPAddressField": "string",
    "IPAddressField": "string",
    "UUIDField": "uuid",
    "DateTimeField": "timestamp",
    "DateField": "date",
    "JSONField": "json",
}


def introspect_django(
    app_labels: Optional[Iterable[str]] = None,
    include: Optional[Iterable[str]] = None,
    exclude: Optional[Iterable[str]] = None,
    using: str = "default",
) -> SchemaGraph:
    """Reads Django models into a schema graph. Call it after `django.setup()`.

    Args:
        app_labels: Only read models from these apps. By default every installed app is read
            except Django's own (`django.contrib.*`: users, sessions, admin and so on); name those
            apps here if you want them.
        include: Keep only these database table names.
        exclude: Leave out these database table names.
        using: The database alias, used to spell column types.
    """
    labels = set(app_labels) if app_labels is not None else None
    keep = set(include) if include is not None else None
    drop = set(exclude or [])
    connection = connections[using]

    tables: List[Dict[str, Any]] = []
    seen = set()
    for model in apps.get_models(include_auto_created=True):
        meta = model._meta
        if meta.proxy or meta.swapped or meta.db_table in seen:
            continue
        if labels is not None:
            if meta.app_label not in labels:
                continue
        elif model.__module__.startswith("django.contrib."):
            continue
        if keep is not None and meta.db_table not in keep:
            continue
        if meta.db_table in drop:
            continue
        seen.add(meta.db_table)

        columns: List[Dict[str, Any]] = []
        foreign_keys: List[Dict[str, Any]] = []
        for field in meta.local_concrete_fields:
            kind = _kind_of(field)
            info: Dict[str, Any] = {
                "name": field.column,
                "kind": kind,
                "dbType": _db_type(field, connection),
                "nullable": bool(field.null),
            }
            if field.primary_key:
                info["primaryKey"] = True
            choices = [value for value, _label in (getattr(field, "flatchoices", None) or [])]
            if kind == "string" and choices and all(isinstance(v, str) for v in choices):
                info["kind"] = "enum"
                info["enumValues"] = list(choices)
            if kind == "timestamp":
                # The PostgreSQL backend stores DateTimeField as `timestamp with time zone`.
                info["withTimezone"] = True
            comment = getattr(field, "db_comment", None) or str(field.help_text or "")
            if comment:
                info["comment"] = comment
            columns.append(info)

            related = field.related_model if (field.many_to_one or field.one_to_one) else None
            if related is not None and not isinstance(related, str):
                foreign_keys.append(
                    {
                        "columns": [field.column],
                        "referencedTable": related._meta.db_table,
                        "referencedColumns": [field.target_field.column],
                    }
                )

        entry: Dict[str, Any] = {"name": meta.db_table, "columns": columns, "foreignKeys": foreign_keys}
        table_comment = getattr(meta, "db_table_comment", None)
        if table_comment:
            entry["comment"] = table_comment
        tables.append(entry)
    return {"tables": tables}


def _kind_of(field: Any) -> str:
    if field.many_to_one or field.one_to_one:
        target = getattr(field, "target_field", None)
        if target is not None and target is not field:
            return _kind_of(target)
    return _KIND_BY_INTERNAL_TYPE.get(field.get_internal_type(), "unknown")


def _db_type(field: Any, connection: Any) -> str:
    try:
        return str(field.db_type(connection) or field.get_internal_type())
    except Exception:
        return field.get_internal_type()


class DjangoExecutor:
    """Runs each query read-only, with a statement timeout, inside `transaction.atomic`."""

    def __init__(self, using: str = "default"):
        self.using = using

    def execute(self, compiled: CompiledQuery, timeout_ms: int, max_rows: int) -> ExecuteResult:
        params = require_pyformat(compiled, "django_executor")
        connection = connections[self.using]
        if connection.vendor != "postgresql":
            raise RuntimeError(f'django_executor needs a PostgreSQL database; "{self.using}" is {connection.vendor}.')
        with transaction.atomic(using=self.using):
            with connection.cursor() as cursor:
                # SET LOCAL is undone when this block rolls back, including when it is a savepoint
                # inside a transaction the caller already opened (for example ATOMIC_REQUESTS).
                cursor.execute("SET LOCAL transaction_read_only = on")
                cursor.execute(timeout_statement(timeout_ms))
                cursor.execute(compiled.sql, params)
                rows = rows_from_cursor(cursor)
            transaction.set_rollback(True, using=self.using)
        return finish(rows, max_rows)


def django_executor(using: str = "default") -> DjangoExecutor:
    """Executor for a Django database connection on PostgreSQL.

    `using` is the database alias. Prefer a dedicated alias in `DATABASES` that connects with a
    read-only role. Each query runs read-only with a statement timeout and is always rolled back;
    inside an open transaction it uses a savepoint, so your own writes are not affected.
    """
    return DjangoExecutor(using)


GetAuth = Callable[[HttpRequest], Mapping[str, Any]]


def noquery_urls(nq: Any, get_auth: GetAuth, on_error: Optional[Callable[[BaseException], None]] = None) -> List[Any]:
    """URL patterns for the noquery API: `catalog` (GET), `ask`, `run` and `validate` (POST).

        urlpatterns = [path("analytics/", include(noquery_urls(nq, get_auth)))]

    `get_auth(request)` returns the auth context for the logged-in user, built from the verified
    session only. Raise `PermissionDenied` there to refuse a request. The views keep Django's CSRF
    protection, so browser clients must send the CSRF token.
    """

    def view(route: str, methods: List[str]):
        @require_http_methods(methods)
        def endpoint(request: HttpRequest) -> JsonResponse:
            auth = get_auth(request)
            body: Any = {}
            if request.method == "POST":
                try:
                    body = json.loads(request.body or b"{}")
                except ValueError:
                    return _json(400, {"error": {"code": "bad_request", "message": "Request body must be a JSON object."}})
            status, payload = handle(nq, route, body, auth, on_error=on_error)
            return _json(status, payload)

        endpoint.__name__ = f"noquery_{route}"
        return endpoint

    return [
        path("catalog", view("catalog", ["GET"]), name="noquery-catalog"),
        path("ask", view("ask", ["POST"]), name="noquery-ask"),
        path("run", view("run", ["POST"]), name="noquery-run"),
        path("validate", view("validate", ["POST"]), name="noquery-validate"),
    ]


def _json(status: int, payload: Dict[str, Any]) -> JsonResponse:
    return JsonResponse(payload, status=status)
