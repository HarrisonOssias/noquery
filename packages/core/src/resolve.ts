import type { Filter, FilterOp, FilterScalar, QueryIR, TimeGrain, TimeRange } from "./ir.js";
import { FILTER_OPS, TIME_GRAINS, TIME_PRESETS } from "./ir.js";
import type { Dimension, DimensionType, Entity, Measure, MeasureFormat, SemanticModel } from "./semantic.js";
import { humanize } from "./draft.js";

/**
 * Who is asking. Supplied by the host app on every call; never by the model.
 * Row scopes read their values from here by key (e.g. `tenantId`).
 */
export interface AuthContext {
  userId?: string;
  roles?: string[];
  [key: string]: unknown;
}

export type QueryErrorCode =
  | "invalid_ir"
  | "empty_query"
  | "unknown_entity"
  | "unknown_field"
  | "wrong_field_kind"
  | "aggregate_only"
  | "mixed_measure_entities"
  | "no_join_path"
  | "invalid_filter"
  | "invalid_time"
  | "invalid_timezone"
  | "invalid_order_by"
  | "invalid_limit"
  | "missing_auth_attribute";

export interface QueryError {
  code: QueryErrorCode;
  message: string;
  path?: string;
}

export interface ResolveOptions {
  defaultTimezone?: string;
  defaultLimit?: number;
  maxLimit?: number;
}

export interface TableRef {
  entity: string;
  table: string;
  schema?: string;
  alias: string;
}

export interface JoinStep extends TableRef {
  parentAlias: string;
  /** Pairs of [parentColumn, childColumn]. */
  on: Array<[string, string]>;
}

export type OutputRole = "time" | "dimension" | "measure";

export interface OutputColumn {
  key: string;
  ref: string;
  role: OutputRole;
  type: DimensionType;
  label: string;
  grain?: TimeGrain;
  format?: MeasureFormat;
}

export interface SelectedDimension {
  key: string;
  alias: string;
  dimension: Dimension;
}

export interface SelectedMeasure {
  key: string;
  alias: string;
  measure: Measure;
}

export interface SelectedTime {
  key: string;
  alias: string;
  dimension: Dimension;
  grain: TimeGrain;
}

export type Condition =
  | { kind: "dimension"; alias: string; dimension: Dimension; op: FilterOp; value?: FilterScalar | FilterScalar[] }
  | { kind: "scope"; alias: string; column: string; value: FilterScalar | FilterScalar[] }
  | { kind: "time_range"; alias: string; dimension: Dimension; range: TimeRange };

export interface HavingCondition {
  alias: string;
  measure: Measure;
  op: Exclude<FilterOp, "in" | "not_in" | "contains" | "starts_with" | "is_null" | "is_not_null">;
  value: number;
}

/** A query that passed validation, with joins planned and policies applied. Dialects compile this. */
export interface ResolvedQuery {
  ir: QueryIR;
  timezone: string;
  base: TableRef;
  joins: JoinStep[];
  time?: SelectedTime;
  dimensions: SelectedDimension[];
  measures: SelectedMeasure[];
  where: Condition[];
  having: HavingCondition[];
  orderBy: Array<{ key: string; dir: "asc" | "desc" }>;
  limit: number;
  columns: OutputColumn[];
  /** Human labels for every field the IR references, including filter-only fields. */
  labels: Record<string, string>;
}

export type ResolveResult =
  | { ok: true; query: ResolvedQuery }
  | { ok: false; errors: QueryError[] };

type FieldHit =
  | { kind: "dimension"; entity: string; name: string; dimension: Dimension }
  | { kind: "measure"; entity: string; name: string; measure: Measure };

const DEFAULTS = { defaultTimezone: "UTC", defaultLimit: 1000, maxLimit: 10000 };
const MAX_IN_VALUES = 1000;

export function hasAccess(roles: string[] | undefined, auth: AuthContext): boolean {
  if (!roles || roles.length === 0) return true;
  const mine = auth.roles ?? [];
  return roles.some((r) => mine.includes(r));
}

export function isTimezone(tz: string): boolean {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: tz });
    return true;
  } catch {
    return false;
  }
}

