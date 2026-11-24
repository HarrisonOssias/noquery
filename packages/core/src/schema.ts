/**
 * The schema graph is the canonical, ORM-independent description of a database.
 * Every introspection adapter (Drizzle, Prisma, raw catalog queries, ...) emits this shape.
 */

export type ColumnKind =
  | "string"
  | "integer"
  | "number"
  | "boolean"
  | "timestamp"
  | "date"
  | "uuid"
  | "enum"
  | "json"
  | "unknown";

export interface ColumnInfo {
  name: string;
  kind: ColumnKind;
  /** The database type as the database spells it, e.g. `timestamp with time zone`. */
  dbType: string;
  nullable: boolean;
  primaryKey?: boolean;
  enumValues?: string[];
  /** For timestamps: true when the column stores an absolute instant (`timestamptz`). */
  withTimezone?: boolean;
  comment?: string;
}

export interface ForeignKeyInfo {
  columns: string[];
  referencedTable: string;
  referencedSchema?: string;
  referencedColumns: string[];
}

export interface TableInfo {
  name: string;
  schema?: string;
  columns: ColumnInfo[];
  foreignKeys: ForeignKeyInfo[];
  comment?: string;
}

export interface SchemaGraph {
  tables: TableInfo[];
}

export interface SchemaAdapter {
  introspect(): Promise<SchemaGraph> | SchemaGraph;
}
