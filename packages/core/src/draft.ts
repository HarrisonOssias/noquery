import type { ColumnInfo, SchemaGraph, TableInfo } from "./schema.js";
import type { Dimension, Entity, Join, Measure, SemanticModel } from "./semantic.js";

export interface DraftOptions {
  /** Column names that should start out hidden. Matched case-insensitively against the whole name. */
  sensitivePatterns?: RegExp[];
  /** Tables to leave out of the draft. */
  excludeTables?: string[];
  /** Preferred names for an entity's default time dimension, in order. */
  timePreference?: string[];
}

const DEFAULT_SENSITIVE = [
  /e[-_]?mail/i,
  /phone/i,
  /password/i,
  /passwd/i,
  /secret/i,
  /token/i,
  /api[-_]?key/i,
  /ssn/i,
  /sin_number/i,
  /date_of_birth|^dob$|birth_?date/i,
  /^first_name$|^last_name$|^full_name$/i,
  /address/i,
  /health_card|insurance_number/i,
];

const DEFAULT_TIME_PREFERENCE = [
  "occurred_at",
  "started_at",
  "start_time",
  "timestamp",
  "ts",
  "event_time",
  "created_at",
  "inserted_at",
  "updated_at",
];

/**
 * Builds a first-pass semantic model from a schema graph:
 * one entity per table, a dimension per useful column, count and numeric measures,
 * many-to-one joins from foreign keys, and likely-sensitive columns hidden.
 */
export function draftSemantics(graph: SchemaGraph, options: DraftOptions = {}): SemanticModel {
  const sensitive = options.sensitivePatterns ?? DEFAULT_SENSITIVE;
  const exclude = new Set(options.excludeTables ?? []);
  const preference = options.timePreference ?? DEFAULT_TIME_PREFERENCE;

  const tables = graph.tables.filter((t) => !exclude.has(t.name));
  const names = entityNames(tables);
  const entities: Record<string, Entity> = {};
  const joins: Join[] = [];

  for (const table of tables) {
    const entityName = names.get(tableKey(table))!;
    const fkColumns = new Set(table.foreignKeys.flatMap((fk) => fk.columns));
    const pk = table.columns.filter((c) => c.primaryKey);

    const dimensions: Record<string, Dimension> = {};
    const measures: Record<string, Measure> = {
      count: { agg: "count", label: `Number of ${humanize(entityName).toLowerCase()}`, format: "integer" },
    };

    for (const column of table.columns) {
      const isKey = column.primaryKey || fkColumns.has(column.name);
      const isSensitive = sensitive.some((re) => re.test(column.name));
      const dim = dimensionFor(column);
      if (dim && !isKey) {
        if (isSensitive) dim.policy = "hidden";
        dimensions[column.name] = dim;
      }
      if (!isKey && !isSensitive && (column.kind === "number" || column.kind === "integer")) {
        if (!looksLikeIdentifier(column.name)) {
          measures[`total_${column.name}`] = { agg: "sum", column: column.name, label: `Total ${humanize(column.name)}` };
          measures[`avg_${column.name}`] = { agg: "avg", column: column.name, label: `Average ${humanize(column.name)}` };
        }
      }
    }

    if (pk.length === 1) {
      const key = pk[0]!;
      dimensions[key.name] = { type: key.kind === "integer" || key.kind === "number" ? "number" : "string", column: key.name, policy: "aggregate_only", description: "Primary key" };
    }
    for (const fk of table.foreignKeys) {
      if (fk.columns.length !== 1) continue;
      const col = table.columns.find((c) => c.name === fk.columns[0]);
      if (!col || dimensions[col.name]) continue;
      dimensions[col.name] = { type: col.kind === "integer" || col.kind === "number" ? "number" : "string", column: col.name, policy: "aggregate_only", description: `Foreign key to ${fk.referencedTable}` };
    }

    const timeDims = Object.entries(dimensions).filter(([, d]) => d.type === "time" && d.policy !== "hidden");
    const defaultTime =
      preference.find((p) => timeDims.some(([name]) => name === p)) ?? timeDims[0]?.[0];

    const entity: Entity = {
      table: table.name,
      label: humanize(entityName),
      dimensions,
      measures,
    };
    if (table.schema) entity.schema = table.schema;
    if (table.comment) entity.description = table.comment;
    if (defaultTime) entity.defaultTime = defaultTime;
    entities[entityName] = entity;

    for (const fk of table.foreignKeys) {
      const target = names.get(tableKey({ name: fk.referencedTable, schema: fk.referencedSchema ?? table.schema }))
        ?? names.get(tableKey({ name: fk.referencedTable }));
      if (!target) continue;
      joins.push({
        from: entityName,
        to: target,
        on: fk.columns.map((c, i) => [c, fk.referencedColumns[i]!] as [string, string]),
        relationship: "many_to_one",
      });
    }
  }

  return { entities, joins };
}

function dimensionFor(column: ColumnInfo): Dimension | undefined {
  const base = { column: column.name, label: humanize(column.name) } as const;
  const withComment = <T extends Dimension>(d: T): T => (column.comment ? { ...d, description: column.comment } : d);
  switch (column.kind) {
    case "string":
    case "uuid":
      return withComment({ ...base, type: "string" });
    case "enum":
      return withComment({ ...base, type: "string", ...(column.enumValues ? { enumValues: column.enumValues } : {}) });
    case "integer":
    case "number":
      return withComment({ ...base, type: "number" });
    case "boolean":
      return withComment({ ...base, type: "boolean" });
    case "timestamp":
      return withComment({ ...base, type: "time", ...(column.withTimezone === false ? { timeStorage: "utc" as const } : {}) });
    case "date":
      return withComment({ ...base, type: "time", timeStorage: "local" });
    default:
      return undefined;
  }
}

function looksLikeIdentifier(name: string): boolean {
  return /(^id$|_id$|^code$|_code$|_number$|^year$|^version$)/i.test(name);
}

function tableKey(t: Pick<TableInfo, "name" | "schema">): string {
  return `${t.schema ?? ""}.${t.name}`;
}

/** Entity names are table names, prefixed with the schema only when two tables share a name. */
function entityNames(tables: TableInfo[]): Map<string, string> {
  const counts = new Map<string, number>();
  for (const t of tables) counts.set(t.name, (counts.get(t.name) ?? 0) + 1);
  const out = new Map<string, string>();
  for (const t of tables) {
    const clash = (counts.get(t.name) ?? 0) > 1;
    const raw = clash && t.schema ? `${t.schema}_${t.name}` : t.name;
    out.set(tableKey(t), raw.replace(/[^A-Za-z0-9_]/g, "_"));
  }
  return out;
}

export function humanize(name: string): string {
  const words = name.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[_-]+/g, " ").trim().toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}
