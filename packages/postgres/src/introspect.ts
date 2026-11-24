import type { ColumnInfo, ColumnKind, ForeignKeyInfo, SchemaGraph, TableInfo } from "@noquery/core";

type Row = Record<string, unknown>;

/** Runs a catalog query. Wrap your driver: `(sql, params) => pool.query(sql, params).then(r => r.rows)`. */
export type CatalogQuery = (sql: string, params: unknown[]) => Promise<Row[]>;

export interface IntrospectPostgresOptions {
  /** Schemas to read. Default `["public"]`. */
  schemas?: string[];
  /** Include views (useful for analytics views). Default true. */
  includeViews?: boolean;
}

const COLUMNS_SQL = `
SELECT c.table_schema, c.table_name, c.column_name, c.is_nullable, c.data_type, c.udt_name, c.udt_schema,
       t.table_type,
       col_description(format('%I.%I', c.table_schema, c.table_name)::regclass::oid, c.ordinal_position) AS column_comment,
       obj_description(format('%I.%I', c.table_schema, c.table_name)::regclass::oid, 'pg_class') AS table_comment
FROM information_schema.columns c
JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = ANY($1::text[]) AND t.table_type = ANY($2::text[])
ORDER BY c.table_schema, c.table_name, c.ordinal_position`;

const CONSTRAINTS_SQL = `
SELECT n.nspname AS table_schema, cl.relname AS table_name, con.contype,
       array(SELECT a.attname::text FROM unnest(con.conkey) WITH ORDINALITY k(num, ord)
             JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.num ORDER BY k.ord) AS columns,
       fn.nspname AS ref_schema, fcl.relname AS ref_table,
       array(SELECT a.attname::text FROM unnest(con.confkey) WITH ORDINALITY k(num, ord)
             JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.num ORDER BY k.ord) AS ref_columns
FROM pg_constraint con
JOIN pg_class cl ON cl.oid = con.conrelid
JOIN pg_namespace n ON n.oid = cl.relnamespace
LEFT JOIN pg_class fcl ON fcl.oid = con.confrelid
LEFT JOIN pg_namespace fn ON fn.oid = fcl.relnamespace
WHERE con.contype IN ('p', 'f') AND n.nspname = ANY($1::text[])
ORDER BY con.conname`;

const ENUMS_SQL = `
SELECT n.nspname AS enum_schema, t.typname AS enum_name,
       array_agg(e.enumlabel::text ORDER BY e.enumsortorder) AS labels
FROM pg_type t
JOIN pg_enum e ON e.enumtypid = t.oid
JOIN pg_namespace n ON n.oid = t.typnamespace
GROUP BY 1, 2`;

/** Reads tables, views, columns, keys, enums and comments from the Postgres catalog. */
export async function introspectPostgres(
  query: CatalogQuery,
  options: IntrospectPostgresOptions = {},
): Promise<SchemaGraph> {
  const schemas = options.schemas ?? ["public"];
  const tableTypes = options.includeViews === false ? ["BASE TABLE"] : ["BASE TABLE", "VIEW"];
  const [columns, constraints, enums] = await Promise.all([
    query(COLUMNS_SQL, [schemas, tableTypes]),
    query(CONSTRAINTS_SQL, [schemas]),
    query(ENUMS_SQL, []),
  ]);

  const enumValues = new Map<string, string[]>();
  for (const e of enums) enumValues.set(`${e.enum_schema}.${e.enum_name}`, toArray(e.labels));

  const tables = new Map<string, TableInfo>();
  const key = (schema: unknown, name: unknown) => `${String(schema)}.${String(name)}`;
  for (const c of columns) {
    const k = key(c.table_schema, c.table_name);
    let table = tables.get(k);
    if (!table) {
      table = { name: String(c.table_name), schema: String(c.table_schema), columns: [], foreignKeys: [] };
      if (c.table_comment) table.comment = String(c.table_comment);
      tables.set(k, table);
    }
    const dataType = String(c.data_type);
    const enumKey = key(c.udt_schema, c.udt_name);
    const kind = kindOf(dataType, enumValues.has(enumKey));
    const column: ColumnInfo = {
      name: String(c.column_name),
      kind,
      dbType: dataType === "USER-DEFINED" ? String(c.udt_name) : dataType,
      nullable: c.is_nullable === "YES",
    };
    if (kind === "enum") column.enumValues = enumValues.get(enumKey)!;
    if (dataType === "timestamp with time zone") column.withTimezone = true;
    if (dataType === "timestamp without time zone") column.withTimezone = false;
    if (c.column_comment) column.comment = String(c.column_comment);
    table.columns.push(column);
  }

  for (const con of constraints) {
    const table = tables.get(key(con.table_schema, con.table_name));
    if (!table) continue;
    const cols = toArray(con.columns);
    if (con.contype === "p") {
      for (const col of table.columns) if (cols.includes(col.name)) col.primaryKey = true;
    } else {
      const fk: ForeignKeyInfo = {
        columns: cols,
        referencedTable: String(con.ref_table),
        referencedSchema: String(con.ref_schema),
        referencedColumns: toArray(con.ref_columns),
      };
      table.foreignKeys.push(fk);
    }
  }

  return { tables: [...tables.values()] };
}

function kindOf(dataType: string, isEnum: boolean): ColumnKind {
  if (isEnum) return "enum";
  switch (dataType) {
    case "smallint":
    case "integer":
    case "bigint":
      return "integer";
    case "numeric":
    case "real":
    case "double precision":
    case "money":
      return "number";
    case "boolean":
      return "boolean";
    case "text":
    case "character varying":
    case "character":
    case "citext":
      return "string";
    case "uuid":
      return "uuid";
    case "timestamp with time zone":
    case "timestamp without time zone":
      return "timestamp";
    case "date":
      return "date";
    case "json":
    case "jsonb":
      return "json";
    default:
      return "unknown";
  }
}

/** Some drivers return Postgres arrays as `{a,b}` strings. */
function toArray(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String);
  if (typeof value === "string" && value.startsWith("{") && value.endsWith("}")) {
    const inner = value.slice(1, -1);
    return inner === "" ? [] : inner.split(",").map((s) => s.replace(/^"|"$/g, ""));
  }
  return [];
}
