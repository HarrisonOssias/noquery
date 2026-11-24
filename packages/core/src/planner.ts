import type { Catalog, CatalogDimension } from "./catalog.js";
import { catalogToText } from "./catalog.js";
import type { Filter, FilterOp, FilterScalar, QueryIR, TimeGrain, TimePreset } from "./ir.js";
import { FILTER_OPS, TIME_GRAINS, TIME_PRESETS } from "./ir.js";
import type { QueryError } from "./resolve.js";

/** A JSON Schema object. Kept loose so any provider can pass it through. */
export type JsonSchema = Record<string, unknown>;

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface LLMRequest {
  /** Stable instructions plus the caller's catalog. Safe to cache per catalog. */
  system: string;
  messages: ChatMessage[];
  /** The response must be a JSON value matching this schema. */
  schema: JsonSchema;
}

/** The only thing a model provider has to implement: return JSON matching a schema. */
export interface LLMProvider {
  name: string;
  generateJson(request: LLMRequest): Promise<unknown>;
}

export type PlannerResult =
  | { kind: "query"; ir: QueryIR }
  | { kind: "clarify"; question: string; options: string[] }
  | { kind: "unanswerable"; reason: string };

export interface PlannerExample {
  question: string;
  ir: QueryIR;
}

export interface PlannerTurn {
  question: string;
  result: PlannerResult;
}

export interface PlanInput {
  catalog: Catalog;
  question: string;
  timezone: string;
  now: Date;
  history?: PlannerTurn[];
  /** Set on a retry: the previous answer and why it failed validation. */
  repair?: { previous: unknown; errors: QueryError[] };
}

export interface Planner {
  plan(input: PlanInput): Promise<PlannerResult>;
}

/**
 * Thrown by providers when the model call itself fails (bad credentials, rate limit, network).
 * The message is shown to callers, so it must never contain secrets.
 */
export class PlannerError extends Error {
  constructor(
    /** Short reason, e.g. "authentication", "rate_limited", "unreachable", "api_error". */
    readonly reason: string,
    message: string,
    /** True when retrying later may succeed. */
    readonly retryable = false,
  ) {
    super(message);
    this.name = "PlannerError";
  }
}

export interface PlannerOptions {
  /** Verified question-to-IR pairs shown to the model as examples (golden queries). */
  examples?: PlannerExample[];
  /** Extra domain guidance appended to the instructions. */
  instructions?: string;
}

const INSTRUCTIONS = `You turn questions about a company's data into a query for an analytics engine. You never write SQL; the engine compiles and secures your query, and rejects any field that is not in the catalog below.

How to build a query:
- Use only field references that appear in the catalog, written exactly as "entity.field".
- All measures in one query must come from the same entity. Dimensions and filters may use that entity or the entities listed under "can be combined with".
- For trends or "per day/week/month" questions, set time.grain. For "last week", "this month" and similar, use time.range_preset. For explicit dates use range_from and range_to as YYYY-MM-DD; range_to is exclusive, so "in March 2026" is from 2026-03-01 to 2026-04-01.
- When a question mentions a period but no time field, use the entity's default time field.
- Filter values are always given as strings in "values": numbers as "42", yes/no as "true"/"false", dates as YYYY-MM-DD. Use "in" with several values for "any of". Use the listed values exactly when a dimension lists them.
- To filter on a total ("devices with more than 10 trips"), put a filter on the measure.
- For "top N" questions, sort by the measure descending and set limit to N.

When to do something other than query:
- kind "clarify": only when the question could reasonably mean two things that give materially different answers. Ask one short question and offer the likely options. Otherwise pick the most sensible reading.
- kind "unanswerable": when the catalog has no data for the question, or the question asks to change data. Say what is missing and name the closest field that does exist.

Set the fields that do not apply to the chosen kind to null.`;

/** Builds the JSON Schema the model's answer must match, with field names as enums. */
export function plannerSchema(catalog: Catalog): JsonSchema {
  const measures = catalog.entities.flatMap((e) => e.measures.map((m) => m.ref));
  const dimensions = catalog.entities.flatMap((e) => e.dimensions.map((d) => d.ref));
  const times = catalog.entities.flatMap((e) => e.dimensions.filter((d) => d.type === "time").map((d) => d.ref));
  const enumOf = (values: string[]) => (values.length > 0 ? { type: "string", enum: values } : { type: "string" });
  const nullable = (schema: JsonSchema) => ({ anyOf: [schema, { type: "null" }] });
  const object = (properties: Record<string, JsonSchema>) => ({
    type: "object",
    additionalProperties: false,
    required: Object.keys(properties),
    properties,
  });

  const query = object({
    measures: { type: "array", items: enumOf(measures) },
    dimensions: { type: "array", items: enumOf(dimensions) },
    time: nullable(
      object({
        field: enumOf(times),
        grain: nullable({ type: "string", enum: [...TIME_GRAINS] }),
        range_preset: nullable({ type: "string", enum: [...TIME_PRESETS] }),
        range_from: nullable({ type: "string" }),
        range_to: nullable({ type: "string" }),
      }),
    ),
    filters: {
      type: "array",
      items: object({
        field: enumOf([...dimensions, ...measures]),
        op: { type: "string", enum: [...FILTER_OPS] },
        values: { type: "array", items: { type: "string" } },
      }),
    },
    order_by: {
      type: "array",
      items: object({
        field: enumOf([...dimensions, ...measures]),
        dir: { type: "string", enum: ["asc", "desc"] },
      }),
    },
    limit: nullable({ type: "integer" }),
  });

  return object({
    kind: { type: "string", enum: ["query", "clarify", "unanswerable"] },
    query: nullable(query),
    clarification: nullable(object({ question: { type: "string" }, options: { type: "array", items: { type: "string" } } })),
    reason: nullable({ type: "string" }),
  });
}

