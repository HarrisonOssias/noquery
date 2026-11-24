"""The catalog: what one caller is allowed to see. Hidden, aggregate-only and role-gated fields are removed."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from .draft import humanize
from .resolve import AuthContext, has_access

Catalog = Dict[str, Any]


def build_catalog(model: Mapping[str, Any], auth: AuthContext) -> Catalog:
    """Returns {"entities": [{"name", "label", "dimensions", "measures", "joinsTo", ...}]}."""
    entities = model.get("entities") or {}
    visible = [name for name, e in entities.items() if has_access(e.get("roles"), auth)]
    visible_set = set(visible)
    safe_next: Dict[str, List[str]] = {}

    def link(a: str, b: str) -> None:
        targets = safe_next.setdefault(a, [])
        if b not in targets:
            targets.append(b)

    for j in model.get("joins") or []:
        if j["from"] not in visible_set or j["to"] not in visible_set:
            continue
        if j.get("relationship") == "many_to_one":
            link(j["from"], j["to"])
        elif j.get("relationship") == "one_to_many":
            link(j["to"], j["from"])
        else:
            link(j["from"], j["to"])
            link(j["to"], j["from"])

    out_entities: List[Dict[str, Any]] = []
    for name in visible:
        entity = entities[name]
        dimensions: List[Dict[str, Any]] = []
        for fname, d in (entity.get("dimensions") or {}).items():
            if d.get("policy") in ("hidden", "aggregate_only") or not has_access(d.get("roles"), auth):
                continue
            item: Dict[str, Any] = {"ref": f"{name}.{fname}", "label": d.get("label") or humanize(fname), "type": d["type"]}
            if d.get("description"):
                item["description"] = d["description"]
            if d.get("synonyms"):
                item["synonyms"] = list(d["synonyms"])
            if d.get("enumValues"):
                item["enumValues"] = list(d["enumValues"])
            dimensions.append(item)
        measures: List[Dict[str, Any]] = []
        for fname, m in (entity.get("measures") or {}).items():
            if not has_access(m.get("roles"), auth):
                continue
            item = {"ref": f"{name}.{fname}", "label": m.get("label") or humanize(fname)}
            if m.get("description"):
                item["description"] = m["description"]
            if m.get("synonyms"):
                item["synonyms"] = list(m["synonyms"])
            measures.append(item)

        reach: List[str] = []
        queue = [name]
        while queue:
            current = queue.pop(0)
            for nxt in safe_next.get(current, []):
                if nxt != name and nxt not in reach:
                    reach.append(nxt)
                    queue.append(nxt)

        out: Dict[str, Any] = {
            "name": name,
            "label": entity.get("label") or humanize(name),
            "dimensions": dimensions,
            "measures": measures,
            "joinsTo": reach,
        }
        if entity.get("description"):
            out["description"] = entity["description"]
        if entity.get("synonyms"):
            out["synonyms"] = list(entity["synonyms"])
        default_time = entity.get("defaultTime")
        if default_time and any(d["ref"] == f"{name}.{default_time}" for d in dimensions):
            out["defaultTime"] = f"{name}.{default_time}"
        out_entities.append(out)
    return {"entities": out_entities}


def catalog_to_text(catalog: Mapping[str, Any]) -> str:
    """Compact text rendering of a catalog for a model prompt."""
    lines: List[str] = []
    for e in catalog["entities"]:
        description = f': {e["description"]}' if e.get("description") else ""
        lines.append(f'## {e["name"]} — {e["label"]}{description}')
        if e.get("synonyms"):
            lines.append(f'also called: {", ".join(e["synonyms"])}')
        if e.get("defaultTime"):
            lines.append(f'default time field: {e["defaultTime"]}')
        if e.get("joinsTo"):
            lines.append(f'can be combined with: {", ".join(e["joinsTo"])}')
        lines.append("measures:")
        for m in e["measures"]:
            lines.append(f'  - {m["ref"]}: {_field_text(m)}')
        lines.append("dimensions:")
        for d in e["dimensions"]:
            values = f' values: {" | ".join(d["enumValues"])}' if d.get("enumValues") else ""
            lines.append(f'  - {d["ref"]} ({d["type"]}): {_field_text(d)}{values}')
        lines.append("")
    return "\n".join(lines)


def _field_text(field: Mapping[str, Any]) -> str:
    bits = [field["label"]]
    if field.get("description"):
        bits.append(f'— {field["description"]}')
    if field.get("synonyms"):
        bits.append(f'(aka {", ".join(field["synonyms"])})')
    return " ".join(bits)
