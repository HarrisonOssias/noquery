import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import type { AuthContext, LLMRequest, QueryIR, SchemaGraph } from "@noquery/core";
import {
  buildCatalog,
  catalogToText,
  createPlanner,
  draftSemantics,
  explainQuery,
  mergeSemantics,
  plannerSchema,
  resolveQuery,
  suggestChart,
  toPlannerResult,
} from "@noquery/core";
import { describe, expect, it } from "vitest";
import { authA, fleet, ORG_A, ORG_B } from "../../core/test/fixtures.js";
import { postgresDialect } from "../src/index.js";

/**
 * The Python package (clients/python) is a port of the TypeScript engine. This file writes down what
 * the TypeScript engine produces for a fixed set of inputs; the Python test suite reads the same
 * file and must produce identical output (tests/test_parity.py).
 *
 * After an intended behavior change, regenerate the file and port the change to Python:
 *   NOQUERY_UPDATE_PARITY=1 pnpm test
 */
const FIXTURE = fileURLToPath(new URL("../../../clients/python/tests/fixtures/parity.json", import.meta.url));

const clinical: AuthContext = { userId: "u2", orgId: ORG_A, roles: ["clinical"] };
const multiOrg: AuthContext = { userId: "u3", orgId: [ORG_A, ORG_B], roles: [] };
const noOrg: AuthContext = { userId: "u4" };