interface RawTime {
  field: string;
  grain: TimeGrain | null;
  range_preset: TimePreset | null;
  range_from: string | null;
  range_to: string | null;
}

interface RawQuery {
  measures: string[];
  dimensions: string[];
  time: RawTime | null;
  filters: Array<{ field: string; op: FilterOp; values: string[] }>;
  order_by: Array<{ field: string; dir: "asc" | "desc" }>;
  limit: number | null;
}

interface RawAnswer {
  kind: "query" | "clarify" | "unanswerable";
  query: RawQuery | null;
  clarification: { question: string; options: string[] } | null;
  reason: string | null;
}

export function createPlanner(provider: LLMProvider, options: PlannerOptions = {}): Planner {
  return {
    async plan(input: PlanInput): Promise<PlannerResult> {
      const system = buildSystemPrompt(input.catalog, options);
      const messages: ChatMessage[] = [];
      for (const turn of input.history ?? []) {
        messages.push({ role: "user", content: turn.question });
        messages.push({ role: "assistant", content: JSON.stringify(turn.result) });
      }
      messages.push({ role: "user", content: `${dateLine(input.now, input.timezone)}\n\nQuestion: ${input.question}` });
      if (input.repair) {
        messages.push({ role: "assistant", content: JSON.stringify(input.repair.previous) });
        messages.push({
          role: "user",
          content: `The engine rejected that query:\n${input.repair.errors.map((e) => `- ${e.message}`).join("\n")}\nReturn a corrected answer to the same question.`,
        });
      }
      const raw = (await provider.generateJson({ system, messages, schema: plannerSchema(input.catalog) })) as RawAnswer;
      return toPlannerResult(raw, input.catalog);
    },
  };
}

function buildSystemPrompt(catalog: Catalog, options: PlannerOptions): string {
  const parts = [INSTRUCTIONS];
  if (options.instructions) parts.push(`Guidance for this data:\n${options.instructions}`);
  parts.push(`# Catalog\n\n${catalogToText(catalog)}`);
  if (options.examples?.length) {
    const examples = options.examples
      .map((e) => `Question: ${e.question}\nQuery: ${JSON.stringify(e.ir)}`)
      .join("\n\n");
    parts.push(`# Verified examples\n\n${examples}`);
  }
  return parts.join("\n\n");
}

function dateLine(now: Date, timezone: string): string {
  const fmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    weekday: "long",
  });
  const parts = Object.fromEntries(fmt.formatToParts(now).map((p) => [p.type, p.value]));
  return `Today is ${parts.weekday}, ${parts.year}-${parts.month}-${parts.day} in ${timezone}.`;
}

/** Converts the model's answer into a PlannerResult, coercing string filter values by field type. */
export function toPlannerResult(raw: RawAnswer, catalog: Catalog): PlannerResult {
  if (!raw || typeof raw !== "object") return { kind: "unanswerable", reason: "The planner returned no answer." };
  if (raw.kind === "clarify") {
    return {
      kind: "clarify",
      question: raw.clarification?.question ?? "Could you say more about what you need?",
      options: raw.clarification?.options ?? [],
    };
  }
  if (raw.kind === "unanswerable" || !raw.query) {
    return { kind: "unanswerable", reason: raw.reason ?? "That question can't be answered from the available data." };
  }

  const dims = new Map<string, CatalogDimension>();
  for (const e of catalog.entities) for (const d of e.dimensions) dims.set(d.ref, d);

  const q = raw.query;
  const ir: QueryIR = { version: 1 };
  if (q.measures?.length) ir.measures = q.measures;
  if (q.dimensions?.length) ir.dimensions = q.dimensions;
  if (q.time) {
    ir.time = { field: q.time.field };
    if (q.time.grain) ir.time.grain = q.time.grain;
    if (q.time.range_preset) ir.time.range = q.time.range_preset;
    else if (q.time.range_from && q.time.range_to) ir.time.range = { from: q.time.range_from, to: q.time.range_to };
  }
  if (q.filters?.length) {
    ir.filters = q.filters.map((f): Filter => {
      const dim = dims.get(f.field);
      const coerce = (v: string): FilterScalar => {
        if (!dim) return toNumber(v); // a measure: totals are numbers
        if (dim.type === "number") return toNumber(v);
        if (dim.type === "boolean") return v === "true" ? true : v === "false" ? false : v;
        return v;
      };
      const values = f.values ?? [];
      if (f.op === "is_null" || f.op === "is_not_null") return { field: f.field, op: f.op };
      if (f.op === "in" || f.op === "not_in") return { field: f.field, op: f.op, value: values.map(coerce) };
      return { field: f.field, op: f.op, value: coerce(values[0] ?? "") };
    });
  }
  if (q.order_by?.length) ir.orderBy = q.order_by.map((o) => ({ field: o.field, dir: o.dir }));
  if (typeof q.limit === "number") ir.limit = q.limit;
  return { kind: "query", ir };
}

function toNumber(v: string): FilterScalar {
  const n = Number(v);
  return v.trim() !== "" && Number.isFinite(n) ? n : v;
}
