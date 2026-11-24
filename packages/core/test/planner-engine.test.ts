import { describe, expect, it, vi } from "vitest";
import type { Dialect, Executor, LLMProvider, LLMRequest } from "../src/index.js";
import {
  buildCatalog,
  createEngine,
  createPlanner,
  explainQuery,
  plannerSchema,
  QueryValidationError,
  resolveQuery,
  suggestChart,
  toPlannerResult,
} from "../src/index.js";
import { authA, fleet } from "./fixtures.js";

const catalog = buildCatalog(fleet, authA);

const fakeDialect: Dialect = { name: "fake", compile: (q) => ({ sql: `-- ${q.base.entity}`, params: [] }) };

function fakeExecutor(rows: Array<Record<string, unknown>>): Executor {
  return { execute: vi.fn(async () => ({ rows, truncated: false })) };
}

function rawQuery(query: Record<string, unknown>) {
  return {
    kind: "query",
    query: { measures: [], dimensions: [], time: null, filters: [], order_by: [], limit: null, ...query },
    clarification: null,
    reason: null,
  };
}

function scriptedProvider(...answers: unknown[]): LLMProvider & { requests: LLMRequest[] } {
  const requests: LLMRequest[] = [];
  return {
    name: "scripted",
    requests,
    async generateJson(request) {
      requests.push(request);
      const next = answers.shift();
      if (next === undefined) throw new Error("No more scripted answers");
      return next;
    },
  };
}

describe("plannerSchema", () => {
  it("lists only visible fields as enums and marks every object closed", () => {
    const schema = plannerSchema(catalog) as any;
    const query = schema.properties.query.anyOf[0];
    expect(query.properties.measures.items.enum).toContain("trips.count");
    expect(query.properties.measures.items.enum).not.toContain("patients.count");
    expect(query.properties.dimensions.items.enum).not.toContain("devices.serial");
    expect([...query.properties.time.anyOf[0].properties.field.enum].sort()).toEqual(["devices.installed_at", "trips.started_at"]);
    const objects: any[] = [];
    const walk = (node: any) => {
      if (!node || typeof node !== "object") return;
      if (node.type === "object") objects.push(node);
      Object.values(node).forEach(walk);
    };
    walk(schema);
    expect(objects.length).toBeGreaterThan(3);
    for (const o of objects) {
      expect(o.additionalProperties).toBe(false);
      expect(o.required.sort()).toEqual(Object.keys(o.properties).sort());
    }
  });
});

describe("toPlannerResult", () => {
  it("coerces filter values by field type", () => {
    const result = toPlannerResult(
      rawQuery({
        measures: ["trips.count"],
        filters: [
          { field: "trips.distance_m", op: "gt", values: ["500"] },
          { field: "trips.is_assisted", op: "eq", values: ["true"] },
          { field: "devices.status", op: "in", values: ["active", "retired"] },
          { field: "trips.note", op: "is_null", values: [] },
          { field: "trips.count", op: "gte", values: ["3"] },
        ],
        time: { field: "trips.started_at", grain: "week", range_preset: null, range_from: "2026-03-01", range_to: "2026-04-01" },
        order_by: [{ field: "trips.count", dir: "desc" }],
        limit: 5,
      }) as never,
      catalog,
    );
    expect(result).toEqual({
      kind: "query",
      ir: {
        version: 1,
        measures: ["trips.count"],
        time: { field: "trips.started_at", grain: "week", range: { from: "2026-03-01", to: "2026-04-01" } },
        filters: [
          { field: "trips.distance_m", op: "gt", value: 500 },
          { field: "trips.is_assisted", op: "eq", value: true },
          { field: "devices.status", op: "in", value: ["active", "retired"] },
          { field: "trips.note", op: "is_null" },
          { field: "trips.count", op: "gte", value: 3 },
        ],
        orderBy: [{ field: "trips.count", dir: "desc" }],
        limit: 5,
      },
    });
  });

  it("passes clarifications and refusals through", () => {
    expect(
      toPlannerResult({ kind: "clarify", query: null, clarification: { question: "Which?", options: ["a", "b"] }, reason: null }, catalog),
    ).toEqual({ kind: "clarify", question: "Which?", options: ["a", "b"] });
    expect(toPlannerResult({ kind: "unanswerable", query: null, clarification: null, reason: "No battery data." }, catalog)).toEqual({
      kind: "unanswerable",
      reason: "No battery data.",
    });
  });
});

