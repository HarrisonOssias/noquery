import type { Catalog } from "./catalog.js";
import { buildCatalog } from "./catalog.js";
import type { ChartSpec } from "./chart.js";
import { suggestChart } from "./chart.js";
import { explainQuery } from "./explain.js";
import type { QueryIR } from "./ir.js";
import type { Planner, PlannerTurn } from "./planner.js";
import type { AuthContext, OutputColumn, QueryError, ResolvedQuery, ResolveResult } from "./resolve.js";
import { resolveQuery } from "./resolve.js";
import type { CompiledQuery, Dialect, Executor } from "./runtime.js";
import type { SemanticModel } from "./semantic.js";
import { checkSemantics } from "./semantic.js";

export interface AuditEvent {
  type: "run" | "ask";
  at: string;
  userId?: string;
  roles?: string[];
  question?: string;
  ir?: QueryIR;
  sql?: string;
  params?: unknown[];
  rowCount?: number;
  durationMs: number;
  outcome: "answer" | "clarify" | "unanswerable" | "invalid" | "error";
  errors?: QueryError[];
  error?: string;
}

export interface EngineOptions {
  model: SemanticModel;
  dialect: Dialect;
  executor: Executor;
  /** Needed only for `ask`. */
  planner?: Planner;
  /** IANA time zone for time buckets and ranges when a query gives none. Default "UTC". */
  defaultTimezone?: string;
  /** Rows returned when a query sets no limit. Default 1000. */
  defaultLimit?: number;
  /** Largest limit a query may ask for. Default 10000. */
  maxLimit?: number;
  /** Statement timeout in milliseconds. Default 15000. */
  timeoutMs?: number;
  /** How many times `ask` lets the planner fix a query the validator rejected. Default 1. */
  maxRepairs?: number;
  /** Called after every run and ask. Use it to keep an audit log. */
  onAudit?: (event: AuditEvent) => void | Promise<void>;
  now?: () => Date;
}

export interface RunResult {
  ir: QueryIR;
  sql: string;
  params: unknown[];
  columns: OutputColumn[];
  rows: Array<Record<string, unknown>>;
  /** True when more rows matched than the limit allowed. */
  truncated: boolean;
  explanation: string;
  chart: ChartSpec;
  durationMs: number;
}

export type AskResult =
  | ({ type: "answer" } & RunResult)
  | { type: "clarify"; question: string; options: string[] }
  | { type: "unanswerable"; reason: string }
  | { type: "invalid"; errors: QueryError[]; ir?: QueryIR };

export interface CallOptions {
  auth: AuthContext;
}

export interface AskOptions extends CallOptions {
  history?: PlannerTurn[];
  /** The asker's IANA time zone; "today" and time buckets follow it. Defaults to the engine's. */
  timezone?: string;
}

export class QueryValidationError extends Error {
  constructor(readonly errors: QueryError[]) {
    super(errors.map((e) => e.message).join(" "));
    this.name = "QueryValidationError";
  }
}

export interface Engine {
  /** True when a planner is configured, so `ask` can be used. */
  readonly hasPlanner: boolean;
  /** The fields this caller may use, for UIs and for the planner. */
  catalog(auth: AuthContext): Catalog;
  validate(ir: QueryIR, auth: AuthContext): ResolveResult;
  /** Validates and compiles without running. Throws QueryValidationError when invalid. */
  compile(ir: QueryIR, auth: AuthContext): CompiledQuery & { query: ResolvedQuery };
  run(ir: QueryIR, options: CallOptions): Promise<RunResult>;
  ask(question: string, options: AskOptions): Promise<AskResult>;
}

