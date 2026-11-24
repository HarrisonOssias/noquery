import { describe, expect, it } from "vitest";
import type { QueryErrorCode, QueryIR, ResolveResult } from "../src/index.js";
import { checkSemantics, resolveQuery } from "../src/index.js";
import { authA, fleet, ORG_A } from "./fixtures.js";

const resolve = (ir: Omit<QueryIR, "version">, auth: Record<string, unknown> = authA) =>
  resolveQuery(fleet, { version: 1, ...ir }, auth);

function codes(result: ResolveResult): QueryErrorCode[] {
  return result.ok ? [] : result.errors.map((e) => e.code);
}

describe("resolveQuery", () => {
  it("accepts the fixture model", () => {
    expect(checkSemantics(fleet)).toEqual([]);
  });

  it("plans joins through many-to-one edges and scopes every joined entity", () => {
    const result = resolve({
      measures: ["trips.count"],
      dimensions: ["organizations.region"],
      time: { field: "trips.started_at", grain: "day", range: "last_7_days", tz: "America/Toronto" },
    });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const q = result.query;
    expect(q.base).toMatchObject({ entity: "trips", alias: "t0" });
    expect(q.joins.map((j) => [j.entity, j.alias, j.parentAlias])).toEqual([
      ["devices", "t1", "t0"],
      ["organizations", "t2", "t1"],
    ]);
    const scopes = q.where.filter((c) => c.kind === "scope");
    expect(scopes).toEqual([
      { kind: "scope", alias: "t0", column: "org_id", value: ORG_A },
      { kind: "scope", alias: "t1", column: "org_id", value: ORG_A },
      { kind: "scope", alias: "t2", column: "id", value: ORG_A },
    ]);
    expect(q.columns.map((c) => c.key)).toEqual([
      "trips.started_at__day",
      "organizations.region",
      "trips.count",
    ]);
    expect(q.orderBy).toEqual([{ key: "trips.started_at__day", dir: "asc" }]);
    expect(q.timezone).toBe("America/Toronto");
  });

  it("rejects fields that do not exist", () => {
    expect(codes(resolve({ measures: ["trips.nope"] }))).toEqual(["unknown_field"]);
    expect(codes(resolve({ measures: ["nope.count"] }))).toEqual(["unknown_entity"]);
    expect(codes(resolve({ measures: ["trips; drop table x"] }))).toEqual(["unknown_field"]);
  });

  it("treats hidden fields and role-gated entities as unknown", () => {
    expect(codes(resolve({ measures: ["patients.count"] }))).toEqual(["unknown_entity"]);
    const clinical = { ...authA, roles: ["clinical"] };
    expect(resolve({ measures: ["patients.count"], dimensions: ["patients.ward"] }, clinical).ok).toBe(true);
    expect(codes(resolve({ measures: ["patients.count"], dimensions: ["patients.email"] }, clinical))).toEqual([
      "unknown_field",
    ]);
  });

  it("never lets aggregate-only fields be grouped, filtered or listed", () => {
    expect(codes(resolve({ measures: ["devices.count"], dimensions: ["devices.serial"] }))).toEqual(["aggregate_only"]);
    expect(
      codes(resolve({ measures: ["devices.count"], filters: [{ field: "devices.serial", op: "eq", value: "X1" }] })),
    ).toEqual(["aggregate_only"]);
  });

  it("refuses joins that would inflate totals", () => {
    const result = resolve({ measures: ["devices.count"], dimensions: ["trips.is_assisted"] });
    expect(codes(result)).toEqual(["no_join_path"]);
    expect(result.ok ? "" : result.errors[0]!.message).toMatch(/inflate totals/);
  });

  it("requires measures from a single entity", () => {
    expect(codes(resolve({ measures: ["trips.count", "devices.count"] }))).toContain("mixed_measure_entities");
  });

  it("fails closed when the auth context lacks a row-scope value", () => {
    expect(codes(resolve({ measures: ["trips.count"] }, { userId: "u1" }))).toEqual(["missing_auth_attribute"]);
    expect(codes(resolve({ measures: ["trips.count"] }, { orgId: { $ne: null } }))).toEqual(["missing_auth_attribute"]);
  });

  it("accepts a list of scope values", () => {
    const result = resolve({ measures: ["trips.count"] }, { orgId: [ORG_A, "other"] });
    expect(result.ok && result.query.where[0]).toEqual({ kind: "scope", alias: "t0", column: "org_id", value: [ORG_A, "other"] });
  });

  it("checks filter values against the field type and allowed values", () => {
    expect(codes(resolve({ measures: ["devices.count"], filters: [{ field: "devices.status", op: "eq", value: "broken" }] }))).toEqual(["invalid_filter"]);
    expect(codes(resolve({ measures: ["trips.count"], filters: [{ field: "trips.distance_m", op: "gt", value: "far" }] }))).toEqual(["invalid_filter"]);
    expect(codes(resolve({ measures: ["trips.count"], filters: [{ field: "trips.is_assisted", op: "gt", value: true }] }))).toEqual(["invalid_filter"]);
    expect(codes(resolve({ measures: ["trips.count"], filters: [{ field: "trips.note", op: "in", value: [] }] }))).toEqual(["invalid_filter"]);
    expect(resolve({ measures: ["trips.count"], filters: [{ field: "devices.status", op: "in", value: ["active", "retired"] }] }).ok).toBe(true);
  });

  it("puts measure filters in HAVING", () => {
    const result = resolve({
      measures: ["trips.count"],
      dimensions: ["devices.model"],
      filters: [{ field: "trips.count", op: "gt", value: 10 }],
    });
    expect(result.ok && result.query.having).toHaveLength(1);
    expect(codes(resolve({ measures: ["trips.count"], filters: [{ field: "trips.count", op: "contains", value: "1" }] }))).toEqual(["invalid_filter"]);
  });

  it("validates time ranges, grains and time zones", () => {
    const t = (time: QueryIR["time"]) => codes(resolve({ measures: ["trips.count"], ...(time ? { time } : {}) }));
    expect(t({ field: "trips.started_at", range: "last_7_days" })).toEqual([]);
    expect(t({ field: "trips.started_at", range: { from: "2026-03-01", to: "2026-04-01" } })).toEqual([]);
    expect(t({ field: "trips.started_at", range: { from: "2026-04-01", to: "2026-03-01" } })).toEqual(["invalid_time"]);
    expect(t({ field: "trips.started_at", range: { from: "March", to: "April" } })).toEqual(["invalid_time"]);
    expect(t({ field: "trips.started_at", range: "last_decade" as never })).toEqual(["invalid_time"]);
    expect(t({ field: "trips.distance_m", grain: "day" })).toEqual(["invalid_time"]);
    expect(t({ field: "trips.started_at", grain: "day", tz: "Mars/Olympus" })).toEqual(["invalid_timezone"]);
  });

  it("enforces the limit cap", () => {
    expect(codes(resolve({ measures: ["trips.count"], limit: 0 }))).toEqual(["invalid_limit"]);
    expect(codes(resolve({ measures: ["trips.count"], limit: 1_000_000 }))).toEqual(["invalid_limit"]);
    expect(codes(resolve({ measures: ["trips.count"], limit: 2.5 }))).toEqual(["invalid_limit"]);
  });

  it("only sorts by selected fields and defaults to the first measure descending", () => {
    expect(codes(resolve({ measures: ["trips.count"], orderBy: [{ field: "trips.note", dir: "asc" }] }))).toEqual(["invalid_order_by"]);
    const result = resolve({ measures: ["trips.count"], dimensions: ["devices.model"] });
    expect(result.ok && result.query.orderBy).toEqual([{ key: "trips.count", dir: "desc" }]);
  });

  it("rejects malformed input", () => {
    expect(codes(resolveQuery(fleet, { version: 2 } as never, authA))).toEqual(["invalid_ir"]);
    expect(codes(resolve({}))).toEqual(["empty_query"]);
    expect(codes(resolve({ measures: ["trips.started_at"] }))).toEqual(["wrong_field_kind"]);
  });
});
