import { draftSemantics } from "@noquery/core";
import {
  boolean,
  doublePrecision,
  integer,
  jsonb,
  pgEnum,
  pgSchema,
  pgTable,
  primaryKey,
  text,
  timestamp,
  uuid,
} from "drizzle-orm/pg-core";
import { describe, expect, it } from "vitest";
import { introspectDrizzle } from "../src/index.js";

const deviceStatus = pgEnum("device_status", ["active", "inactive", "retired"]);
const analytics = pgSchema("analytics");

const organizations = pgTable("organizations", {
  id: uuid("id").primaryKey(),
  name: text("name").notNull(),
});

const devices = pgTable("devices", {
  id: uuid("id").primaryKey(),
  orgId: uuid("org_id").notNull().references(() => organizations.id),
  status: deviceStatus("status").notNull(),
  contactEmail: text("contact_email"),
  installedAt: timestamp("installed_at", { withTimezone: true }).notNull(),
});

const trips = pgTable("trips", {
  id: integer("id").primaryKey(),
  deviceId: uuid("device_id").notNull().references(() => devices.id),
  startedAt: timestamp("started_at").notNull(),
  distanceM: doublePrecision("distance_m"),
  isAssisted: boolean("is_assisted").notNull().default(false),
  payload: jsonb("payload"),
});

const tripTags = pgTable(
  "trip_tags",
  { tripId: integer("trip_id").notNull().references(() => trips.id), tag: text("tag").notNull() },
  (t) => [primaryKey({ columns: [t.tripId, t.tag] })],
);

const dailyRollup = analytics.table("daily_rollup", { day: text("day").primaryKey(), trips: integer("trips") });

const schema = { deviceStatus, organizations, devices, trips, tripTags, dailyRollup, notATable: 42 };

describe("introspectDrizzle", () => {
  const graph = introspectDrizzle(schema);

  it("finds every table, including other schemas, and ignores everything else", () => {
    expect(graph.tables.map((t) => t.name).sort()).toEqual(["daily_rollup", "devices", "organizations", "trip_tags", "trips"]);
    expect(graph.tables.find((t) => t.name === "daily_rollup")!.schema).toBe("analytics");
  });

  it("maps column types, nullability, enums and time zones", () => {
    const devicesInfo = graph.tables.find((t) => t.name === "devices")!;
    const col = (name: string) => devicesInfo.columns.find((c) => c.name === name)!;
    expect(col("id")).toMatchObject({ kind: "uuid", primaryKey: true, nullable: false });
    expect(col("status")).toMatchObject({ kind: "enum", enumValues: ["active", "inactive", "retired"] });
    expect(col("contact_email")).toMatchObject({ kind: "string", nullable: true });
    expect(col("installed_at")).toMatchObject({ kind: "timestamp", withTimezone: true });

    const tripsInfo = graph.tables.find((t) => t.name === "trips")!;
    const tcol = (name: string) => tripsInfo.columns.find((c) => c.name === name)!;
    expect(tcol("started_at")).toMatchObject({ kind: "timestamp", withTimezone: false });
    expect(tcol("distance_m")).toMatchObject({ kind: "number" });
    expect(tcol("is_assisted")).toMatchObject({ kind: "boolean" });
    expect(tcol("payload")).toMatchObject({ kind: "json" });
  });

  it("reads foreign keys and composite primary keys", () => {
    const tripsInfo = graph.tables.find((t) => t.name === "trips")!;
    expect(tripsInfo.foreignKeys).toEqual([{ columns: ["device_id"], referencedTable: "devices", referencedColumns: ["id"] }]);
    const tags = graph.tables.find((t) => t.name === "trip_tags")!;
    expect(tags.columns.filter((c) => c.primaryKey).map((c) => c.name).sort()).toEqual(["tag", "trip_id"]);
  });

  it("honours include and exclude", () => {
    expect(introspectDrizzle(schema, { include: ["trips"] }).tables.map((t) => t.name)).toEqual(["trips"]);
    expect(introspectDrizzle(schema, { exclude: ["trips", "trip_tags"] }).tables).toHaveLength(3);
  });

  it("feeds straight into draftSemantics", () => {
    const model = draftSemantics(graph);
    expect(model.joins).toEqual(
      expect.arrayContaining([
        { from: "devices", to: "organizations", on: [["org_id", "id"]], relationship: "many_to_one" },
        { from: "trips", to: "devices", on: [["device_id", "id"]], relationship: "many_to_one" },
      ]),
    );
    expect(model.entities.devices!.dimensions.contact_email!.policy).toBe("hidden");
    expect(model.entities.trips!.dimensions.started_at!.timeStorage).toBe("utc");
  });
});
