"""Fakes and sample data shared by the test modules."""

from noquery import CompiledQuery, ExecuteResult


class FakeDialect:
    name = "fake"

    def __init__(self):
        self.compiled = []

    def compile(self, query):
        self.compiled.append(query)
        return CompiledQuery(sql=f'-- {query["base"]["entity"]}', params=[])


class FakeExecutor:
    def __init__(self, rows=None, truncated=False, error=None):
        self.rows = rows if rows is not None else []
        self.truncated = truncated
        self.error = error
        self.calls = []

    def execute(self, compiled, timeout_ms, max_rows):
        self.calls.append({"compiled": compiled, "timeout_ms": timeout_ms, "max_rows": max_rows})
        if self.error:
            raise self.error
        return ExecuteResult(rows=list(self.rows), truncated=self.truncated)


class ScriptedPlanner:
    """Returns queued plans in order and records what it was asked."""

    def __init__(self, *plans):
        self.plans = list(plans)
        self.calls = []

    def plan(self, **kwargs):
        self.calls.append(kwargs)
        plan = self.plans.pop(0)
        if isinstance(plan, Exception):
            raise plan
        return plan


def query_plan(**ir):
    return {"kind": "query", "ir": {"version": 1, **ir}}
