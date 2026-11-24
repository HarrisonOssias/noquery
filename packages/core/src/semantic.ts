/**
 * The semantic layer says what the data means: entities, dimensions, measures, joins and policies.
 * It is drafted from a schema graph (see `draftSemantics`) and curated by people.
 */

export type DimensionType = "string" | "number" | "boolean" | "time";

/**
 * - `visible`: usable anywhere.
 * - `hidden`: invisible to the planner and rejected by the validator.
 * - `aggregate_only`: may feed measures (e.g. count distinct) but never be grouped, filtered or listed.
 */
export type FieldPolicy = "visible" | "hidden" | "aggregate_only";

export interface Dimension {
  type: DimensionType;
  /** Column on the entity's table. Either `column` or `sql` is required. */
  column?: string;
  /** Trusted SQL expression; `${TABLE}` is replaced with the entity's table alias. */
  sql?: string;
  label?: string;
  description?: string;
  synonyms?: string[];
  /** Allowed values; filters on this dimension are checked against them. */
  enumValues?: string[];
  policy?: FieldPolicy;
  /** If set, only callers with one of these roles can see this field. */
  roles?: string[];
  /**
   * Time dimensions only: how the column stores time.
   * - `instant` (default): an absolute instant, e.g. Postgres `timestamptz`.
   * - `utc`: a timestamp without time zone that holds UTC wall-clock time.
   * - `local`: a wall-clock value read in the query's time zone, e.g. a `date` column.
   */
  timeStorage?: TimeStorage;
}

export type TimeStorage = "instant" | "utc" | "local";

export type Aggregation = "count" | "count_distinct" | "sum" | "avg" | "min" | "max";

export type MeasureFormat = "number" | "integer" | "percent" | "currency" | "duration_seconds";

export interface Measure {
  /**
   * Aggregation applied to `column` or `sql`. `count` with neither counts rows.
   * Omit `agg` only when `sql` is itself an aggregate expression.
   */
  agg?: Aggregation;
  column?: string;
  /** Trusted SQL expression; `${TABLE}` is replaced with the entity's table alias. */
  sql?: string;
  label?: string;
  description?: string;
  synonyms?: string[];
  roles?: string[];
  format?: MeasureFormat;
}

/** Adds `<alias>.<column> = auth[authKey]` to every query that touches the entity. */
export interface RowScope {
  column: string;
  authKey: string;
}

export interface Entity {
  table: string;
  schema?: string;
  label?: string;
  description?: string;
  synonyms?: string[];
  /** Name of the time dimension used when a question gives a time range but no field. */
  defaultTime?: string;
  dimensions: Record<string, Dimension>;
  measures: Record<string, Measure>;
  rowScope?: RowScope[];
  roles?: string[];
}

/**
 * `many_to_one`: each `from` row matches at most one `to` row (a foreign key from -> to).
 * Joins are only followed in directions that cannot multiply rows (no fan-out).
 */
export type Relationship = "many_to_one" | "one_to_one" | "one_to_many";

export interface Join {
  from: string;
  to: string;
  /** Pairs of [fromColumn, toColumn]. */
  on: Array<[string, string]>;
  relationship: Relationship;
}

export interface SemanticModel {
  entities: Record<string, Entity>;
  joins?: Join[];
}

/** Identity helper that gives authors type-checking and autocomplete. */
export function defineSemantics<const T extends SemanticModel>(model: T): T {
  return model;
}

export type EntityOverlay = Partial<Omit<Entity, "dimensions" | "measures">> & {
  dimensions?: Record<string, Partial<Dimension> | null>;
  measures?: Record<string, Partial<Measure> | null>;
};

export interface SemanticOverlay {
  /** `null` removes an entity from the base model. */
  entities?: Record<string, EntityOverlay | null>;
  joins?: Join[];
  /** Joins to drop from the base model, matched by `from` and `to`. */
  removeJoins?: Array<{ from: string; to: string }>;
}

/**
 * Applies a curated overlay on top of a (usually auto-drafted) model.
 * Fields merge shallowly; `null` removes an entity, dimension or measure.
 */
