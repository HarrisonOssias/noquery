import { PGlite } from "@electric-sql/pglite";
import type { Engine, QueryIR } from "@noquery/core";
import { createEngine, draftSemantics, mergeSemantics } from "@noquery/core";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { authA, fleet, ORG_A, ORG_B } from "../../core/test/fixtures.js";
import { introspectPostgres, pgliteExecutor, postgresDialect } from "../src/index.js";

const D1 = "00000000-0000-0000-0000-0000000000d1";
const D2 = "00000000-0000-0000-0000-0000000000d2";
const D3 = "00000000-0000-0000-0000-0000000000d3";

const SCHEMA = `
CREATE TYPE device_status AS ENUM ('active', 'inactive', 'retired');
CREATE TABLE organizations (id uuid PRIMARY KEY, name text NOT NULL, region text NOT NULL);
COMMENT ON TABLE organizations IS 'Customer organizations';
CREATE TABLE devices (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES organizations(id),
  serial text NOT NULL,
  status device_status NOT NULL,
  model text NOT NULL,
  installed_at timestamptz NOT NULL
);
CREATE TABLE trips (
  id bigserial PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES organizations(id),
  device_id uuid NOT NULL REFERENCES devices(id),
  started_at timestamptz NOT NULL,
  ended_at timestamptz NOT NULL,
  distance_m double precision,
  is_assisted boolean NOT NULL DEFAULT false,
  note text
);
COMMENT ON COLUMN trips.distance_m IS 'Metres travelled';
CREATE TABLE patients (id serial PRIMARY KEY, email text, ward text);

INSERT INTO organizations VALUES ('${ORG_A}', 'Acme Care', 'east'), ('${ORG_B}', 'Bayside', 'west');
INSERT INTO devices VALUES
  ('${D1}', '${ORG_A}', 'SN-1', 'active',  'X1', '2025-06-01T00:00:00Z'),
  ('${D2}', '${ORG_A}', 'SN-2', 'retired', 'X2', '2025-07-01T00:00:00Z'),
  ('${D3}', '${ORG_B}', 'SN-3', 'active',  'X1', '2025-08-01T00:00:00Z');
INSERT INTO trips (org_id, device_id, started_at, ended_at, distance_m, is_assisted, note) VALUES
  ('${ORG_A}', '${D1}', '2026-03-01T04:30:00Z', '2026-03-01T04:40:00Z', 1000, true,  '100% fine'),
  ('${ORG_A}', '${D1}', '2026-03-01T06:00:00Z', '2026-03-01T06:20:00Z', 2000, false, 'bumpy_ride'),
  ('${ORG_A}', '${D2}', '2026-03-02T15:00:00Z', '2026-03-02T15:30:00Z', 500,  false, NULL),
  ('${ORG_B}', '${D3}', '2026-03-01T12:00:00Z', '2026-03-01T13:00:00Z', 9999, false, NULL),
  ('${ORG_A}', '${D1}', now() - interval '1 day', now() - interval '1 day' + interval '5 minutes', 100, false, 'recent');
`;

const MARCH = { from: "2026-03-01", to: "2026-04-01" } as const;
const MARCH_UTC = { from: "2026-03-01T00:00:00Z", to: "2026-04-01T00:00:00Z" } as const;

let db: PGlite;
let engine: Engine;

beforeAll(async () => {
  db = new PGlite();
  await db.exec(SCHEMA);
  engine = createEngine({
    model: fleet,
    dialect: postgresDialect(),
    executor: pgliteExecutor(db),
    defaultTimezone: "America/Toronto",
  });
});

afterAll(async () => {
  await db.close();
});

const run = (ir: Omit<QueryIR, "version">, auth: Record<string, unknown> = authA) =>
  engine.run({ version: 1, ...ir }, { auth });