/** `ir` is deliberately loose: several cases are malformed on purpose. */
const QUERIES: Array<{ name: string; auth: AuthContext; ir: unknown; options?: { defaultTimezone?: string; defaultLimit?: number; maxLimit?: number } }> = [
  { name: "count", auth: authA, ir: { version: 1, measures: ["trips.count"] } },
  { name: "all measure kinds", auth: authA, ir: { version: 1, measures: ["trips.count", "trips.avg_duration_min", "trips.total_distance_km", "trips.unique_devices"] } },
  { name: "sql measure without agg", auth: authA, ir: { version: 1, measures: ["devices.active_count", "devices.count"], dimensions: ["devices.model"] } },
  { name: "joined dimension", auth: authA, ir: { version: 1, measures: ["trips.count"], dimensions: ["devices.model"] } },
  { name: "two-hop join", auth: authA, ir: { version: 1, measures: ["trips.count"], dimensions: ["organizations.region", "devices.model"] } },
  { name: "filter-only join", auth: authA, ir: { version: 1, measures: ["trips.count"], filters: [{ field: "organizations.region", op: "eq", value: "east" }] } },
  { name: "distinct dimensions", auth: authA, ir: { version: 1, dimensions: ["devices.model", "devices.status"] } },
  { name: "duplicate fields", auth: authA, ir: { version: 1, measures: ["trips.count", "trips.count"], dimensions: ["devices.model", "devices.model"] } },
  {
    name: "time bucket with preset",
    auth: authA,
    ir: { version: 1, measures: ["trips.count"], time: { field: "trips.started_at", grain: "week", range: "last_30_days", tz: "America/Toronto" } },
  },
  { name: "hour grain default tz", auth: authA, ir: { version: 1, measures: ["trips.count"], time: { field: "trips.started_at", grain: "hour", range: "today" } }, options: { defaultTimezone: "Europe/Paris" } },
  { name: "range without grain", auth: authA, ir: { version: 1, measures: ["trips.count"], time: { field: "trips.started_at", range: "yesterday" } } },
  { name: "grain without range", auth: authA, ir: { version: 1, measures: ["trips.count"], time: { field: "trips.started_at", grain: "month" } } },
  { name: "time field only", auth: authA, ir: { version: 1, time: { field: "trips.started_at", grain: "day" } } },
  { name: "time on joined entity", auth: authA, ir: { version: 1, measures: ["trips.count"], time: { field: "devices.installed_at", grain: "year", range: "this_year" } } },
  { name: "absolute date range", auth: authA, ir: { version: 1, measures: ["trips.count"], time: { field: "trips.started_at", range: { from: "2026-03-01", to: "2026-04-01" } } } },
  {
    name: "absolute instant range",
    auth: authA,
    ir: { version: 1, measures: ["trips.count"], time: { field: "trips.started_at", range: { from: "2026-03-01T00:00:00Z", to: "2026-03-02T12:30:00.5+02:00" } } },
  },
  { name: "mixed date and instant range", auth: authA, ir: { version: 1, measures: ["trips.count"], time: { field: "trips.started_at", range: { from: "2026-03-01", to: "2026-03-01T00:00:00Z" } } } },
  ...["today", "yesterday", "last_7_days", "last_14_days", "last_90_days", "this_week", "last_week", "this_month", "last_month", "this_quarter", "last_quarter", "this_year", "last_year"].map((range) => ({
    name: `preset ${range}`,
    auth: authA,
    ir: { version: 1, measures: ["trips.count"], time: { field: "trips.started_at", range } },
  })),
  {
    name: "every dimension filter op",
    auth: authA,
    ir: {
      version: 1,
      measures: ["trips.count"],
      filters: [
        { field: "devices.status", op: "eq", value: "active" },
        { field: "devices.status", op: "neq", value: "retired" },
        { field: "trips.distance_m", op: "gt", value: 10 },
        { field: "trips.distance_m", op: "gte", value: 10.5 },
        { field: "trips.distance_m", op: "lt", value: 1000 },
        { field: "trips.distance_m", op: "lte", value: 999.25 },
        { field: "devices.status", op: "in", value: ["active", "inactive"] },
        { field: "devices.model", op: "not_in", value: ["X9"] },
        { field: "trips.note", op: "contains", value: "50%_o'brien\\" },
        { field: "trips.note", op: "starts_with", value: "re" },
        { field: "trips.note", op: "is_null" },
        { field: "trips.note", op: "is_not_null" },
        { field: "trips.is_assisted", op: "eq", value: true },
        { field: "trips.is_assisted", op: "neq", value: false },
      ],
    },
  },
  {
    name: "time filters",
    auth: authA,
    ir: {
      version: 1,
      measures: ["trips.count"],
      filters: [
        { field: "trips.started_at", op: "gte", value: "2026-01-01" },
        { field: "trips.started_at", op: "lt", value: "2026-02-01T00:00:00-05:00" },
        { field: "devices.installed_at", op: "in", value: ["2025-06-01", "2025-07-01T00:00:00Z"] },
        { field: "devices.installed_at", op: "not_in", value: ["2025-08-01"] },
        { field: "devices.installed_at", op: "neq", value: "2025-09-01" },
      ],
    },
  },
  {
    name: "measure filters",
    auth: authA,
    ir: {
      version: 1,
      measures: ["trips.count"],
      dimensions: ["devices.model"],
      filters: [
        { field: "trips.count", op: "gt", value: 10 },
        { field: "trips.total_distance_km", op: "neq", value: 0 },
        { field: "trips.avg_duration_min", op: "lte", value: 12.5 },
      ],
    },
  },
  { name: "measure filter only", auth: authA, ir: { version: 1, dimensions: ["devices.model"], filters: [{ field: "devices.count", op: "gte", value: 2 }] } },
  {
    name: "order and limit",
    auth: authA,
    ir: { version: 1, measures: ["trips.count", "trips.total_distance_km"], dimensions: ["devices.model"], orderBy: [{ field: "trips.total_distance_km", dir: "asc" }, { field: "devices.model", dir: "desc" }], limit: 5 },
  },
  {
    name: "order by time field",
    auth: authA,
    ir: { version: 1, measures: ["trips.count"], dimensions: ["devices.model"], time: { field: "trips.started_at", grain: "day", range: "last_7_days" }, orderBy: [{ field: "trips.started_at", dir: "desc" }], limit: 100 },
  },
  { name: "custom default limit", auth: authA, ir: { version: 1, measures: ["trips.count"] }, options: { defaultLimit: 25, maxLimit: 50 } },
  { name: "list-valued row scope", auth: multiOrg, ir: { version: 1, measures: ["trips.count"], dimensions: ["organizations.name"] } },
  { name: "role-gated entity allowed", auth: clinical, ir: { version: 1, measures: ["patients.count"], dimensions: ["patients.ward"] } },

  // --- rejected -------------------------------------------------------------------------------
  { name: "not an object", auth: authA, ir: "select 1" },
  { name: "wrong version", auth: authA, ir: { version: 2, measures: ["trips.count"] } },
  { name: "empty", auth: authA, ir: { version: 1 } },
  { name: "unknown entity", auth: authA, ir: { version: 1, measures: ["nope.count"] } },
  { name: "unknown field", auth: authA, ir: { version: 1, measures: ["trips.nope"], dimensions: ["devices.nope"] } },
  { name: "malformed references", auth: authA, ir: { version: 1, measures: ["trips"], dimensions: ["devices.model; drop table trips", 42, true, null] } },
  { name: "role-gated entity denied", auth: authA, ir: { version: 1, measures: ["patients.count"] } },
  { name: "hidden field", auth: clinical, ir: { version: 1, dimensions: ["patients.email"] } },
  { name: "aggregate-only field", auth: authA, ir: { version: 1, measures: ["devices.count"], dimensions: ["devices.serial"], filters: [{ field: "devices.id", op: "eq", value: "x" }] } },
  { name: "wrong field kinds", auth: authA, ir: { version: 1, measures: ["devices.model"], dimensions: ["trips.count"] } },
  { name: "mixed measure entities", auth: authA, ir: { version: 1, measures: ["trips.count", "devices.count"] } },
  { name: "fan-out join", auth: authA, ir: { version: 1, measures: ["devices.count"], dimensions: ["trips.note"] } },
  { name: "no join path", auth: clinical, ir: { version: 1, measures: ["patients.count"], dimensions: ["devices.model"] } },
  { name: "missing auth attribute", auth: noOrg, ir: { version: 1, measures: ["trips.count"], dimensions: ["devices.model"] } },
  { name: "empty list auth attribute", auth: { orgId: [] }, ir: { version: 1, measures: ["trips.count"] } },
  {
    name: "bad time",
    auth: authA,
    ir: { version: 1, measures: ["trips.count"], time: { field: "devices.model", grain: "fortnight", range: "last_3_days", tz: "Mars/Olympus" } },
  },
  { name: "time field is a measure", auth: authA, ir: { version: 1, time: { field: "trips.count", grain: "day" } } },
  { name: "bad absolute range", auth: authA, ir: { version: 1, measures: ["trips.count"], time: { field: "trips.started_at", range: { from: "March 1", to: "2026-04-01" } } } },
  { name: "range without offset", auth: authA, ir: { version: 1, measures: ["trips.count"], time: { field: "trips.started_at", range: { from: "2026-03-01", to: "2026-04-01T00:00:00" } } } },
  { name: "reversed range", auth: authA, ir: { version: 1, measures: ["trips.count"], time: { field: "trips.started_at", range: { from: "2026-04-01", to: "2026-03-01" } } } },
  { name: "range of the wrong type", auth: authA, ir: { version: 1, measures: ["trips.count"], time: { field: "trips.started_at", range: 7 } } },
  {
    name: "bad filters",
    auth: authA,
    ir: {
      version: 1,
      measures: ["trips.count"],
      filters: [
        { field: "devices.status", op: "like", value: "a" },
        { field: "devices.status", op: "eq", value: "broken" },
        { field: "devices.status", op: "eq" },
        { field: "devices.status", op: "eq", value: ["active"] },
        { field: "devices.status", op: "in", value: "active" },
        { field: "devices.status", op: "in", value: [] },
        { field: "devices.status", op: "in", value: ["active", 3] },
        { field: "trips.note", op: "is_null", value: "x" },
        { field: "trips.distance_m", op: "contains", value: "1" },
        { field: "trips.note", op: "contains", value: 1 },
        { field: "trips.distance_m", op: "gt", value: "10" },
        { field: "trips.distance_m", op: "eq", value: true },
        { field: "trips.is_assisted", op: "gt", value: true },
        { field: "trips.is_assisted", op: "eq", value: "true" },
        { field: "trips.started_at", op: "gte", value: "last week" },
        { field: "trips.started_at", op: "gte", value: 20260101 },
        { field: "trips.count", op: "in", value: [1] },
        { field: "trips.count", op: "gt", value: "10" },
        null,
        "devices.status = 'active'",
      ],
    },
  },
  {
    name: "bad order by",
    auth: authA,
    ir: { version: 1, measures: ["trips.count"], orderBy: [{ field: "devices.model", dir: "asc" }, { field: "trips.count", dir: "down" }, null] },
  },
  ...[0, -1, 1.5, 10001, "10"].map((limit) => ({ name: `bad limit ${JSON.stringify(limit)}`, auth: authA, ir: { version: 1, measures: ["trips.count"], limit } })),
];

