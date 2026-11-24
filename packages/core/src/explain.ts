import type { Filter, TimeRange } from "./ir.js";
import type { ResolvedQuery } from "./resolve.js";

const PRESET_TEXT: Record<string, string> = {
  today: "today",
  yesterday: "yesterday",
  last_7_days: "in the last 7 days",
  last_14_days: "in the last 14 days",
  last_30_days: "in the last 30 days",
  last_90_days: "in the last 90 days",
  this_week: "this week",
  last_week: "last week",
  this_month: "this month",
  last_month: "last month",
  this_quarter: "this quarter",
  last_quarter: "last quarter",
  this_year: "this year",
  last_year: "last year",
};

const OP_TEXT: Record<Filter["op"], string> = {
  eq: "is",
  neq: "is not",
  gt: "is more than",
  gte: "is at least",
  lt: "is less than",
  lte: "is at most",
  in: "is one of",
  not_in: "is not one of",
  contains: "contains",
  starts_with: "starts with",
  is_null: "is empty",
  is_not_null: "is not empty",
};

/**
 * A plain-English sentence describing what a query actually does.
 * Generated from the resolved query, not by a model, so it always matches what ran.
 */
export function explainQuery(query: ResolvedQuery): string {
  const label = (ref: string) => query.labels[ref] ?? ref;
  const lower = (s: string) => s.charAt(0).toLowerCase() + s.slice(1);
  const parts: string[] = [];

  const measures = query.columns.filter((c) => c.role === "measure").map((c) => c.label);
  const dims = query.columns.filter((c) => c.role === "dimension").map((c) => lower(c.label));
  const time = query.columns.find((c) => c.role === "time");

  if (measures.length > 0) {
    parts.push(capitalize(joinWords(measures.map((m, i) => (i === 0 ? m : lower(m))))));
  } else {
    parts.push(`Distinct ${joinWords(dims)}`);
  }
  if (measures.length > 0 && dims.length > 0) parts.push(`by ${joinWords(dims)}`);
  if (time) parts.push(`per ${time.grain}`);

  const range = query.ir.time?.range;
  if (range) parts.push(rangeText(range));
  if (query.ir.time && (range || time)) parts.push(`(${query.timezone})`);

  const filters = query.ir.filters ?? [];
  if (filters.length > 0) {
    const clauses = filters.map((f) => {
      const name = lower(query.labels[f.field] ?? f.field);
      const value = f.value === undefined ? "" : ` ${Array.isArray(f.value) ? f.value.join(", ") : String(f.value)}`;
      return `${name} ${OP_TEXT[f.op]}${value}`;
    });
    parts.push(`where ${joinWords(clauses)}`);
  }

  let sentence = parts.join(" ") + ".";
  const order = query.orderBy[0];
  if (order && query.ir.orderBy?.length) {
    sentence += ` Sorted by ${lower(label(query.columns.find((c) => c.key === order.key)?.ref ?? order.key))}, ${order.dir === "desc" ? "highest" : "lowest"} first.`;
  }
  if (query.ir.limit !== undefined) sentence += ` Showing up to ${query.limit} rows.`;
  return sentence;
}

function rangeText(range: TimeRange): string {
  if (typeof range === "string") return PRESET_TEXT[range] ?? range;
  return `from ${range.from} to ${range.to}`;
}

function joinWords(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`;
}

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}
