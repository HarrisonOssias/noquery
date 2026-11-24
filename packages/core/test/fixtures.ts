import { defineSemantics } from "../src/index.js";

/** A small fleet-telemetry model used across the test suites. */
export const fleet = defineSemantics({
  entities: {
    organizations: {
      table: "organizations",
      label: "Organization",
      dimensions: {
        name: { type: "string", column: "name" },
        region: { type: "string", column: "region", enumValues: ["east", "west"] },
      },
      measures: { count: { agg: "count", label: "Organizations" } },
      rowScope: [{ column: "id", authKey: "orgId" }],
    },
    devices: {
      table: "devices",
      label: "Device",
      synonyms: ["units", "chairs"],
      defaultTime: "installed_at",
      dimensions: {
        id: { type: "string", column: "id", policy: "aggregate_only" },
        serial: { type: "string", column: "serial", policy: "aggregate_only" },
        status: { type: "string", column: "status", enumValues: ["active", "inactive", "retired"] },
        model: { type: "string", column: "model" },
        installed_at: { type: "time", column: "installed_at" },
      },
      measures: {
        count: { agg: "count", label: "Devices" },
        active_count: { sql: "count(*) FILTER (WHERE ${TABLE}.status = 'active')", label: "Active devices" },
      },
      rowScope: [{ column: "org_id", authKey: "orgId" }],
    },
    trips: {
      table: "trips",
      label: "Trip",
      defaultTime: "started_at",
      dimensions: {
        started_at: { type: "time", column: "started_at" },
        distance_m: { type: "number", column: "distance_m", label: "Distance (m)" },
        is_assisted: { type: "boolean", column: "is_assisted", label: "Assisted" },
        note: { type: "string", column: "note" },
      },
      measures: {
        count: { agg: "count", label: "Trips" },
        avg_duration_min: {
          agg: "avg",
          sql: "extract(epoch from ${TABLE}.ended_at - ${TABLE}.started_at) / 60",
          label: "Average duration (min)",
        },
        total_distance_km: { agg: "sum", sql: "${TABLE}.distance_m / 1000.0", label: "Total distance (km)" },
        unique_devices: { agg: "count_distinct", column: "device_id", label: "Devices with trips" },
      },
      rowScope: [{ column: "org_id", authKey: "orgId" }],
    },
    patients: {
      table: "patients",
      roles: ["clinical"],
      dimensions: {
        email: { type: "string", column: "email", policy: "hidden" },
        ward: { type: "string", column: "ward" },
      },
      measures: { count: { agg: "count", label: "Patients" } },
    },
  },
  joins: [
    { from: "trips", to: "devices", on: [["device_id", "id"]], relationship: "many_to_one" },
    { from: "devices", to: "organizations", on: [["org_id", "id"]], relationship: "many_to_one" },
  ],
});

export const ORG_A = "00000000-0000-0000-0000-00000000000a";
export const ORG_B = "00000000-0000-0000-0000-00000000000b";

export const authA = { userId: "u1", orgId: ORG_A, roles: ["analyst"] };
