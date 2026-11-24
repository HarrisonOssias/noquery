import type { ResolvedQuery } from "./resolve.js";

export interface CompiledQuery {
  sql: string;
  params: unknown[];
}

/** Turns a validated, policy-scoped query into SQL for one database. */
export interface Dialect {
  name: string;
  compile(query: ResolvedQuery): CompiledQuery;
}

export interface ExecuteOptions {
  /** Abort the statement after this many milliseconds. */
  timeoutMs: number;
  /** Return at most this many rows; `truncated` reports whether more existed. */
  maxRows: number;
}

export interface ExecuteResult {
  rows: Array<Record<string, unknown>>;
  truncated: boolean;
}

/**
 * Runs compiled SQL on the host's own connection. Implementations must run read-only
 * and enforce `timeoutMs` at the database.
 */
export interface Executor {
  execute(query: CompiledQuery, options: ExecuteOptions): Promise<ExecuteResult>;
}