describe("createEngine", () => {
  it("runs a query, normalizes numeric strings and explains it", async () => {
    const engine = createEngine({
      model: fleet,
      dialect: fakeDialect,
      executor: fakeExecutor([{ "devices.model": "X1", "trips.count": "12" }]),
    });
    const result = await engine.run(
      { version: 1, measures: ["trips.count"], dimensions: ["devices.model"], time: { field: "trips.started_at", range: "last_week" } },
      { auth: authA },
    );
    expect(result.rows).toEqual([{ "devices.model": "X1", "trips.count": 12 }]);
    expect(result.chart).toEqual({ type: "bar", x: "devices.model", y: ["trips.count"] });
    expect(result.explanation).toBe("Trips by device model last week (UTC).");
  });

  it("throws a QueryValidationError and audits invalid queries", async () => {
    const onAudit = vi.fn();
    const engine = createEngine({ model: fleet, dialect: fakeDialect, executor: fakeExecutor([]), onAudit });
    await expect(engine.run({ version: 1, measures: ["patients.count"] }, { auth: authA })).rejects.toBeInstanceOf(QueryValidationError);
    expect(onAudit).toHaveBeenCalledWith(expect.objectContaining({ type: "run", outcome: "invalid", userId: "u1" }));
  });

  it("refuses a broken semantic model at startup", () => {
    expect(() =>
      createEngine({
        model: { entities: { x: { table: "x", dimensions: {}, measures: { bad: { agg: "sum" } } } } },
        dialect: fakeDialect,
        executor: fakeExecutor([]),
      }),
    ).toThrow(/needs a column or sql/);
  });

  it("asks, lets the planner repair a rejected query, then answers", async () => {
    const provider = scriptedProvider(
      rawQuery({ measures: ["devices.count"], dimensions: ["trips.is_assisted"] }),
      rawQuery({ measures: ["trips.unique_devices"], dimensions: ["trips.is_assisted"] }),
    );
    const executor = fakeExecutor([{ "trips.is_assisted": true, "trips.unique_devices": 4 }]);
    const engine = createEngine({
      model: fleet,
      dialect: fakeDialect,
      executor,
      planner: createPlanner(provider),
      now: () => new Date("2026-11-23T15:00:00Z"),
    });

    const result = await engine.ask("How many devices did assisted trips?", { auth: authA, timezone: "America/Toronto" });

    expect(result.type).toBe("answer");
    expect(provider.requests).toHaveLength(2);
    const first = provider.requests[0]!;
    expect(first.system).toContain("trips.unique_devices");
    expect(first.system).not.toContain("patients");
    expect(first.messages.at(-1)!.content).toContain("Today is Monday, 2026-11-23 in America/Toronto.");
    const retry = provider.requests[1]!.messages.at(-1)!.content;
    expect(retry).toContain("inflate totals");
  });

  it("returns the validation errors when repairs run out", async () => {
    const bad = rawQuery({ measures: ["devices.count"], dimensions: ["trips.is_assisted"] });
    const engine = createEngine({
      model: fleet,
      dialect: fakeDialect,
      executor: fakeExecutor([]),
      planner: createPlanner(scriptedProvider(bad, bad)),
    });
    const result = await engine.ask("devices by assisted", { auth: authA });
    expect(result.type).toBe("invalid");
  });

  it("stamps the asker's time zone onto planned queries", async () => {
    const provider = scriptedProvider(
      rawQuery({ measures: ["trips.count"], time: { field: "trips.started_at", grain: "day", range_preset: "last_7_days", range_from: null, range_to: null } }),
    );
    const engine = createEngine({ model: fleet, dialect: fakeDialect, executor: fakeExecutor([]), planner: createPlanner(provider) });
    const result = await engine.ask("trips per day this week", { auth: authA, timezone: "America/Vancouver" });
    expect(result.type === "answer" && result.ir.time?.tz).toBe("America/Vancouver");
  });
});

describe("explainQuery and suggestChart", () => {
  it("describes filters, ranges and sorting in plain words", () => {
    const resolved = resolveQuery(
      fleet,
      {
        version: 1,
        measures: ["trips.count"],
        dimensions: ["organizations.region"],
        time: { field: "trips.started_at", grain: "week", range: { from: "2026-01-01", to: "2026-02-01" }, tz: "America/Toronto" },
        filters: [{ field: "devices.status", op: "in", value: ["active", "retired"] }],
        orderBy: [{ field: "trips.count", dir: "desc" }],
        limit: 10,
      },
      authA,
    );
    if (!resolved.ok) throw new Error("expected ok");
    expect(explainQuery(resolved.query)).toBe(
      "Trips by organization region per week from 2026-01-01 to 2026-02-01 (America/Toronto) where device status is one of active, retired. Sorted by trips, highest first. Showing up to 10 rows.",
    );
  });

  it("picks charts from the result shape", () => {
    const col = (key: string, role: "time" | "dimension" | "measure") => ({ key, ref: key, role, type: "number" as const, label: key });
    expect(suggestChart([col("m", "measure")]).type).toBe("kpi");
    expect(suggestChart([col("t", "time"), col("m", "measure")]).type).toBe("line");
    expect(suggestChart([col("t", "time"), col("d", "dimension"), col("m", "measure")], [{ d: "a" }, { d: "b" }])).toEqual({
      type: "line",
      x: "t",
      y: ["m"],
      series: "d",
    });
    expect(suggestChart([col("d", "dimension")]).type).toBe("table");
  });
});