/**
 * Validates an IR against the semantic model and the caller's auth context, and plans the query.
 * Every rule here is enforced in code; nothing depends on the model behaving.
 */
export function resolveQuery(
  model: SemanticModel,
  ir: QueryIR,
  auth: AuthContext,
  options: ResolveOptions = {},
): ResolveResult {
  const opts = { ...DEFAULTS, ...stripUndefined(options) };
  const errors: QueryError[] = [];
  const fail = (code: QueryErrorCode, message: string, path?: string) => {
    errors.push(path ? { code, message, path } : { code, message });
  };

  if (!ir || typeof ir !== "object" || ir.version !== 1) {
    return { ok: false, errors: [{ code: "invalid_ir", message: "IR must be an object with version: 1." }] };
  }

  const lookup = (ref: unknown, path: string): FieldHit | undefined => {
    if (typeof ref !== "string" || !/^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$/.test(ref)) {
      fail("unknown_field", `"${String(ref)}" is not a field reference like "entity.field".`, path);
      return undefined;
    }
    const [entityName, fieldName] = ref.split(".") as [string, string];
    const entity = model.entities[entityName];
    if (!entity || !hasAccess(entity.roles, auth)) {
      fail("unknown_entity", `Unknown entity "${entityName}".`, path);
      return undefined;
    }
    const dimension = entity.dimensions[fieldName];
    if (dimension && dimension.policy !== "hidden" && hasAccess(dimension.roles, auth)) {
      return { kind: "dimension", entity: entityName, name: fieldName, dimension };
    }
    const measure = entity.measures[fieldName];
    if (measure && hasAccess(measure.roles, auth)) {
      return { kind: "measure", entity: entityName, name: fieldName, measure };
    }
    fail("unknown_field", `Unknown field "${ref}".`, path);
    return undefined;
  };

  const groupable = (hit: FieldHit | undefined, path: string): Extract<FieldHit, { kind: "dimension" }> | undefined => {
    if (!hit) return undefined;
    if (hit.kind !== "dimension") {
      fail("wrong_field_kind", `"${hit.entity}.${hit.name}" is a measure, not a dimension.`, path);
      return undefined;
    }
    if (hit.dimension.policy === "aggregate_only") {
      fail("aggregate_only", `"${hit.entity}.${hit.name}" can only be counted, not grouped, filtered or listed.`, path);
      return undefined;
    }
    return hit;
  };

  // --- measures -------------------------------------------------------------------------------
  const measureHits: Array<Extract<FieldHit, { kind: "measure" }>> = [];
  for (const [i, ref] of (ir.measures ?? []).entries()) {
    const hit = lookup(ref, `measures[${i}]`);
    if (!hit) continue;
    if (hit.kind !== "measure") {
      fail("wrong_field_kind", `"${ref}" is a dimension, not a measure.`, `measures[${i}]`);
      continue;
    }
    measureHits.push(hit);
  }

  // --- dimensions -----------------------------------------------------------------------------
  const dimensionHits: Array<Extract<FieldHit, { kind: "dimension" }>> = [];
  for (const [i, ref] of (ir.dimensions ?? []).entries()) {
    const hit = groupable(lookup(ref, `dimensions[${i}]`), `dimensions[${i}]`);
    if (hit) dimensionHits.push(hit);
  }

  // --- time -----------------------------------------------------------------------------------
  let timeHit: Extract<FieldHit, { kind: "dimension" }> | undefined;
  const timezone = ir.time?.tz ?? opts.defaultTimezone;
  if (ir.time) {
    timeHit = groupable(lookup(ir.time.field, "time.field"), "time.field");
    if (timeHit && timeHit.dimension.type !== "time") {
      fail("invalid_time", `"${ir.time.field}" is not a time field.`, "time.field");
      timeHit = undefined;
    }
    if (ir.time.grain !== undefined && !TIME_GRAINS.includes(ir.time.grain)) {
      fail("invalid_time", `Unknown grain "${String(ir.time.grain)}".`, "time.grain");
    }
    if (ir.time.range !== undefined) checkRange(ir.time.range, fail);
    if (!isTimezone(timezone)) fail("invalid_timezone", `Unknown time zone "${timezone}".`, "time.tz");
  }

  // --- filters --------------------------------------------------------------------------------
  const dimensionFilters: Array<{ hit: Extract<FieldHit, { kind: "dimension" }>; filter: Filter }> = [];
  const measureFilters: Array<{ hit: Extract<FieldHit, { kind: "measure" }>; filter: Filter; value: number }> = [];
  for (const [i, filter] of (ir.filters ?? []).entries()) {
    const path = `filters[${i}]`;
    if (!filter || typeof filter !== "object" || !FILTER_OPS.includes(filter.op)) {
      fail("invalid_filter", `Filter ${i} has an unknown op "${String(filter?.op)}".`, path);
      continue;
    }
    const hit = lookup(filter.field, `${path}.field`);
    if (!hit) continue;
    if (hit.kind === "measure") {
      const value = checkMeasureFilter(filter, fail, path);
      if (value !== undefined) measureFilters.push({ hit, filter, value });
      continue;
    }
    const dim = groupable(hit, `${path}.field`);
    if (!dim) continue;
    if (checkDimensionFilter(dim.dimension, filter, fail, path)) dimensionFilters.push({ hit: dim, filter });
  }

  // --- base entity ----------------------------------------------------------------------------
  const measureEntities = new Set([...measureHits, ...measureFilters.map((f) => f.hit)].map((h) => h.entity));
  if (measureEntities.size > 1) {
    fail(
      "mixed_measure_entities",
      `Measures must come from one entity per query; got ${[...measureEntities].join(", ")}.`,
      "measures",
    );
  }
  const baseName = [...measureEntities][0] ?? dimensionHits[0]?.entity ?? timeHit?.entity;
  if (!baseName) {
    if (errors.length === 0) fail("empty_query", "Select at least one measure, dimension or time field.");
    return { ok: false, errors };
  }

  // --- limit ----------------------------------------------------------------------------------
  const limit = ir.limit ?? opts.defaultLimit;
  if (!Number.isInteger(limit) || limit < 1 || limit > opts.maxLimit) {
    fail("invalid_limit", `limit must be an integer from 1 to ${opts.maxLimit}.`, "limit");
  }

  // --- joins ----------------------------------------------------------------------------------
  const needed = new Set<string>([
    ...dimensionHits.map((h) => h.entity),
    ...dimensionFilters.map((f) => f.hit.entity),
    ...(timeHit ? [timeHit.entity] : []),
  ]);
  needed.delete(baseName);
  const plan = planJoins(model, baseName, needed, auth);
  for (const missing of plan.unreachable) {
    const why = plan.fanOut.has(missing)
      ? `joining it would repeat ${baseName} rows and inflate totals`
      : "there is no join between them";
    fail("no_join_path", `Cannot combine ${baseName} with ${missing}: ${why}.`);
  }

  if (errors.length > 0) return { ok: false, errors };

  // --- assemble -------------------------------------------------------------------------------
  const baseEntity = model.entities[baseName]!;
  const base: TableRef = tableRef(baseName, baseEntity, "t0");
  const aliases = new Map<string, string>([[baseName, "t0"]]);
  const joins: JoinStep[] = plan.steps.map((step, i) => {
    const alias = `t${i + 1}`;
    aliases.set(step.entity, alias);
    return {
      ...tableRef(step.entity, model.entities[step.entity]!, alias),
      parentAlias: aliases.get(step.parent)!,
      on: step.on,
    };
  });
  const aliasOf = (entity: string) => aliases.get(entity)!;

  const columns: OutputColumn[] = [];
  const keys = new Map<string, string>();
  const ensureKey = (ref: string, key: string) => keys.set(ref, key);

  let time: SelectedTime | undefined;
  if (timeHit && ir.time?.grain) {
    const ref = `${timeHit.entity}.${timeHit.name}`;
    const key = `${ref}__${ir.time.grain}`;
    time = { key, alias: aliasOf(timeHit.entity), dimension: timeHit.dimension, grain: ir.time.grain };
    ensureKey(ref, key);
    columns.push({ key, ref, role: "time", type: "time", grain: ir.time.grain, label: labelOf(model, timeHit.entity, timeHit.name, timeHit.dimension.label) });
  }

  const dimensions: SelectedDimension[] = [];
  for (const hit of dimensionHits) {
    const ref = `${hit.entity}.${hit.name}`;
    if (keys.has(ref)) continue;
    ensureKey(ref, ref);
    dimensions.push({ key: ref, alias: aliasOf(hit.entity), dimension: hit.dimension });
    columns.push({ key: ref, ref, role: "dimension", type: hit.dimension.type, label: labelOf(model, hit.entity, hit.name, hit.dimension.label) });
  }

  const measures: SelectedMeasure[] = [];
  for (const hit of measureHits) {
    const ref = `${hit.entity}.${hit.name}`;
    if (keys.has(ref)) continue;
    ensureKey(ref, ref);
    measures.push({ key: ref, alias: aliasOf(hit.entity), measure: hit.measure });
    const col: OutputColumn = { key: ref, ref, role: "measure", type: "number", label: labelOf(model, hit.entity, hit.name, hit.measure.label) };
    if (hit.measure.format) col.format = hit.measure.format;
    columns.push(col);
  }

  const where: Condition[] = [];
  for (const scope of scopesFor([baseName, ...plan.steps.map((s) => s.entity)], model, auth, aliasOf, fail)) {
    where.push(scope);
  }
  if (timeHit && ir.time?.range) {
    where.push({ kind: "time_range", alias: aliasOf(timeHit.entity), dimension: timeHit.dimension, range: ir.time.range });
  }
  for (const { hit, filter } of dimensionFilters) {
    const cond: Condition = { kind: "dimension", alias: aliasOf(hit.entity), dimension: hit.dimension, op: filter.op };
    if (filter.value !== undefined) cond.value = filter.value;
    where.push(cond);
  }

  const having: HavingCondition[] = measureFilters.map(({ hit, filter, value }) => ({
    alias: aliasOf(hit.entity),
    measure: hit.measure,
    op: filter.op as HavingCondition["op"],
    value,
  }));

  const orderBy: Array<{ key: string; dir: "asc" | "desc" }> = [];
  for (const [i, order] of (ir.orderBy ?? []).entries()) {
    const key = keys.get(order?.field);
    if (!key) {
      fail("invalid_order_by", `Can only sort by a selected field; "${String(order?.field)}" is not selected.`, `orderBy[${i}]`);
      continue;
    }
    if (order.dir !== "asc" && order.dir !== "desc") {
      fail("invalid_order_by", `Sort direction must be "asc" or "desc".`, `orderBy[${i}].dir`);
      continue;
    }
    orderBy.push({ key, dir: order.dir });
  }
  if (orderBy.length === 0) {
    if (time) orderBy.push({ key: time.key, dir: "asc" });
    else if (measures[0]) orderBy.push({ key: measures[0].key, dir: "desc" });
    else if (dimensions[0]) orderBy.push({ key: dimensions[0].key, dir: "asc" });
  }

  if (errors.length > 0) return { ok: false, errors };

  const labels: Record<string, string> = {};
  for (const c of columns) labels[c.ref] = c.label;
  for (const { hit } of dimensionFilters) labels[`${hit.entity}.${hit.name}`] ??= labelOf(model, hit.entity, hit.name, hit.dimension.label);
  for (const { hit } of measureFilters) labels[`${hit.entity}.${hit.name}`] ??= labelOf(model, hit.entity, hit.name, hit.measure.label);

  return {
    ok: true,
    query: { ir, timezone, base, joins, dimensions, measures, where, having, orderBy, limit, columns, labels, ...(time ? { time } : {}) },
  };
}

