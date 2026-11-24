import type Anthropic from "@anthropic-ai/sdk";
import { buildCatalog, createEngine, createPlanner, plannerSchema } from "@noquery/core";
import { describe, expect, it, vi } from "vitest";
import { authA, fleet } from "../../core/test/fixtures.js";
import { anthropicProvider, PlannerRefusalError } from "../src/index.js";

function fakeClient(response: Record<string, unknown>) {
  const create = vi.fn(async (_params: Record<string, unknown>) => response);
  const client = { beta: { messages: { create } } } as unknown as Anthropic;
  return { client, create };
}

const answer = {
  kind: "query",
  query: { measures: ["trips.count"], dimensions: [], time: null, filters: [], order_by: [], limit: null },
  clarification: null,
  reason: null,
};

describe("anthropicProvider", () => {
  it("sends a cached system prompt, a JSON schema output format and server-side fallbacks", async () => {
    const { client, create } = fakeClient({ stop_reason: "end_turn", content: [{ type: "text", text: JSON.stringify(answer) }] });
    const provider = anthropicProvider({ client, effort: "low" });
    const schema = plannerSchema(buildCatalog(fleet, authA));

    const result = await provider.generateJson({ system: "SYSTEM", messages: [{ role: "user", content: "Q" }], schema });

    expect(result).toEqual(answer);
    const params = create.mock.calls[0]![0];
    expect(params).toMatchObject({
      model: "claude-opus-5-5",
      max_tokens: 16000,
      system: [{ type: "text", text: "SYSTEM", cache_control: { type: "ephemeral" } }],
      messages: [{ role: "user", content: "Q" }],
      output_config: { format: { type: "json_schema", schema }, effort: "low" },
      betas: ["server-side-fallback-2026-07-01"],
      fallbacks: "default",
    });
  });

  it("can turn fallbacks off for platforms that lack them", async () => {
    const { client, create } = fakeClient({ stop_reason: "end_turn", content: [{ type: "text", text: "{}" }] });
    await anthropicProvider({ client, fallbacks: false }).generateJson({ system: "", messages: [], schema: {} });
    const params = create.mock.calls[0]![0];
    expect(params).not.toHaveProperty("fallbacks");
    expect(params).not.toHaveProperty("betas");
  });

  it("raises a typed error on refusal and on truncated output", async () => {
    const refused = fakeClient({ stop_reason: "refusal", stop_details: { category: "cyber" }, content: [] });
    await expect(anthropicProvider({ client: refused.client }).generateJson({ system: "", messages: [], schema: {} })).rejects.toBeInstanceOf(
      PlannerRefusalError,
    );
    const cut = fakeClient({ stop_reason: "max_tokens", content: [{ type: "text", text: "{" }] });
    await expect(anthropicProvider({ client: cut.client }).generateJson({ system: "", messages: [], schema: {} })).rejects.toThrow(/max_tokens/);
  });

  it("plugs into the engine as a planner", async () => {
    const { client } = fakeClient({ stop_reason: "end_turn", content: [{ type: "text", text: JSON.stringify(answer) }] });
    const engine = createEngine({
      model: fleet,
      dialect: { name: "fake", compile: () => ({ sql: "select 1", params: [] }) },
      executor: { execute: async () => ({ rows: [{ "trips.count": 7 }], truncated: false }) },
      planner: createPlanner(anthropicProvider({ client })),
    });
    const result = await engine.ask("How many trips?", { auth: authA });
    expect(result).toMatchObject({ type: "answer", rows: [{ "trips.count": 7 }], explanation: "Trips." });
  });
});

const live = process.env.NOQUERY_LIVE_ANTHROPIC === "1";

describe.skipIf(!live)("anthropicProvider (live API)", () => {
  it("plans a real question against the fixture catalog", async () => {
    const planner = createPlanner(anthropicProvider({ effort: "low" }));
    const result = await planner.plan({
      catalog: buildCatalog(fleet, authA),
      question: "How many trips per day last week, split by device model?",
      timezone: "America/Toronto",
      now: new Date(),
    });
    expect(result.kind).toBe("query");
    if (result.kind !== "query") return;
    expect(result.ir.measures).toEqual(["trips.count"]);
    expect(result.ir.dimensions).toEqual(["devices.model"]);
    expect(result.ir.time).toMatchObject({ field: "trips.started_at", grain: "day", range: "last_week" });
  }, 120_000);
});
