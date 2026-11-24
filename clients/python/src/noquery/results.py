"""Result types shared by the embedded engine (`Noquery`) and the HTTP client (`NoqueryClient`)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional


@dataclass
class Column:
    key: str
    ref: str
    role: str  # "time", "dimension" or "measure"
    type: str  # "string", "number", "boolean" or "time"
    label: str
    grain: Optional[str] = None
    format: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Column":
        return cls(
            key=data["key"],
            ref=data["ref"],
            role=data["role"],
            type=data["type"],
            label=data["label"],
            grain=data.get("grain"),
            format=data.get("format"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


@dataclass
class Result:
    """The outcome of `ask` or `run`.

    `type` is one of:
      - "answer": rows, columns, sql, explanation and chart are set.
      - "clarify": the question was ambiguous; see `question` and `options`.
      - "unanswerable": the data can't answer it; see `reason`.
      - "invalid": the planner could not produce a valid query; see `errors`.
    """

    type: str
    rows: List[Dict[str, Any]] = field(default_factory=list)
    columns: List[Column] = field(default_factory=list)
    ir: Optional[Dict[str, Any]] = None
    sql: Optional[str] = None
    params: List[Any] = field(default_factory=list)
    truncated: bool = False
    explanation: Optional[str] = None
    chart: Optional[Dict[str, Any]] = None
    duration_ms: Optional[int] = None
    question: Optional[str] = None
    options: List[str] = field(default_factory=list)
    reason: Optional[str] = None
    errors: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Result":
        return cls(
            type=data.get("type", "answer"),
            rows=list(data.get("rows", [])),
            columns=[Column.from_dict(c) for c in data.get("columns", [])],
            ir=data.get("ir"),
            sql=data.get("sql"),
            params=list(data.get("params", [])),
            truncated=bool(data.get("truncated", False)),
            explanation=data.get("explanation"),
            chart=data.get("chart"),
            duration_ms=data.get("durationMs"),
            question=data.get("question"),
            options=list(data.get("options", [])),
            reason=data.get("reason"),
            errors=list(data.get("errors", [])),
        )

    def to_dict(self) -> Dict[str, Any]:
        """The same JSON shape the HTTP API returns, so `Result.from_dict(result.to_dict())` round-trips."""
        if self.type == "clarify":
            return {"type": "clarify", "question": self.question or "", "options": list(self.options)}
        if self.type == "unanswerable":
            return {"type": "unanswerable", "reason": self.reason or ""}
        if self.type == "invalid":
            out: Dict[str, Any] = {"type": "invalid", "errors": list(self.errors)}
            if self.ir is not None:
                out["ir"] = self.ir
            return out
        return {
            "type": self.type,
            "ir": self.ir,
            "sql": self.sql,
            "params": list(self.params),
            "columns": [c.to_dict() for c in self.columns],
            "rows": self.rows,
            "truncated": self.truncated,
            "explanation": self.explanation,
            "chart": self.chart,
            "durationMs": self.duration_ms,
        }

    @property
    def ok(self) -> bool:
        return self.type == "answer"

    def labeled_rows(self) -> List[Dict[str, Any]]:
        """Rows keyed by human labels ("Trips") instead of field keys ("trips.count")."""
        labels = {c.key: c.label for c in self.columns}
        return [{labels.get(k, k): v for k, v in row.items()} for row in self.rows]

    def to_dataframe(self, labels: bool = False):
        """Rows as a pandas DataFrame. Requires `pip install "noquery[pandas]"`."""
        try:
            import pandas as pd
        except ImportError as error:  # pragma: no cover - depends on the environment
            raise ImportError('to_dataframe() needs pandas: pip install "noquery[pandas]"') from error
        rows = self.labeled_rows() if labels else self.rows
        order = [c.label if labels else c.key for c in self.columns]
        return pd.DataFrame(rows, columns=order or None)

    def to_turn(self, question: str) -> Dict[str, Any]:
        """This result as a history entry, so a follow-up question ("now just Ontario") can build on it."""
        if self.type == "answer":
            planned: Dict[str, Any] = {"kind": "query", "ir": self.ir}
        elif self.type == "clarify":
            planned = {"kind": "clarify", "question": self.question or "", "options": self.options}
        else:
            planned = {"kind": "unanswerable", "reason": self.reason or ""}
        return {"question": question, "result": planned}


@dataclass
class Validation:
    ok: bool
    errors: List[Dict[str, Any]] = field(default_factory=list)