const SCHEMA_GRAPH: SchemaGraph = {
  tables: [
    {
      name: "organizations",
      schema: "public",
      comment: "Customer organizations",
      columns: [
        { name: "id", kind: "uuid", dbType: "uuid", nullable: false, primaryKey: true },
        { name: "name", kind: "string", dbType: "text", nullable: false },
        { name: "contactEmail", kind: "string", dbType: "text", nullable: true },
        { name: "plan", kind: "enum", dbType: "plan", nullable: false, enumValues: ["free", "pro"] },
      ],
      foreignKeys: [],
    },
    {
      name: "trips",
      schema: "public",
      columns: [
        { name: "id", kind: "integer", dbType: "bigint", nullable: false, primaryKey: true },
        { name: "org_id", kind: "uuid", dbType: "uuid", nullable: false },
        { name: "created_at", kind: "timestamp", dbType: "timestamp without time zone", nullable: false, withTimezone: false },
        { name: "started_at", kind: "timestamp", dbType: "timestamp with time zone", nullable: false, withTimezone: true },
        { name: "service_date", kind: "date", dbType: "date", nullable: true },
        { name: "distance_m", kind: "number", dbType: "double precision", nullable: true, comment: "Metres travelled" },
        { name: "seat_count", kind: "integer", dbType: "integer", nullable: true },
        { name: "route_code", kind: "integer", dbType: "integer", nullable: true },
        { name: "is_assisted", kind: "boolean", dbType: "boolean", nullable: false },
        { name: "payload", kind: "json", dbType: "jsonb", nullable: true },
        { name: "driver_phone", kind: "string", dbType: "text", nullable: true },
        { name: "api_token_cost", kind: "number", dbType: "numeric", nullable: true },
      ],
      foreignKeys: [{ columns: ["org_id"], referencedTable: "organizations", referencedSchema: "public", referencedColumns: ["id"] }],
    },
    {
      name: "trips",
      schema: "archive",
      columns: [
        { name: "trip_id", kind: "integer", dbType: "bigint", nullable: false, primaryKey: true },
        { name: "version", kind: "integer", dbType: "integer", nullable: false, primaryKey: true },
        { name: "archived_at", kind: "timestamp", dbType: "timestamp with time zone", nullable: false, withTimezone: true },
      ],
      foreignKeys: [{ columns: ["trip_id"], referencedTable: "trips", referencedSchema: "public", referencedColumns: ["id"] }],
    },
    {
      name: "audit-log",
      columns: [{ name: "id", kind: "integer", dbType: "integer", nullable: false, primaryKey: true }],
      foreignKeys: [{ columns: ["id"], referencedTable: "missing", referencedColumns: ["id"] }],
    },
  ],
};

