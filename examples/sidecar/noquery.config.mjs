// noquery sidecar config. Start it with:  pnpm --filter noquery-sidecar-example start
//
// Environment:
//   DATABASE_URL           your Postgres; leave unset to use the built-in demo database
//   NOQUERY_SCHEMAS        comma-separated schemas to read (default "public")
//   NOQUERY_TIMEZONE       default time zone for "today", "last week" and buckets (default "UTC")
//   NOQUERY_PLANNER=off    run without an LLM; /v1/ask is disabled, /v1/run still works
//   ANTHROPIC_API_KEY      used by the planner (or sign in once with `ant auth login`)
//   NOQUERY_SERVICE_TOKEN  read by noquery-server itself: the secret your Python code sends
import { createPlanner, draftSemantics, mergeSemantics } from "@noquery/core";
import { introspectPostgres, pgExecutor, pgliteExecutor, postgresDialect } from "@noquery/postgres";
import { anthropicProvider } from "@noquery/anthropic";

let executor;
let catalogQuery;
let overlay = {};

if (process.env.DATABASE_URL) {
  const { default: pg } = await import("pg");
  // Use a read-only database role here if you can. noquery also runs every query in a READ ONLY transaction.
  const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 5 });
  executor = pgExecutor(pool);
  catalogQuery = (sql, params) => pool.query(sql, params).then((r) => r.rows);
  // Put your curation here: descriptions, synonyms, row scopes, hidden columns, custom measures.
  // See the "Semantic layer" section of the README.
  overlay = {};
} else {
  const { createDemoDatabase, demoOverlay } = await import("./demo-data.mjs");
  const db = await createDemoDatabase();
  executor = pgliteExecutor(db);
  catalogQuery = (sql, params) => db.query(sql, params).then((r) => r.rows);
  overlay = demoOverlay;
}

const graph = await introspectPostgres(catalogQuery, {
  schemas: (process.env.NOQUERY_SCHEMAS ?? "public").split(",").map((s) => s.trim()),
});

export default {
  model: mergeSemantics(draftSemantics(graph), overlay),
  dialect: postgresDialect(),
  executor,
  planner: process.env.NOQUERY_PLANNER === "off" ? undefined : createPlanner(anthropicProvider({ effort: "low" })),
  defaultTimezone: process.env.NOQUERY_TIMEZONE ?? "UTC",
  onAudit: (event) => console.log(JSON.stringify({ audit: event.type, outcome: event.outcome, userId: event.userId, ms: event.durationMs })),
};
