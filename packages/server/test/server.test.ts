import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import type { Executor, LLMProvider } from "@noquery/core";
import { createEngine, createPlanner, PlannerError } from "@noquery/core";
import { postgresDialect } from "@noquery/postgres";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fleet, ORG_A } from "../../core/test/fixtures.js";
import { createNoqueryServer, serviceTokenAuth } from "../src/index.js";

const TOKEN = "test-token-0123456789";

const plannerAnswer = {
  kind: "query",
  query: { measures: ["trips.count"], dimensions: [], time: null, filters: [], order_by: [], limit: null },
  clarification: null,
  reason: null,
};

let server: Server | undefined;

async function start(options: { planner?: boolean; executor?: Executor; onError?: (e: unknown) => void } = {}) {
  const executor: Executor = options.executor ?? { execute: vi.fn(async () => ({ rows: [{ "trips.count": "5" }], truncated: false })) };
  const provider: LLMProvider = { name: "scripted", generateJson: async () => plannerAnswer };
  const engine = createEngine({
    model: fleet,
    dialect: postgresDialect(),
    executor,
    ...(options.planner ? { planner: createPlanner(provider) } : {}),
  });
  server = createNoqueryServer({ engine, authenticate: serviceTokenAuth(TOKEN), ...(options.onError ? { onError: options.onError } : {}) });
  await new Promise<void>((resolve) => server!.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  const base = `http://127.0.0.1:${port}`;
  const post = (path: string, body: unknown, token: string | null = TOKEN) =>
    fetch(base + path, {
      method: "POST",
      headers: { "content-type": "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify(body),
    });
  return { base, post, executor };
}

afterEach(async () => {
  await new Promise<void>((resolve) => (server ? server.close(() => resolve()) : resolve()));
  server = undefined;
});

const auth = { userId: "u1", orgId: ORG_A, roles: ["analyst"] };
const json = async (res: Response): Promise<any> => res.json();
const ir = { version: 1, measures: ["trips.count"] };

describe("noquery server", () => {
  it("reports health without auth", async () => {
    const { base } = await start({ planner: true });
    const res = await fetch(`${base}/healthz`);
    expect(await json(res)).toEqual({ ok: true, planner: true });
  });

  it("rejects missing or wrong service tokens", async () => {
    const { post } = await start();
    expect((await post("/v1/run", { auth, ir }, null)).status).toBe(401);
    const res = await post("/v1/run", { auth, ir }, "wrong-token-0123456789");
    expect(res.status).toBe(401);
    expect(await json(res)).toEqual({ error: { code: "unauthorized", message: "Missing or invalid service token." } });
  });

  it("requires a well-formed auth object", async () => {
    const { post } = await start();
    expect((await post("/v1/run", { ir })).status).toBe(400);
    expect((await post("/v1/run", { auth: { orgId: { $ne: null } }, ir })).status).toBe(400);
    expect((await post("/v1/run", { auth: { roles: "admin" }, ir })).status).toBe(400);
  });

  it("runs a query with the caller's row scope bound as a parameter", async () => {
    const { post, executor } = await start();
    const res = await post("/v1/run", { auth, ir });
    expect(res.status).toBe(200);
    const body = await json(res);
    expect(body.rows).toEqual([{ "trips.count": 5 }]);
    expect(body.explanation).toBe("Trips.");
    const compiled = (executor.execute as ReturnType<typeof vi.fn>).mock.calls[0]![0];
    expect(compiled.params).toEqual([ORG_A]);
  });

  it("returns 422 with reasons for invalid queries, and validates without running", async () => {
    const { post, executor } = await start();
    const res = await post("/v1/run", { auth, ir: { version: 1, measures: ["patients.count"] } });
    expect(res.status).toBe(422);
    const body = await json(res);
    expect(body.error.code).toBe("invalid_query");
    expect(body.error.errors[0].code).toBe("unknown_entity");

    const check = await post("/v1/validate", { auth, ir: { version: 1, measures: ["trips.nope"] } });
    expect(check.status).toBe(200);
    expect((await json(check)).ok).toBe(false);
    expect(executor.execute).not.toHaveBeenCalled();
  });

  it("serves the caller's catalog", async () => {
    const { post } = await start();
    const body = await json(await post("/v1/catalog", { auth }));
    expect(body.entities.map((e: { name: string }) => e.name).sort()).toEqual(["devices", "organizations", "trips"]);
  });

  it("answers questions when a planner is configured, and says so when not", async () => {
    const withPlanner = await start({ planner: true });
    const res = await withPlanner.post("/v1/ask", { auth, question: "How many trips?", timezone: "America/Toronto" });
    expect(res.status).toBe(200);
    expect(await json(res)).toMatchObject({ type: "answer", rows: [{ "trips.count": 5 }] });
    expect((await withPlanner.post("/v1/ask", { auth, question: "" })).status).toBe(400);
    await new Promise<void>((resolve) => server!.close(() => resolve()));

    const without = await start();
    const off = await without.post("/v1/ask", { auth, question: "How many trips?" });
    expect(off.status).toBe(501);
    expect((await json(off)).error.code).toBe("planner_not_configured");
  });

  it("rejects bad requests clearly", async () => {
    const { base, post } = await start();
    expect((await post("/v1/nope", { auth })).status).toBe(404);
    expect((await fetch(`${base}/v1/run`)).status).toBe(405);
    const text = await fetch(`${base}/v1/run`, { method: "POST", headers: { authorization: `Bearer ${TOKEN}` }, body: "x" });
    expect(text.status).toBe(415);
    const broken = await fetch(`${base}/v1/run`, {
      method: "POST",
      headers: { authorization: `Bearer ${TOKEN}`, "content-type": "application/json" },
      body: "{not json",
    });
    expect(broken.status).toBe(400);
  });

  it("hides internal error details from callers", async () => {
    const onError = vi.fn();
    const { post } = await start({
      executor: { execute: async () => Promise.reject(new Error("password authentication failed for user admin")) },
      onError,
    });
    const res = await post("/v1/run", { auth, ir });
    expect(res.status).toBe(500);
    expect(await json(res)).toEqual({ error: { code: "internal", message: "Internal error." } });
    expect(onError).toHaveBeenCalled();
  });

  it("reports planner failures with a safe reason instead of a generic 500", async () => {
    const failing: LLMProvider = {
      name: "failing",
      generateJson: async () => {
        throw new PlannerError("authentication", "Claude rejected the API credentials (401). Check ANTHROPIC_API_KEY on the noquery server.");
      },
    };
    const engine = createEngine({ model: fleet, dialect: postgresDialect(), executor: { execute: async () => ({ rows: [], truncated: false }) }, planner: createPlanner(failing) });
    server = createNoqueryServer({ engine, authenticate: serviceTokenAuth(TOKEN) });
    await new Promise<void>((resolve) => server!.listen(0, "127.0.0.1", resolve));
    const { port } = server.address() as AddressInfo;
    const res = await fetch(`http://127.0.0.1:${port}/v1/ask`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${TOKEN}` },
      body: JSON.stringify({ auth, question: "How many trips?" }),
    });
    expect(res.status).toBe(502);
    expect(await json(res)).toEqual({
      error: {
        code: "planner_failed",
        message: "Claude rejected the API credentials (401). Check ANTHROPIC_API_KEY on the noquery server.",
        reason: "authentication",
        retryable: false,
      },
    });
  });

  it("refuses short service tokens", () => {
    expect(() => serviceTokenAuth("short")).toThrow(/at least 16/);
  });
});
