import type { CompiledQuery, ExecuteOptions, ExecuteResult, Executor } from "@noquery/core";

type Row = Record<string, unknown>;

function finish(rows: Row[], options: ExecuteOptions): ExecuteResult {
  if (rows.length > options.maxRows) return { rows: rows.slice(0, options.maxRows), truncated: true };
  return { rows, truncated: false };
}

function timeoutStatement(ms: number): string {
  const value = Math.max(1, Math.floor(ms));
  return `SET LOCAL statement_timeout = ${value}`;
}

/** Minimal shape of a `pg` Pool (node-postgres). Drizzle's `db.$client` is one. */
export interface PgPoolLike {
  connect(): Promise<PgClientLike>;
}

export interface PgClientLike {
  query(text: string, values?: unknown[]): Promise<{ rows: Row[] }>;
  release(error?: Error | boolean): void;
}

/** Runs each query in its own read-only transaction with a statement timeout. */
export function pgExecutor(pool: PgPoolLike): Executor {
  return {
    async execute(query: CompiledQuery, options: ExecuteOptions) {
      const client = await pool.connect();
      // Only a failed ROLLBACK leaves the connection in an unknown state; then it is discarded.
      let broken: Error | undefined;
      try {
        await client.query("BEGIN READ ONLY");
        await client.query(timeoutStatement(options.timeoutMs));
        const result = await client.query(query.sql, query.params);
        return finish(result.rows, options);
      } finally {
        try {
          await client.query("ROLLBACK");
        } catch (error) {
          broken = error instanceof Error ? error : new Error(String(error));
        }
        client.release(broken);
      }
    },
  };
}

/** Minimal shape of a postgres.js `sql` instance. */
export interface PostgresJsLike {
  begin<T>(options: string, callback: (sql: PostgresJsTxLike) => Promise<T>): Promise<T>;
}

export interface PostgresJsTxLike {
  unsafe(query: string, params?: unknown[]): PromiseLike<Row[]>;
}

/** Executor for postgres.js (`import postgres from "postgres"`). */
export function postgresJsExecutor(sql: PostgresJsLike): Executor {
  return {
    async execute(query: CompiledQuery, options: ExecuteOptions) {
      const rows = await sql.begin("read only", async (tx) => {
        await tx.unsafe(timeoutStatement(options.timeoutMs));
        return Array.from(await tx.unsafe(query.sql, query.params));
      });
      return finish(rows, options);
    },
  };
}

/** Minimal shape of a PGlite instance (in-process Postgres, handy for tests and demos). */
export interface PGliteLike {
  transaction<T>(callback: (tx: PGliteTxLike) => Promise<T>): Promise<T>;
}

export interface PGliteTxLike {
  query<T = Row>(query: string, params?: unknown[]): Promise<{ rows: T[] }>;
  exec(query: string): Promise<unknown>;
}

/** Executor for PGlite. Read-only is enforced with SET TRANSACTION READ ONLY. */
export function pgliteExecutor(db: PGliteLike): Executor {
  return {
    async execute(query: CompiledQuery, options: ExecuteOptions) {
      const rows = await db.transaction(async (tx) => {
        await tx.exec("SET TRANSACTION READ ONLY");
        await tx.exec(timeoutStatement(options.timeoutMs));
        const result = await tx.query<Row>(query.sql, query.params);
        return result.rows;
      });
      return finish(rows, options);
    },
  };
}
