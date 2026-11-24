import { resolveQuery } from "@noquery/core";
import { describe, expect, it } from "vitest";
import { authA, fleet, ORG_A } from "../../core/test/fixtures.js";
import { postgresDialect, quoteIdent } from "../src/index.js";

describe("postgresDialect", () => {
  it("compiles a grouped, joined, time-bucketed query with bound parameters only", () => {
    const resolved = resolveQuery(
      fleet,
      {
        version: 1,
        measures: ["trips.count"],
        dimensions: ["devices.model"],
        time: { field: "trips.started_at", grain: "week", range: "last_30_days", tz: "America/Toronto" },
        filters: [{ field: "trips.note", op: "contains", value: "o'brien" }],
        limit: 50,
      },
      authA,
    );
    if (!resolved.ok) throw new Error(JSON.stringify(resolved.errors));
    const { sql, params } = postgresDialect().compile(resolved.query);

    expect(sql).toBe(
      [
        `SELECT to_char(date_trunc('week', ("t0"."started_at" AT TIME ZONE $1::text)), 'YYYY-MM-DD') AS "trips.started_at__week", "t1"."model" AS "devices.model", count(*) AS "trips.count"`,
        `FROM "trips" AS "t0"`,
        `LEFT JOIN "devices" AS "t1" ON "t0"."device_id" = "t1"."id"`,
        `WHERE "t0"."org_id" = $2 AND "t1"."org_id" = $3 AND "t0"."started_at" >= ((date_trunc('day', (now() AT TIME ZONE $1::text)) - interval '29 days') AT TIME ZONE $1::text) AND "t0"."started_at" < ((date_trunc('day', (now() AT TIME ZONE $1::text)) + interval '1 day') AT TIME ZONE $1::text) AND "t0"."note" ILIKE $4`,
        `GROUP BY 1, 2`,
        `ORDER BY "trips.started_at__week" ASC NULLS LAST`,
        `LIMIT 51`,
      ].join("\n"),
    );
    expect(params).toEqual(["America/Toronto", ORG_A, ORG_A, "%o'brien%"]);
  });

  it("quotes identifiers safely", () => {
    expect(quoteIdent('we"ird')).toBe('"we""ird"');
  });
});