function tableRef(entity: string, def: Entity, alias: string): TableRef {
  const ref: TableRef = { entity, table: def.table, alias };
  if (def.schema) ref.schema = def.schema;
  return ref;
}

function labelOf(model: SemanticModel, entity: string, field: string, label?: string): string {
  if (label) return label;
  const entityLabel = model.entities[entity]?.label ?? humanize(entity);
  return `${entityLabel} ${humanize(field).toLowerCase()}`;
}

interface PlannedStep {
  entity: string;
  parent: string;
  on: Array<[string, string]>;
}

/**
 * Breadth-first search over joins that cannot multiply base rows
 * (many-to-one and one-to-one), from the base entity to every needed entity.
 */
export function planJoins(model: SemanticModel, base: string, needed: Set<string>, auth: AuthContext) {
  type Edge = { to: string; on: Array<[string, string]> };
  const safe = new Map<string, Edge[]>();
  const unsafe = new Map<string, string[]>();
  const add = <T>(map: Map<string, T[]>, key: string, value: T) => map.set(key, [...(map.get(key) ?? []), value]);
  const visible = (name: string) => {
    const e = model.entities[name];
    return !!e && hasAccess(e.roles, auth);
  };

  for (const join of model.joins ?? []) {
    if (!visible(join.from) || !visible(join.to)) continue;
    const forward = join.on;
    const backward = join.on.map(([a, b]) => [b, a] as [string, string]);
    if (join.relationship === "many_to_one") {
      add(safe, join.from, { to: join.to, on: forward });
      add(unsafe, join.to, join.from);
    } else if (join.relationship === "one_to_many") {
      add(safe, join.to, { to: join.from, on: backward });
      add(unsafe, join.from, join.to);
    } else {
      add(safe, join.from, { to: join.to, on: forward });
      add(safe, join.to, { to: join.from, on: backward });
    }
  }

  const parent = new Map<string, PlannedStep>();
  const seen = new Set([base]);
  const queue = [base];
  while (queue.length > 0) {
    const current = queue.shift()!;
    for (const edge of safe.get(current) ?? []) {
      if (seen.has(edge.to)) continue;
      seen.add(edge.to);
      parent.set(edge.to, { entity: edge.to, parent: current, on: edge.on });
      queue.push(edge.to);
    }
  }

  const unreachable: string[] = [];
  const include = new Set<string>();
  for (const target of needed) {
    if (!seen.has(target)) {
      unreachable.push(target);
      continue;
    }
    let node: string | undefined = target;
    while (node && node !== base) {
      include.add(node);
      node = parent.get(node)?.parent;
    }
  }

  // Order steps so every parent is joined before its children.
  const steps: PlannedStep[] = [];
  const placed = new Set([base]);
  while (steps.length < include.size) {
    for (const entity of include) {
      const step = parent.get(entity)!;
      if (!placed.has(entity) && placed.has(step.parent)) {
        steps.push(step);
        placed.add(entity);
      }
    }
  }

  // Which unreachable targets could have been reached through a fan-out join (for a clearer error).
  const fanOut = new Set<string>();
  if (unreachable.length > 0) {
    const all = new Set(seen);
    const q = [...seen];
    while (q.length > 0) {
      const current = q.shift()!;
      const next = [...(safe.get(current) ?? []).map((e) => e.to), ...(unsafe.get(current) ?? [])];
      for (const n of next) {
        if (!all.has(n)) {
          all.add(n);
          q.push(n);
        }
      }
    }
    for (const u of unreachable) if (all.has(u)) fanOut.add(u);
  }

  return { steps, unreachable, fanOut };
}

