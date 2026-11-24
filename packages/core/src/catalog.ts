import type { AuthContext } from "./resolve.js";
import { hasAccess } from "./resolve.js";
import type { DimensionType, SemanticModel } from "./semantic.js";
import { humanize } from "./draft.js";

export interface CatalogField {
  ref: string;
  label: string;
  description?: string;
  synonyms?: string[];
}

export interface CatalogDimension extends CatalogField {
  type: DimensionType;
  enumValues?: string[];
}

export interface CatalogEntity {
  name: string;
  label: string;
  description?: string;
  synonyms?: string[];
  defaultTime?: string;
  dimensions: CatalogDimension[];
  measures: CatalogField[];
  /** Entities this one can be combined with (reachable without inflating totals). */
  joinsTo: string[];
}

/** What one caller is allowed to see: hidden, aggregate-only and role-gated fields are removed. */
export interface Catalog {
  entities: CatalogEntity[];
}

export function buildCatalog(model: SemanticModel, auth: AuthContext): Catalog {
  const visible = new Set(
    Object.entries(model.entities)
      .filter(([, e]) => hasAccess(e.roles, auth))
      .map(([name]) => name),
  );
  const safeNext = new Map<string, Set<string>>();
  const link = (a: string, b: string) => safeNext.set(a, new Set([...(safeNext.get(a) ?? []), b]));
  for (const j of model.joins ?? []) {
    if (!visible.has(j.from) || !visible.has(j.to)) continue;
    if (j.relationship === "many_to_one") link(j.from, j.to);
    else if (j.relationship === "one_to_many") link(j.to, j.from);
    else {
      link(j.from, j.to);
      link(j.to, j.from);
    }
  }

  const entities: CatalogEntity[] = [];
  for (const name of visible) {
    const entity = model.entities[name]!;
    const dimensions: CatalogDimension[] = [];
    for (const [fname, d] of Object.entries(entity.dimensions)) {
      if (d.policy === "hidden" || d.policy === "aggregate_only" || !hasAccess(d.roles, auth)) continue;
      const item: CatalogDimension = { ref: `${name}.${fname}`, label: d.label ?? humanize(fname), type: d.type };
      if (d.description) item.description = d.description;
      if (d.synonyms?.length) item.synonyms = d.synonyms;
      if (d.enumValues?.length) item.enumValues = d.enumValues;
      dimensions.push(item);
    }
    const measures: CatalogField[] = [];
    for (const [fname, m] of Object.entries(entity.measures)) {
      if (!hasAccess(m.roles, auth)) continue;
      const item: CatalogField = { ref: `${name}.${fname}`, label: m.label ?? humanize(fname) };
      if (m.description) item.description = m.description;
      if (m.synonyms?.length) item.synonyms = m.synonyms;
      measures.push(item);
    }
    const reach = new Set<string>();
    const queue = [name];
    while (queue.length) {
      const cur = queue.shift()!;
      for (const n of safeNext.get(cur) ?? []) {
        if (n !== name && !reach.has(n)) {
          reach.add(n);
          queue.push(n);
        }
      }
    }
    const out: CatalogEntity = {
      name,
      label: entity.label ?? humanize(name),
      dimensions,
      measures,
      joinsTo: [...reach],
    };
    if (entity.description) out.description = entity.description;
    if (entity.synonyms?.length) out.synonyms = entity.synonyms;
    if (entity.defaultTime && dimensions.some((d) => d.ref === `${name}.${entity.defaultTime}`)) {
      out.defaultTime = `${name}.${entity.defaultTime}`;
    }
    entities.push(out);
  }
  return { entities };
}

/** Compact text rendering of a catalog for a model prompt. */
export function catalogToText(catalog: Catalog): string {
  const lines: string[] = [];
  for (const e of catalog.entities) {
    lines.push(`## ${e.name} — ${e.label}${e.description ? `: ${e.description}` : ""}`);
    if (e.synonyms?.length) lines.push(`also called: ${e.synonyms.join(", ")}`);
    if (e.defaultTime) lines.push(`default time field: ${e.defaultTime}`);
    if (e.joinsTo.length) lines.push(`can be combined with: ${e.joinsTo.join(", ")}`);
    lines.push("measures:");
    for (const m of e.measures) lines.push(`  - ${m.ref}: ${fieldText(m)}`);
    lines.push("dimensions:");
    for (const d of e.dimensions) {
      const values = d.enumValues ? ` values: ${d.enumValues.join(" | ")}` : "";
      lines.push(`  - ${d.ref} (${d.type}): ${fieldText(d)}${values}`);
    }
    lines.push("");
  }
  return lines.join("\n");
}

function fieldText(f: CatalogField): string {
  const bits = [f.label];
  if (f.description) bits.push(`— ${f.description}`);
  if (f.synonyms?.length) bits.push(`(aka ${f.synonyms.join(", ")})`);
  return bits.join(" ");
}