export function createEngine(options: EngineOptions): Engine {
  const issues = checkSemantics(options.model);
  if (issues.length > 0) {
    throw new Error(`Semantic model has problems:\n${issues.map((i) => `- ${i.path}: ${i.message}`).join("\n")}`);
  }
  const resolveOptions = {
    defaultTimezone: options.defaultTimezone ?? "UTC",
    defaultLimit: options.defaultLimit ?? 1000,
    maxLimit: options.maxLimit ?? 10000,
  };
  const timeoutMs = options.timeoutMs ?? 15000;
  const maxRepairs = options.maxRepairs ?? 1;
  const now = options.now ?? (() => new Date());

  const audit = async (event: Omit<AuditEvent, "at">, auth: AuthContext) => {
    if (!options.onAudit) return;
    const full: AuditEvent = { ...event, at: now().toISOString() };
    if (auth.userId) full.userId = auth.userId;
    if (auth.roles) full.roles = auth.roles;
    await options.onAudit(full);
  };

  const validate = (ir: QueryIR, auth: AuthContext) => resolveQuery(options.model, ir, auth, resolveOptions);

  const compile = (ir: QueryIR, auth: AuthContext) => {
    const result = validate(ir, auth);
    if (!result.ok) throw new QueryValidationError(result.errors);
    return { ...options.dialect.compile(result.query), query: result.query };
  };

  async function execute(query: ResolvedQuery, started: number): Promise<RunResult> {
    const compiled = options.dialect.compile(query);
    const { rows, truncated } = await options.executor.execute(compiled, { timeoutMs, maxRows: query.limit });
    const normalized = normalizeRows(rows, query.columns);
    return {
      ir: query.ir,
      sql: compiled.sql,
      params: compiled.params,
      columns: query.columns,
      rows: normalized,
      truncated,
      explanation: explainQuery(query),
      chart: suggestChart(query.columns, normalized),
      durationMs: Date.now() - started,
    };
  }

  return {
    hasPlanner: Boolean(options.planner),
    catalog: (auth) => buildCatalog(options.model, auth),
    validate,
    compile,

    async run(ir, { auth }) {
      const started = Date.now();
      const resolved = validate(ir, auth);
      if (!resolved.ok) {
        await audit({ type: "run", ir, durationMs: Date.now() - started, outcome: "invalid", errors: resolved.errors }, auth);
        throw new QueryValidationError(resolved.errors);
      }
      try {
        const result = await execute(resolved.query, started);
        await audit(
          { type: "run", ir, sql: result.sql, params: result.params, rowCount: result.rows.length, durationMs: result.durationMs, outcome: "answer" },
          auth,
        );
        return result;
      } catch (error) {
        await audit({ type: "run", ir, durationMs: Date.now() - started, outcome: "error", error: errorMessage(error) }, auth);
        throw error;
      }
    },

    async ask(question, { auth, history, timezone: askTimezone }) {
      if (!options.planner) throw new Error("ask() needs a planner. Pass `planner` to createEngine.");
      const started = Date.now();
      const catalog = buildCatalog(options.model, auth);
      const timezone = askTimezone ?? resolveOptions.defaultTimezone;
      const base = { catalog, question, timezone, now: now(), ...(history ? { history } : {}) };

      let plan = await options.planner.plan(base);
      let lastErrors: QueryError[] = [];
      for (let attempt = 0; ; attempt++) {
        if (plan.kind === "query" && plan.ir.time && !plan.ir.time.tz && askTimezone) {
          plan = { ...plan, ir: { ...plan.ir, time: { ...plan.ir.time, tz: askTimezone } } };
        }
        if (plan.kind === "clarify") {
          await audit({ type: "ask", question, durationMs: Date.now() - started, outcome: "clarify" }, auth);
          return { type: "clarify", question: plan.question, options: plan.options };
        }
        if (plan.kind === "unanswerable") {
          await audit({ type: "ask", question, durationMs: Date.now() - started, outcome: "unanswerable" }, auth);
          return { type: "unanswerable", reason: plan.reason };
        }
        const resolved = validate(plan.ir, auth);
        if (resolved.ok) {
          try {
            const result = await execute(resolved.query, started);
            await audit(
              { type: "ask", question, ir: plan.ir, sql: result.sql, params: result.params, rowCount: result.rows.length, durationMs: result.durationMs, outcome: "answer" },
              auth,
            );
            return { type: "answer", ...result };
          } catch (error) {
            await audit({ type: "ask", question, ir: plan.ir, durationMs: Date.now() - started, outcome: "error", error: errorMessage(error) }, auth);
            throw error;
          }
        }
        lastErrors = resolved.errors;
        if (attempt >= maxRepairs) {
          await audit({ type: "ask", question, ir: plan.ir, durationMs: Date.now() - started, outcome: "invalid", errors: lastErrors }, auth);
          return { type: "invalid", errors: lastErrors, ir: plan.ir };
        }
        plan = await options.planner.plan({ ...base, repair: { previous: plan, errors: lastErrors } });
      }
    },
  };
}

/** Drivers often return counts and decimals as strings; measures come back as numbers. */
function normalizeRows(rows: Array<Record<string, unknown>>, columns: OutputColumn[]) {
  const measureKeys = columns.filter((c) => c.role === "measure").map((c) => c.key);
  if (measureKeys.length === 0) return rows;
  return rows.map((row) => {
    const out = { ...row };
    for (const key of measureKeys) {
      const v = out[key];
      if (typeof v === "bigint") out[key] = Number(v);
      else if (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v))) out[key] = Number(v);
    }
    return out;
  });
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