function scopesFor(
  entities: string[],
  model: SemanticModel,
  auth: AuthContext,
  aliasOf: (entity: string) => string,
  fail: (code: QueryErrorCode, message: string, path?: string) => void,
): Condition[] {
  const out: Condition[] = [];
  for (const name of entities) {
    for (const scope of model.entities[name]?.rowScope ?? []) {
      const value = auth[scope.authKey];
      if (isScalar(value) || (Array.isArray(value) && value.length > 0 && value.every(isScalar))) {
        out.push({ kind: "scope", alias: aliasOf(name), column: scope.column, value: value as FilterScalar | FilterScalar[] });
      } else {
        fail("missing_auth_attribute", `Access to ${name} requires "${scope.authKey}" in the auth context.`);
      }
    }
  }
  return out;
}

function isScalar(value: unknown): value is FilterScalar {
  return typeof value === "string" || typeof value === "number" || typeof value === "boolean";
}

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
const DATE_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})$/;

export function isDateOnly(value: string): boolean {
  return DATE_ONLY.test(value);
}

function checkRange(range: TimeRange, fail: (code: QueryErrorCode, message: string, path?: string) => void) {
  if (typeof range === "string") {
    if (!TIME_PRESETS.includes(range)) fail("invalid_time", `Unknown time range "${range}".`, "time.range");
    return;
  }
  if (!range || typeof range !== "object") {
    fail("invalid_time", "time.range must be a preset or { from, to }.", "time.range");
    return;
  }
  for (const side of ["from", "to"] as const) {
    const v = range[side];
    if (typeof v !== "string" || !(DATE_ONLY.test(v) || DATE_TIME.test(v)) || Number.isNaN(Date.parse(v))) {
      fail("invalid_time", `time.range.${side} must be YYYY-MM-DD or ISO-8601 with an offset.`, `time.range.${side}`);
      return;
    }
  }
  if (DATE_ONLY.test(range.from) === DATE_ONLY.test(range.to) && Date.parse(range.from) >= Date.parse(range.to)) {
    fail("invalid_time", "time.range.from must be before time.range.to.", "time.range");
  }
}

