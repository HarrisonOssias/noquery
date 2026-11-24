"""The embedded engine: validates, compiles and runs queries inside your own Python process."""

from __future__ import annotations

import time
from datetime import datetime, timezone as _timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .catalog import build_catalog
from .chart import suggest_chart
from .errors import PlannerUnavailableError, QueryValidationError
from .explain import explain_query
from .resolve import AuthContext, resolve_query
from .results import Column, Result, Validation
from .runtime import CompiledQuery
from .semantic import check_semantics

AuditEvent = Dict[str, Any]


class Noquery:
    """Natural-language analytics on your own database connection.

    Example:
        nq = Noquery(
            model=merge_semantics(draft_semantics(introspect_sqlalchemy(Base)), overlay),
            dialect=postgres_dialect(),
            executor=sqlalchemy_executor(engine),
            planner=create_planner(anthropic_provider()),
            default_timezone="America/Toronto",
        )
        result = nq.ask("How many trips last week?", auth={"userId": "u1", "orgId": org_id, "roles": ["analyst"]})

    Args:
        model: The semantic model (see `noquery.semantic`).
        dialect: Compiles validated queries to SQL, e.g. `postgres_dialect()`.
        executor: Runs SQL read-only on your connection, e.g. `sqlalchemy_executor(engine)`.
        planner: Needed only for `ask`. `create_planner(anthropic_provider())`.
        default_timezone: IANA time zone for time buckets and ranges when a query gives none.
        default_limit: Rows returned when a query sets no limit.
        max_limit: Largest limit a query may ask for.
        timeout_ms: Statement timeout in milliseconds.
        max_repairs: How many times `ask` lets the planner fix a query the validator rejected.
        on_audit: Called with an audit event (a dict) after every run and ask.
        now: Clock override for tests; returns an aware datetime.

    All methods are blocking and safe to call from several threads, as long as the executor is.
    In async code, call them with `asyncio.to_thread(...)`.
    """

    def __init__(
        self,
        model: Mapping[str, Any],
        dialect: Any,
        executor: Any,
        planner: Any = None,
        default_timezone: str = "UTC",
        default_limit: int = 1000,
        max_limit: int = 10000,
        timeout_ms: int = 15000,
        max_repairs: int = 1,
        on_audit: Optional[Callable[[AuditEvent], None]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ):
        issues = check_semantics(model)
        if issues:
            listed = "\n".join(f'- {i["path"]}: {i["message"]}' for i in issues)
            raise ValueError(f"Semantic model has problems:\n{listed}")
        self.model = model
        self.dialect = dialect
        self.executor = executor
        self.planner = planner
        self.default_timezone = default_timezone
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.timeout_ms = timeout_ms
        self.max_repairs = max_repairs
        self.on_audit = on_audit
        self._now = now or (lambda: datetime.now(_timezone.utc))

    @property
    def has_planner(self) -> bool:
        """True when a planner is configured, so `ask` can be used."""
        return self.planner is not None

    def catalog(self, auth: AuthContext) -> Dict[str, Any]:
        """The entities, measures and dimensions this caller may use, for UIs and for the planner."""
        return build_catalog(self.model, auth)

    def validate(self, ir: Mapping[str, Any], auth: AuthContext) -> Validation:
        """Check a query object without running it."""
        resolved = self._resolve(ir, auth)
        return Validation(ok=resolved["ok"], errors=list(resolved.get("errors", [])))

    def compile(self, ir: Mapping[str, Any], auth: AuthContext) -> CompiledQuery:
        """Validates and compiles without running. Raises QueryValidationError when invalid."""
        resolved = self._resolve(ir, auth)
        if not resolved["ok"]:
            raise QueryValidationError.from_errors(resolved["errors"])
        return self.dialect.compile(resolved["query"])

    def run(self, ir: Mapping[str, Any], auth: AuthContext) -> Result:
        """Run a query object directly, without the planner.

        Raises QueryValidationError if the query is invalid for this caller.
        """
        started = time.monotonic()
        resolved = self._resolve(ir, auth)
        if not resolved["ok"]:
            self._audit({"type": "run", "ir": ir, "durationMs": _elapsed(started), "outcome": "invalid", "errors": resolved["errors"]}, auth)
            raise QueryValidationError.from_errors(resolved["errors"])
        try:
            result = self._execute(resolved["query"], started)
        except Exception as error:
            self._audit({"type": "run", "ir": ir, "durationMs": _elapsed(started), "outcome": "error", "error": str(error)}, auth)
            raise
        self._audit(
            {
                "type": "run",
                "ir": ir,
                "sql": result.sql,
                "params": result.params,
                "rowCount": len(result.rows),
                "durationMs": result.duration_ms,
                "outcome": "answer",
            },
            auth,
        )
        return result

    def ask(
        self,
        question: str,
        auth: AuthContext,
        timezone: Optional[str] = None,
        history: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> Result:
        """Answer a plain-English question. Needs a planner.

        `timezone` is the asker's IANA time zone; "today" and time buckets follow it.
        `history` is a list of earlier turns (`result.to_turn(question)`) for follow-up questions.
        """
        if self.planner is None:
            raise PlannerUnavailableError(501, "planner_not_configured", "ask() needs a planner. Pass `planner` to Noquery, or use run().")
        started = time.monotonic()
        catalog = build_catalog(self.model, auth)
        base: Dict[str, Any] = {
            "catalog": catalog,
            "question": question,
            "timezone": timezone or self.default_timezone,
            "now": self._now(),
        }
        if history:
            base["history"] = list(history)

        def audit(outcome: str, **extra: Any) -> None:
            self._audit({"type": "ask", "question": question, **extra, "durationMs": _elapsed(started), "outcome": outcome}, auth)

        plan = self.planner.plan(**base)
        attempt = 0
        while True:
            if plan.get("kind") == "query":
                ir_time = plan["ir"].get("time")
                if ir_time and not ir_time.get("tz") and timezone:
                    plan = {**plan, "ir": {**plan["ir"], "time": {**ir_time, "tz": timezone}}}
            if plan.get("kind") == "clarify":
                audit("clarify")
                return Result(type="clarify", question=plan.get("question"), options=list(plan.get("options") or []))
            if plan.get("kind") == "unanswerable":
                audit("unanswerable")
                return Result(type="unanswerable", reason=plan.get("reason"))
            ir = plan["ir"]
            resolved = self._resolve(ir, auth)
            if resolved["ok"]:
                try:
                    result = self._execute(resolved["query"], started)
                except Exception as error:
                    audit("error", ir=ir, error=str(error))
                    raise
                self._audit(
                    {
                        "type": "ask",
                        "question": question,
                        "ir": ir,
                        "sql": result.sql,
                        "params": result.params,
                        "rowCount": len(result.rows),
                        "durationMs": result.duration_ms,
                        "outcome": "answer",
                    },
                    auth,
                )
                return result
            errors = resolved["errors"]
            if attempt >= self.max_repairs:
                audit("invalid", ir=ir, errors=errors)
                return Result(type="invalid", errors=list(errors), ir=dict(ir))
            attempt += 1
            plan = self.planner.plan(**base, repair={"previous": plan, "errors": errors})

    # --- internals ----------------------------------------------------------------------------

    def _resolve(self, ir: Any, auth: AuthContext) -> Dict[str, Any]:
        return resolve_query(
            self.model,
            ir,
            auth,
            default_timezone=self.default_timezone,
            default_limit=self.default_limit,
            max_limit=self.max_limit,
        )

    def _execute(self, query: Mapping[str, Any], started: float) -> Result:
        compiled = self.dialect.compile(query)
        executed = self.executor.execute(compiled, timeout_ms=self.timeout_ms, max_rows=query["limit"])
        rows = normalize_rows(executed.rows, query["columns"])
        return Result(
            type="answer",
            ir=dict(query["ir"]),
            sql=compiled.sql,
            params=list(compiled.params),
            columns=[Column.from_dict(c) for c in query["columns"]],
            rows=rows,
            truncated=executed.truncated,
            explanation=explain_query(query),
            chart=suggest_chart(query["columns"], rows),
            duration_ms=_elapsed(started),
        )

    def _audit(self, event: AuditEvent, auth: AuthContext) -> None:
        if not self.on_audit:
            return
        full = {**event, "at": self._now().astimezone(_timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
        if auth.get("userId"):
            full["userId"] = auth["userId"]
        if auth.get("roles"):
            full["roles"] = auth["roles"]
        self.on_audit(full)


def normalize_rows(rows: Sequence[Mapping[str, Any]], columns: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Drivers return counts and decimals as Decimal or text; measures come back as int or float."""
    measure_keys = [c["key"] for c in columns if c["role"] == "measure"]
    out: List[Dict[str, Any]] = []
    for row in rows:
        clean = dict(row)
        for key in measure_keys:
            clean[key] = _to_number(clean.get(key))
        out.append(clean)
    return out


def _to_number(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            return float(value)
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, str) and value.strip() != "":
        try:
            number = Decimal(value.strip())
        except ArithmeticError:
            return value
        return _to_number(number)
    return value


def _elapsed(started: float) -> int:
    return int(round((time.monotonic() - started) * 1000))