const OVERLAY = {
  entities: {
    organizations: {
      label: "Customer",
      rowScope: [{ column: "id", authKey: "orgId" }],
      dimensions: { plan: { label: "Plan tier", synonyms: ["tier"] }, name: null, tier_rank: { type: "number" as const, sql: "length(${TABLE}.name)" } },
      measures: { count: { label: "Customers" }, pro_count: { sql: "count(*) FILTER (WHERE ${TABLE}.plan = 'pro')" } },
    },
    audit_log: null,
    regions: { table: "regions", dimensions: { name: { type: "string" as const, column: "name" } }, measures: { count: { agg: "count" as const } } },
  },
  joins: [{ from: "organizations", to: "regions", on: [["region_id", "id"]] as Array<[string, string]>, relationship: "many_to_one" as const }],
  removeJoins: [{ from: "archive_trips", to: "public_trips" }],
};

const rawQuery = (query: Record<string, unknown>) => ({
  kind: "query",
  query: { measures: [], dimensions: [], time: null, filters: [], order_by: [], limit: null, ...query },
  clarification: null,
  reason: null,
});

const RAW_ANSWERS: unknown[] = [
  null,
  { kind: "clarify", query: null, clarification: { question: "Which trips?", options: ["All", "Assisted"] }, reason: null },
  { kind: "clarify", query: null, clarification: null, reason: null },
  { kind: "unanswerable", query: null, clarification: null, reason: "No battery data." },
  { kind: "query", query: null, clarification: null, reason: null },
  rawQuery({ measures: ["trips.count"], dimensions: ["devices.model"], limit: 10, order_by: [{ field: "trips.count", dir: "desc" }] }),
  rawQuery({ measures: ["trips.count"], time: { field: "trips.started_at", grain: "day", range_preset: "last_week", range_from: null, range_to: null } }),
  rawQuery({ measures: ["trips.count"], time: { field: "trips.started_at", grain: null, range_preset: null, range_from: "2026-03-01", range_to: "2026-04-01" } }),
  rawQuery({ measures: ["trips.count"], time: { field: "trips.started_at", grain: null, range_preset: null, range_from: "2026-03-01", range_to: null } }),
  rawQuery({
    measures: ["trips.count"],
    filters: [
      { field: "trips.distance_m", op: "gt", values: ["42"] },
      { field: "trips.distance_m", op: "lte", values: [" 1.50 "] },
      { field: "trips.distance_m", op: "in", values: ["1e3", "-0.5", "abc", ""] },
      { field: "trips.is_assisted", op: "eq", values: ["true"] },
      { field: "trips.is_assisted", op: "neq", values: ["false"] },
      { field: "trips.is_assisted", op: "eq", values: ["maybe"] },
      { field: "devices.status", op: "in", values: ["active", "retired"] },
      { field: "devices.model", op: "eq", values: ["42"] },
      { field: "trips.note", op: "is_null", values: [] },
      { field: "trips.note", op: "is_not_null", values: ["ignored"] },
      { field: "trips.count", op: "gte", values: ["10"] },
      { field: "trips.count", op: "gt", values: [] },
      { field: "trips.started_at", op: "gte", values: ["2026-01-01"] },
    ],
  }),
];

