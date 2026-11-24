/**
 * The Query IR is the contract between the planner (an LLM or a person) and the database.
 * It can only name fields the semantic layer defines, as "entity.field".
 */

export const TIME_GRAINS = ["hour", "day", "week", "month", "quarter", "year"] as const;
export type TimeGrain = (typeof TIME_GRAINS)[number];

export const TIME_PRESETS = [
  "today",
  "yesterday",
  "last_7_days",
  "last_14_days",
  "last_30_days",
  "last_90_days",
  "this_week",
  "last_week",
  "this_month",
  "last_month",
  "this_quarter",
  "last_quarter",
  "this_year",
  "last_year",
] as const;
export type TimePreset = (typeof TIME_PRESETS)[number];

/**
 * A preset, or an absolute half-open range [from, to).
 * `from`/`to` are `YYYY-MM-DD` (read in the query's time zone) or full ISO-8601 with an offset.
 */
export type TimeRange = TimePreset | { from: string; to: string };

export interface TimeSpec {
  /** A time dimension, "entity.field". */
  field: string;
  /** Bucket results by this grain. Omit to filter by range without bucketing. */
  grain?: TimeGrain;
  range?: TimeRange;
  /** IANA time zone used for buckets and ranges. Defaults to the engine's default. */
  tz?: string;
}

export const FILTER_OPS = [
  "eq",
  "neq",
  "gt",
  "gte",
  "lt",
  "lte",
  "in",
  "not_in",
  "contains",
  "starts_with",
  "is_null",
  "is_not_null",
] as const;
export type FilterOp = (typeof FILTER_OPS)[number];

export type FilterScalar = string | number | boolean;

export interface Filter {
  /** A dimension (row filter) or a measure (filter on the aggregate). */
  field: string;
  op: FilterOp;
  value?: FilterScalar | FilterScalar[];
}

export interface OrderBy {
  /** A selected measure, dimension, or the time field. */
  field: string;
  dir: "asc" | "desc";
}

export interface QueryIR {
  version: 1;
  measures?: string[];
  dimensions?: string[];
  time?: TimeSpec;
  filters?: Filter[];
  orderBy?: OrderBy[];
  limit?: number;
}