export function mergeSemantics(base: SemanticModel, overlay: SemanticOverlay): SemanticModel {
  const entities: Record<string, Entity> = {};
  for (const [name, entity] of Object.entries(base.entities)) {
    entities[name] = {
      ...entity,
      dimensions: { ...entity.dimensions },
      measures: { ...entity.measures },
    };
  }

  for (const [name, patch] of Object.entries(overlay.entities ?? {})) {
    if (patch === null) {
      delete entities[name];
      continue;
    }
    const { dimensions, measures, ...rest } = patch;
    const current = entities[name];
    if (!current && !rest.table) {
      throw new Error(`Overlay adds entity "${name}" without a table.`);
    }
    const merged: Entity = {
      ...(current ?? { table: rest.table!, dimensions: {}, measures: {} }),
      ...stripUndefined(rest),
    };
    merged.dimensions = mergeRecord(current?.dimensions ?? {}, dimensions, (d) => {
      if (!d.type) throw new Error(`New dimension in "${name}" needs a type.`);
      return d as Dimension;
    });
    merged.measures = mergeRecord(current?.measures ?? {}, measures, (m) => m as Measure);
    entities[name] = merged;
  }

  const removed = new Set((overlay.removeJoins ?? []).map((j) => `${j.from}->${j.to}`));
  const joins = new Map<string, Join>();
  for (const join of [...(base.joins ?? []), ...(overlay.joins ?? [])]) {
    const key = `${join.from}->${join.to}`;
    if (removed.has(key)) continue;
    joins.set(key, join);
  }
  const keptJoins = [...joins.values()].filter((j) => entities[j.from] && entities[j.to]);

  return { entities, joins: keptJoins };
}

function mergeRecord<T extends object>(
  base: Record<string, T>,
  patch: Record<string, Partial<T> | null> | undefined,
  create: (value: Partial<T>) => T,
): Record<string, T> {
  const out: Record<string, T> = { ...base };
  for (const [key, value] of Object.entries(patch ?? {})) {
    if (value === null) {
      delete out[key];
    } else if (out[key]) {
      out[key] = { ...out[key], ...stripUndefined(value) } as T;
    } else {
      out[key] = create(value);
    }
  }
  return out;
}

function stripUndefined<T extends object>(value: T): T {
  return Object.fromEntries(Object.entries(value).filter(([, v]) => v !== undefined)) as T;
}

export interface ModelIssue {
  path: string;
  message: string;
}

const NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Static checks on a semantic model. Returns an empty array when the model is sound. */
export function checkSemantics(model: SemanticModel): ModelIssue[] {
  const issues: ModelIssue[] = [];
  for (const [entityName, entity] of Object.entries(model.entities)) {
    const path = `entities.${entityName}`;
    if (!NAME.test(entityName)) issues.push({ path, message: "Entity names must be identifiers." });
    if (!entity.table) issues.push({ path, message: "Entity has no table." });
    for (const [dimName, dim] of Object.entries(entity.dimensions)) {
      const p = `${path}.dimensions.${dimName}`;
      if (!NAME.test(dimName)) issues.push({ path: p, message: "Field names must be identifiers." });
      if (!dim.column && !dim.sql) issues.push({ path: p, message: "Dimension needs a column or sql." });
    }
    for (const [measureName, measure] of Object.entries(entity.measures)) {
      const p = `${path}.measures.${measureName}`;
      if (!NAME.test(measureName)) issues.push({ path: p, message: "Field names must be identifiers." });
      if (entity.dimensions[measureName]) {
        issues.push({ path: p, message: "A measure and a dimension share this name." });
      }
      if (!measure.agg && !measure.sql) {
        issues.push({ path: p, message: "Measure needs an agg, or sql that is an aggregate." });
      }
      if (measure.agg && measure.agg !== "count" && !measure.column && !measure.sql) {
        issues.push({ path: p, message: `"${measure.agg}" needs a column or sql.` });
      }
    }
    if (entity.defaultTime) {
      const dim = entity.dimensions[entity.defaultTime];
      if (!dim || dim.type !== "time") {
        issues.push({ path: `${path}.defaultTime`, message: "defaultTime must name a time dimension." });
      }
    }
  }
  for (const [i, join] of (model.joins ?? []).entries()) {
    const p = `joins[${i}]`;
    if (!model.entities[join.from]) issues.push({ path: p, message: `Unknown entity "${join.from}".` });
    if (!model.entities[join.to]) issues.push({ path: p, message: `Unknown entity "${join.to}".` });
    if (join.on.length === 0) issues.push({ path: p, message: "Join needs at least one column pair." });
  }
  return issues;
}