function checkDimensionFilter(
  dim: Dimension,
  filter: Filter,
  fail: (code: QueryErrorCode, message: string, path?: string) => void,
  path: string,
): boolean {
  const { op, value } = filter;
  const bad = (message: string) => {
    fail("invalid_filter", `${filter.field}: ${message}`, path);
    return false;
  };
  if (op === "is_null" || op === "is_not_null") {
    return value === undefined || value === null ? true : bad(`"${op}" takes no value.`);
  }
  if (op === "in" || op === "not_in") {
    if (!Array.isArray(value) || value.length === 0) return bad(`"${op}" needs a non-empty list.`);
    if (value.length > MAX_IN_VALUES) return bad(`"${op}" accepts at most ${MAX_IN_VALUES} values.`);
    return value.every((v) => checkScalar(dim, v, bad));
  }
  if (Array.isArray(value)) return bad(`"${op}" takes a single value.`);
  if (value === undefined || value === null) return bad(`"${op}" needs a value.`);
  if (op === "contains" || op === "starts_with") {
    if (dim.type !== "string") return bad(`"${op}" only works on text fields.`);
    return typeof value === "string" ? true : bad("value must be text.");
  }
  if ((op === "gt" || op === "gte" || op === "lt" || op === "lte") && dim.type === "boolean") {
    return bad(`"${op}" does not apply to yes/no fields.`);
  }
  return checkScalar(dim, value, bad);
}

