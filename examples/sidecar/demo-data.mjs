// An in-process Postgres (PGlite) with a small fleet-telemetry dataset, so the example
// runs without a database. Trips are dated relative to now(), so "last week" always has data.
import { PGlite } from "@electric-sql/pglite";

const SCHEMA = `
CREATE TYPE device_status AS ENUM ('active', 'inactive', 'retired');

CREATE TABLE organizations (id text PRIMARY KEY, name text NOT NULL, region text NOT NULL);
COMMENT ON TABLE organizations IS 'Customer organizations';

CREATE TABLE devices (
  id text PRIMARY KEY,
  org_id text NOT NULL REFERENCES organizations(id),
  serial text NOT NULL,
  status device_status NOT NULL,
  model text NOT NULL,
  installed_at timestamptz NOT NULL
);

CREATE TABLE trips (
  id serial PRIMARY KEY,
  org_id text NOT NULL REFERENCES organizations(id),
  device_id text NOT NULL REFERENCES devices(id),
  started_at timestamptz NOT NULL,
  ended_at timestamptz NOT NULL,
  distance_m double precision NOT NULL,
  is_assisted boolean NOT NULL
);
COMMENT ON COLUMN trips.distance_m IS 'Metres travelled';

INSERT INTO organizations VALUES ('acme', 'Acme Care', 'east'), ('bayside', 'Bayside Health', 'west');

INSERT INTO devices VALUES
  ('d1', 'acme',    'SN-1001', 'active',   'X1', now() - interval '400 days'),
  ('d2', 'acme',    'SN-1002', 'active',   'X2', now() - interval '300 days'),
  ('d3', 'acme',    'SN-1003', 'inactive', 'X1', now() - interval '200 days'),
  ('d4', 'acme',    'SN-1004', 'retired',  'X3', now() - interval '500 days'),
  ('d5', 'bayside', 'SN-2001', 'active',   'X1', now() - interval '100 days'),
  ('d6', 'bayside', 'SN-2002', 'active',   'X2', now() - interval '90 days');

INSERT INTO trips (org_id, device_id, started_at, ended_at, distance_m, is_assisted)
SELECT d.org_id,
       d.id,
       date_trunc('hour', now()) - g * interval '3 hours 17 minutes',
       date_trunc('hour', now()) - g * interval '3 hours 17 minutes' + ((g * 13) % 40 + 5) * interval '1 minute',
       (g * 37) % 2000 + 100,
       g % 5 = 0
FROM generate_series(1, 480) AS g
JOIN devices d ON d.id = 'd' || ((g % 6) + 1);
`;

export async function createDemoDatabase() {
  const db = new PGlite();
  await db.exec(SCHEMA);
  return db;
}

/** Curated changes on top of the auto-drafted model for the demo tables. */
export const demoOverlay = {
  entities: {
    organizations: {
      rowScope: [{ column: "id", authKey: "orgId" }],
    },
    devices: {
      synonyms: ["units", "chairs"],
      rowScope: [{ column: "org_id", authKey: "orgId" }],
      dimensions: { serial: { policy: "aggregate_only" } },
      measures: {
        active_count: { sql: "count(*) FILTER (WHERE ${TABLE}.status = 'active')", label: "Active devices" },
      },
    },
    trips: {
      description: "One continuous movement of a device.",
      synonyms: ["journeys", "rides"],
      rowScope: [{ column: "org_id", authKey: "orgId" }],
      measures: {
        avg_duration_min: {
          agg: "avg",
          sql: "extract(epoch from ${TABLE}.ended_at - ${TABLE}.started_at) / 60",
          label: "Average duration (min)",
        },
        total_distance_km: { agg: "sum", sql: "${TABLE}.distance_m::numeric / 1000", label: "Total distance (km)" },
      },
    },
  },
};
