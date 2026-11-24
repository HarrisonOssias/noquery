"""The two extension points of the engine: a dialect compiles SQL, an executor runs it.

A dialect is any object with a `name` and `compile(query) -> CompiledQuery`.
An executor is any object with `execute(compiled, timeout_ms, max_rows) -> ExecuteResult`.
Executors must run read-only and enforce `timeout_ms` at the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence


@dataclass
class CompiledQuery:
    """SQL plus its bound parameters, in order.

    `paramstyle` says how placeholders are written in `sql`:
      - "pyformat": `%(p1)s`, `%(p2)s`... (psycopg, psycopg2, Django, SQLAlchemy's exec_driver_sql)
      - "numeric": `$1`, `$2`... (asyncpg, and identical to the TypeScript packages)
    """

    sql: str
    params: List[Any] = field(default_factory=list)
    paramstyle: str = "pyformat"

    def named_params(self) -> Dict[str, Any]:
        """Parameters keyed "p1", "p2"..., as "pyformat" placeholders expect."""
        return {f"p{i + 1}": value for i, value in enumerate(self.params)}


@dataclass
class ExecuteResult:
    rows: List[Dict[str, Any]]
    truncated: bool = False


def finish(rows: Sequence[Dict[str, Any]], max_rows: int) -> ExecuteResult:
    """Applies the row cap. Dialects fetch one extra row so truncation can be reported."""
    if len(rows) > max_rows:
        return ExecuteResult(rows=list(rows[:max_rows]), truncated=True)
    return ExecuteResult(rows=list(rows), truncated=False)