const NOW = "2026-03-01T04:30:00.000Z";

async function plannerRequests(): Promise<LLMRequest[]> {
  const requests: LLMRequest[] = [];
  const provider = {
    name: "recorder",
    generateJson: async (request: LLMRequest) => {
      requests.push(request);
      return { kind: "unanswerable", query: null, clarification: null, reason: "recorded" };
    },
  };
  const catalog = buildCatalog(fleet, authA);
  const first: QueryIR = { version: 1, measures: ["trips.count"], dimensions: ["organizations.region"] };
  await createPlanner(provider).plan({ catalog, question: "How many trips today?", timezone: "America/Toronto", now: new Date(NOW) });
  await createPlanner(provider, {
    instructions: "A trip is one continuous movement.",
    examples: [{ question: "Trips by region", ir: first }],
  }).plan({
    catalog,
    question: "Now only the east region",
    timezone: "Asia/Tokyo",
    now: new Date(NOW),
    history: [
      { question: "Trips by region", result: { kind: "query", ir: first } },
      { question: "And battery?", result: { kind: "unanswerable", reason: "No battery data." } },
    ],
    repair: {
      previous: { kind: "query", ir: { version: 1, measures: ["trips.nope"] } },
      errors: [{ code: "unknown_field", message: 'Unknown field "trips.nope".', path: "measures[0]" }],
    },
  });
  return requests;
}

async function build() {
  const numeric = postgresDialect();
  const noProbe = postgresDialect({ probeTruncation: false });
  const queries = QUERIES.map(({ name, auth, ir, options }) => {
    const resolved = resolveQuery(fleet, ir as QueryIR, auth, options);
    if (!resolved.ok) return { name, auth, ir, options: options ?? {}, ok: false, errors: resolved.errors };
    const compiled = numeric.compile(resolved.query);
    return {
      name,
      auth,
      ir,
      options: options ?? {},
      ok: true,
      sql: compiled.sql,
      params: compiled.params,
      sqlNoProbe: noProbe.compile(resolved.query).sql,
      columns: resolved.query.columns,
      explanation: explainQuery(resolved.query),
      chart: suggestChart(resolved.query.columns),
    };
  });

  const catalogs = [authA, clinical, noOrg].map((auth) => {
    const catalog = buildCatalog(fleet, auth);
    return { auth, catalog, text: catalogToText(catalog), schema: plannerSchema(catalog) };
  });

  const draft = draftSemantics(SCHEMA_GRAPH);
  const catalog = buildCatalog(fleet, authA);
  return {
    model: fleet,
    queries,
    catalogs,
    draft: { graph: SCHEMA_GRAPH, model: draft, excluded: draftSemantics(SCHEMA_GRAPH, { excludeTables: ["trips"], timePreference: ["archived_at"] }) },
    merge: { overlay: OVERLAY, model: mergeSemantics(draft, OVERLAY) },
    planner: {
      auth: authA,
      now: NOW,
      results: RAW_ANSWERS.map((raw) => ({ raw, result: toPlannerResult(raw as never, catalog) })),
      requests: await plannerRequests(),
    },
  };
}

describe("parity fixture for the Python package", () => {
  it("matches clients/python/tests/fixtures/parity.json", async () => {
    // Round-trip through JSON so `undefined` fields drop out, as they do in the file.
    const current = JSON.parse(JSON.stringify(await build()));
    if (process.env.NOQUERY_UPDATE_PARITY === "1" || !existsSync(FIXTURE)) {
      mkdirSync(dirname(FIXTURE), { recursive: true });
      writeFileSync(FIXTURE, `${JSON.stringify(current, null, 2)}\n`);
    }
    expect(current).toEqual(JSON.parse(readFileSync(FIXTURE, "utf8")));
  });

  it("covers accepted and rejected queries", async () => {
    const { queries } = await build();
    expect(queries.filter((q) => q.ok).length).toBeGreaterThan(30);
    expect(queries.filter((q) => !q.ok).length).toBeGreaterThan(20);
  });
});