describe("postgres dialect against PGlite", () => {
  it("counts within an absolute range, scoped to the caller's organization", async () => {
    const result = await run({ measures: ["trips.count"], time: { field: "trips.started_at", range: MARCH_UTC } });
    expect(result.rows).toEqual([{ "trips.count": 3 }]);
    expect(result.chart.type).toBe("kpi");
  });

  it("reads date-only ranges in the query's time zone", async () => {
    // 2026-03-01T04:30Z is still Feb 28 in Toronto, so local March holds one trip fewer than UTC March.
    const local = await run({ measures: ["trips.count"], time: { field: "trips.started_at", range: MARCH } });
    expect(local.rows).toEqual([{ "trips.count": 2 }]);
    const utc = await run({ measures: ["trips.count"], time: { field: "trips.started_at", range: MARCH, tz: "UTC" } });
    expect(utc.rows).toEqual([{ "trips.count": 3 }]);
  });

  it("scopes to a different organization, or to several", async () => {
    const b = await run({ measures: ["trips.count"], time: { field: "trips.started_at", range: MARCH_UTC } }, { orgId: ORG_B });
    expect(b.rows).toEqual([{ "trips.count": 1 }]);
    const both = await run({ measures: ["trips.count"], time: { field: "trips.started_at", range: MARCH_UTC } }, { orgId: [ORG_A, ORG_B] });
    expect(both.rows).toEqual([{ "trips.count": 4 }]);
  });

  it("buckets by local day in the query's time zone", async () => {
    const toronto = await run({
      measures: ["trips.count"],
      time: { field: "trips.started_at", grain: "day", range: { from: "2026-02-28", to: "2026-03-03" } },
    });
    expect(toronto.rows).toEqual([
      { "trips.started_at__day": "2026-02-28", "trips.count": 1 },
      { "trips.started_at__day": "2026-03-01", "trips.count": 1 },
      { "trips.started_at__day": "2026-03-02", "trips.count": 1 },
    ]);
    const utc = await run({
      measures: ["trips.count"],
      time: { field: "trips.started_at", grain: "day", range: { from: "2026-02-28", to: "2026-03-03" }, tz: "UTC" },
    });
    expect(utc.rows).toEqual([
      { "trips.started_at__day": "2026-03-01", "trips.count": 2 },
      { "trips.started_at__day": "2026-03-02", "trips.count": 1 },
    ]);
  });

  it("joins one and two hops and applies every entity's row scope", async () => {
    const byModel = await run({ measures: ["trips.count"], dimensions: ["devices.model"], time: { field: "trips.started_at", range: MARCH_UTC } });
    expect(byModel.rows).toEqual([
      { "devices.model": "X1", "trips.count": 2 },
      { "devices.model": "X2", "trips.count": 1 },
    ]);
    const byRegion = await run({ measures: ["trips.count"], dimensions: ["organizations.region"], time: { field: "trips.started_at", range: MARCH_UTC } });
    expect(byRegion.rows).toEqual([{ "organizations.region": "east", "trips.count": 3 }]);
  });

  it("escapes LIKE wildcards in contains filters", async () => {
    const percent = await run({ measures: ["trips.count"], filters: [{ field: "trips.note", op: "contains", value: "100%" }] });
    expect(percent.rows).toEqual([{ "trips.count": 1 }]);
    const underscore = await run({ measures: ["trips.count"], filters: [{ field: "trips.note", op: "contains", value: "_" }] });
    expect(underscore.rows).toEqual([{ "trips.count": 1 }]);
    const prefix = await run({ measures: ["trips.count"], filters: [{ field: "trips.note", op: "starts_with", value: "BUMPY" }] });
    expect(prefix.rows).toEqual([{ "trips.count": 1 }]);
  });

  it("keeps empty values when filtering with neq and not_in", async () => {
    const range = { field: "trips.started_at", range: MARCH_UTC } as const;
    const neq = await run({ measures: ["trips.count"], time: range, filters: [{ field: "trips.note", op: "neq", value: "bumpy_ride" }] });
    expect(neq.rows).toEqual([{ "trips.count": 2 }]);
    const notIn = await run({ measures: ["trips.count"], time: range, filters: [{ field: "trips.note", op: "not_in", value: ["bumpy_ride", "100% fine"] }] });
    expect(notIn.rows).toEqual([{ "trips.count": 1 }]);
    const isNull = await run({ measures: ["trips.count"], time: range, filters: [{ field: "trips.note", op: "is_null" }] });
    expect(isNull.rows).toEqual([{ "trips.count": 1 }]);
  });

  it("filters on enums, booleans, numbers and lists", async () => {
    const range = { field: "trips.started_at", range: MARCH_UTC } as const;
    const filtered = (filters: QueryIR["filters"]) => run({ measures: ["trips.count"], time: range, ...(filters ? { filters } : {}) });
    expect((await filtered([{ field: "devices.status", op: "eq", value: "retired" }])).rows).toEqual([{ "trips.count": 1 }]);
    expect((await filtered([{ field: "trips.is_assisted", op: "eq", value: true }])).rows).toEqual([{ "trips.count": 1 }]);
    expect((await filtered([{ field: "trips.distance_m", op: "gte", value: 1000 }])).rows).toEqual([{ "trips.count": 2 }]);
    expect((await filtered([{ field: "devices.model", op: "in", value: ["X2", "X9"] }])).rows).toEqual([{ "trips.count": 1 }]);
    expect((await filtered([{ field: "trips.started_at", op: "gte", value: "2026-03-02" }])).rows).toEqual([{ "trips.count": 1 }]);
  });

  it("computes custom measures and filters on totals", async () => {
    const range = { field: "trips.started_at", range: MARCH_UTC } as const;
    const measures = await run({ measures: ["trips.avg_duration_min", "trips.total_distance_km", "trips.unique_devices"], time: range });
    expect(measures.rows).toEqual([{ "trips.avg_duration_min": 20, "trips.total_distance_km": 3.5, "trips.unique_devices": 2 }]);
    const having = await run({ measures: ["trips.count"], dimensions: ["devices.model"], time: range, filters: [{ field: "trips.count", op: "gt", value: 1 }] });
    expect(having.rows).toEqual([{ "devices.model": "X1", "trips.count": 2 }]);
    const active = await run({ measures: ["devices.active_count", "devices.count"] });
    expect(active.rows).toEqual([{ "devices.active_count": 1, "devices.count": 2 }]);
  });

  it("resolves relative presets against the database clock", async () => {
    const recent = await run({ measures: ["trips.count"], time: { field: "trips.started_at", range: "last_7_days" } });
    expect(recent.rows).toEqual([{ "trips.count": 1 }]);
    const lastYear = await run({ measures: ["trips.count"], time: { field: "trips.started_at", range: "last_year" } });
    expect(lastYear.rows).toEqual([{ "trips.count": 0 }]);
  });

  it("reports truncation when more rows match than the limit", async () => {
    const result = await run({ measures: ["trips.count"], dimensions: ["trips.note"], orderBy: [{ field: "trips.note", dir: "asc" }], limit: 2 });
    expect(result.rows).toHaveLength(2);
    expect(result.truncated).toBe(true);
  });

  it("runs every query read-only", async () => {
    const executor = pgliteExecutor(db);
    await expect(executor.execute({ sql: "DELETE FROM trips", params: [] }, { timeoutMs: 1000, maxRows: 10 })).rejects.toThrow(/read-only/);
    const count = await db.query<{ n: number }>("SELECT count(*)::int AS n FROM trips");
    expect(count.rows[0]!.n).toBe(5);
  });
});

