import type { ColumnInfo, ColumnKind, ForeignKeyInfo, SchemaGraph, TableInfo } from "@noquery/core";
import { is } from "drizzle-orm";
import { PgTable, getTableConfig } from "drizzle-orm/pg-core";

export interface IntrospectDrizzleOptions {
  /** Only include these table names. */
  include?: string[];
  /** Leave out these table names. */
  exclude?: string[];
}

const KIND_BY_COLUMN_TYPE: Record<string, ColumnKind> = {
  PgUUID: "uuid",
  PgText: "string",
  PgVarchar: "string",
  PgChar: "string",
  PgCidr: "string",
  PgInet: "string",
  PgMacaddr: "string",
  PgSmallInt: "integer",
  PgInteger: "integer",
  PgSerial: "integer",
  PgSmallSerial: "integer",
  PgBigInt53: "integer",
  PgBigInt64: "integer",
  PgBigSerial53: "integer",
  PgBigSerial64: "integer",
  PgNumeric: "number",
  PgReal: "number",
  PgDoublePrecision: "number",
  PgBoolean: "boolean",
  PgTimestamp: "timestamp",
  PgTimestampString: "timestamp",
  PgDate: "date",
  PgDateString: "date",
  PgJson: "json",
  PgJsonb: "json",
  PgEnumColumn: "enum",
};

/**
 * Reads Drizzle `pgTable` definitions into a schema graph.
 * Pass your schema module: `introspectDrizzle(await import("./db/schema"))`.
 */
export function introspectDrizzle(
  schema: Record<string, unknown>,
  options: IntrospectDrizzleOptions = {},
): SchemaGraph {
  const tables: TableInfo[] = [];
  const seen = new Set<unknown>();
  for (const value of Object.values(schema)) {
    if (!is(value, PgTable) || seen.has(value)) continue;
    seen.add(value);
    const config = getTableConfig(value);
    if (options.include && !options.include.includes(config.name)) continue;
    if (options.exclude?.includes(config.name)) continue;

    const compositePk = new Set(config.primaryKeys.flatMap((pk) => pk.columns.map((c) => c.name)));
    const columns: ColumnInfo[] = config.columns.map((column) => {
      const columnType = column.columnType;
      const kind = KIND_BY_COLUMN_TYPE[columnType] ?? kindFromDataType(column.dataType);
      const info: ColumnInfo = {
        name: column.name,
        kind,
        dbType: column.getSQLType(),
        nullable: !column.notNull,
      };
      if (column.primary || compositePk.has(column.name)) info.primaryKey = true;
      if (column.enumValues && column.enumValues.length > 0) {
        info.enumValues = [...column.enumValues];
        if (kind === "string") info.kind = "enum";
      }
      if (kind === "timestamp") {
        info.withTimezone = Boolean((column as unknown as { withTimezone?: boolean }).withTimezone);
      }
      return info;
    });

    const foreignKeys: ForeignKeyInfo[] = config.foreignKeys.map((fk) => {
      const ref = fk.reference();
      const target = getTableConfig(ref.foreignTable);
      const out: ForeignKeyInfo = {
        columns: ref.columns.map((c) => c.name),
        referencedTable: target.name,
        referencedColumns: ref.foreignColumns.map((c) => c.name),
      };
      if (target.schema) out.referencedSchema = target.schema;
      return out;
    });

    const table: TableInfo = { name: config.name, columns, foreignKeys };
    if (config.schema) table.schema = config.schema;
    tables.push(table);
  }
  return { tables };
}

function kindFromDataType(dataType: string): ColumnKind {
  switch (dataType) {
    case "string":
      return "string";
    case "number":
      return "number";
    case "bigint":
      return "integer";
    case "boolean":
      return "boolean";
    case "date":
      return "timestamp";
    case "json":
      return "json";
    default:
      return "unknown";
  }
}