function checkScalar(dim: Dimension, value: unknown, bad: (message: string) => boolean): boolean {
  switch (dim.type) {
    case "string":
      if (typeof value !== "string") return bad("value must be text.");
      if (dim.enumValues && !dim.enumValues.includes(value)) {
        return bad(`"${value}" is not one of ${dim.enumValues.map((v) => `"${v}"`).join(", ")}.`);
      }
      return true;
    case "number":
      return typeof value === "number" && Number.isFinite(value) ? true : bad("value must be a number.");
    case "boolean":
      return typeof value === "boolean" ? true : bad("value must be true or false.");
    case "time":
      return typeof value === "string" && (DATE_ONLY.test(value) || DATE_TIME.test(value))
        ? true
        : bad("value must be YYYY-MM-DD or ISO-8601 with an offset.");
  }
}

function checkMeasureFilter(
  filter: Filter,
  fail: (code: QueryErrorCode, message: string, path?: string) => void,
  path: string,
): number | undefined {
  const allowed: FilterOp[] = ["eq", "neq", "gt", "gte", "lt", "lte"];
  if (!allowed.includes(filter.op)) {
    fail("invalid_filter", `${filter.field}: totals can only be compared with eq, neq, gt, gte, lt or lte.`, path);
    return undefined;
  }
  if (typeof filter.value !== "number" || !Number.isFinite(filter.value)) {
    fail("invalid_filter", `${filter.field}: value must be a number.`, path);
    return undefined;
  }
  return filter.value;
}

function stripUndefined<T extends object>(value: T): Partial<T> {
  return Object.fromEntries(Object.entries(value).filter(([, v]) => v !== undefined)) as Partial<T>;
}