describe("introspectPostgres", () => {
  it("reads tables, keys, enums and comments, and drafts a working model", async () => {
    const graph = await introspectPostgres((sql, params) => db.query<Record<string, unknown>>(sql, params).then((r) => r.rows));
    const names = graph.tables.map((t) => t.name).sort();
    expect(names).toEqual(["devices", "organizations", "patients", "trips"]);

    const trips = graph.tables.find((t) => t.name === "trips")!;
    expect(trips.foreignKeys).toEqual(
      expect.arrayContaining([
        { columns: ["device_id"], referencedTable: "devices", referencedSchema: "public", referencedColumns: ["id"] },
        { columns: ["org_id"], referencedTable: "organizations", referencedSchema: "public", referencedColumns: ["id"] },
      ]),
    );
    expect(trips.columns.find((c) => c.name === "id")).toMatchObject({ kind: "integer", primaryKey: true });
    expect(trips.columns.find((c) => c.name === "started_at")).toMatchObject({ kind: "timestamp", withTimezone: true });
    expect(trips.columns.find((c) => c.name === "distance_m")).toMatchObject({ kind: "number", comment: "Metres travelled" });
    const status = graph.tables.find((t) => t.name === "devices")!.columns.find((c) => c.name === "status");
    expect(status).toMatchObject({ kind: "enum", enumValues: ["active", "inactive", "retired"] });
    expect(graph.tables.find((t) => t.name === "organizations")!.comment).toBe("Customer organizations");

    const model = mergeSemantics(draftSemantics(graph), {
      entities: {
        trips: { rowScope: [{ column: "org_id", authKey: "orgId" }] },
        devices: { rowScope: [{ column: "org_id", authKey: "orgId" }] },
        organizations: { rowScope: [{ column: "id", authKey: "orgId" }] },
      },
    });
    expect(model.entities.patients!.dimensions.email!.policy).toBe("hidden");

    const drafted = createEngine({ model, dialect: postgresDialect(), executor: pgliteExecutor(db) });
    const result = await drafted.run(
      {
        version: 1,
        measures: ["trips.count", "trips.total_distance_m"],
        dimensions: ["organizations.region", "devices.status"],
        time: { field: "trips.started_at", range: MARCH_UTC },
      },
      { auth: { orgId: ORG_A } },
    );
    expect(result.rows).toEqual([
      { "organizations.region": "east", "devices.status": "active", "trips.count": 2, "trips.total_distance_m": 3000 },
      { "organizations.region": "east", "devices.status": "retired", "trips.count": 1, "trips.total_distance_m": 500 },
    ]);
  });
});
