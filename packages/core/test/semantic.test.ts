import { describe, expect, it } from "vitest";
import type { SchemaGraph } from "../src/index.js";
import { buildCatalog, catalogToText, checkSemantics, draftSemantics, mergeSemantics } from "../src/index.js";
import { authA, fleet } from "./fixtures.js";

const graph: SchemaGraph = {
  tables: [
    {
      name: "devices",
      schema: "public",
      columns: [
        { name: "id", kind: "uuid", dbType: "uuid", nullable: false, primaryKey: true },
        { name: "serial_number", kind: "string", dbType: "text", nullable: false },
        { name: "status", kind: "enum", dbType: "device_status", nullable: false, enumValues: ["active", "retired"] },
        { name: "owner_email", kind: "string", dbType: "text", nullable: true },
        { name: "created_at", kind: "timestamp", dbType: "timestamp with time zone", nullable: false, withTimezone: true },
      ],
      foreignKeys: [],
    },
    {
      name: "trips",
      schema: "public",
      columns: [
        { name: "id", kind: "integer", dbType: "bigint", nullable: false, primaryKey: true },
        { name: "device_id", kind: "uuid", dbType: "uuid", nullable: false },
        { name: "distance_m", kind: "number", dbType: "double precision", nullable: true },
        { name: "started_at", kind: "timestamp", dbType: "timestamp without time zone", nullable: false, withTimezone: false },
        { name: "trip_date", kind: "date", dbType: "date", nullable: false },
        { name: "payload", kind: "json", dbType: "jsonb", nullable: true },
      ],
      foreignKeys: [{ columns: ["device_id"], referencedTable: "devices", referencedSchema: "public", referencedColumns: ["id"] }],
    },
  ],
};

describe("draftSemantics", () => {
  const model = draftSemantics(graph);

  it("produces a sound model", () => {
    expect(checkSemantics(model)).toEqual([]);
  });

  it("creates entities, dimensions and measures from columns", () => {
    const trips = model.entities.trips!;
    expect(Object.keys(trips.dimensions).sort()).toEqual(["device_id", "distance_m", "id", "started_at", "trip_date"]);
    expect(trips.dimensions.id!.policy).toBe("aggregate_only");
    expect(trips.dimensions.device_id!.policy).toBe("aggregate_only");
    expect(trips.dimensions.started_at).toMatchObject({ type: "time", timeStorage: "utc" });
    expect(trips.dimensions.trip_date).toMatchObject({ type: "time", timeStorage: "local" });
    expect(Object.keys(trips.measures).sort()).toEqual(["avg_distance_m", "count", "total_distance_m"]);
    expect(trips.defaultTime).toBe("started_at");
  });

  it("hides columns that look sensitive and keeps enum values", () => {
    const devices = model.entities.devices!;
    expect(devices.dimensions.owner_email!.policy).toBe("hidden");
    expect(devices.dimensions.status!.enumValues).toEqual(["active", "retired"]);
  });

  it("turns foreign keys into many-to-one joins", () => {
    expect(model.joins).toEqual([{ from: "trips", to: "devices", on: [["device_id", "id"]], relationship: "many_to_one" }]);
  });
});

describe("mergeSemantics", () => {
  it("applies curated changes on top of a draft", () => {
    const merged = mergeSemantics(draftSemantics(graph), {
      entities: {
        devices: {
          synonyms: ["chairs"],
          rowScope: [{ column: "org_id", authKey: "orgId" }],
          dimensions: { serial_number: { policy: "aggregate_only" }, owner_email: null },
          measures: { active: { sql: "count(*) FILTER (WHERE ${TABLE}.status = 'active')", label: "Active devices" } },
        },
        trips: { measures: { avg_distance_m: null } },
      },
    });
    const devices = merged.entities.devices!;
    expect(devices.synonyms).toEqual(["chairs"]);
    expect(devices.dimensions.serial_number!.policy).toBe("aggregate_only");
    expect(devices.dimensions.serial_number!.column).toBe("serial_number");
    expect(devices.dimensions.owner_email).toBeUndefined();
    expect(devices.measures.active!.label).toBe("Active devices");
    expect(merged.entities.trips!.measures.avg_distance_m).toBeUndefined();
    expect(checkSemantics(merged)).toEqual([]);
  });

  it("drops entities and the joins that pointed at them", () => {
    const merged = mergeSemantics(draftSemantics(graph), { entities: { devices: null } });
    expect(merged.entities.devices).toBeUndefined();
    expect(merged.joins).toEqual([]);
  });
});

describe("buildCatalog", () => {
  it("shows a caller only what they may use", () => {
    const catalog = buildCatalog(fleet, authA);
    const names = catalog.entities.map((e) => e.name).sort();
    expect(names).toEqual(["devices", "organizations", "trips"]);
    const devices = catalog.entities.find((e) => e.name === "devices")!;
    expect(devices.dimensions.map((d) => d.ref)).not.toContain("devices.serial");
    expect(devices.joinsTo).toEqual(["organizations"]);
    const trips = catalog.entities.find((e) => e.name === "trips")!;
    expect(trips.joinsTo.sort()).toEqual(["devices", "organizations"]);

    const text = catalogToText(catalog);
    expect(text).toContain("trips.count");
    expect(text).not.toContain("patients");
    expect(text).not.toContain("serial");
  });
});
