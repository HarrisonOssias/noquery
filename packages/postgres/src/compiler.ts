import type {
  CompiledQuery,
  Condition,
  Dialect,
  Dimension,
  FilterScalar,
  HavingCondition,
  Measure,
  ResolvedQuery,
  TableRef,
  TimeGrain,
  TimeRange,
} from "@noquery/core";
import { isDateOnly } from "@noquery/core";

export function quoteIdent(name: string): string {
  return `"${name.replace(/"/g, '""')}"`;
}

const GRAIN_FORMAT: Record<TimeGrain, string> = {
  hour: `'YYYY-MM-DD"T"HH24:00'`,
  day: `'YYYY-MM-DD'`,
  week: `'YYYY-MM-DD'`,
  month: `'YYYY-MM-DD'`,
  quarter: `'YYYY-MM-DD'`,
  year: `'YYYY-MM-DD'`,
};

const PRESET_UNIT: Record<string, { unit: string; step: string }> = {
  week: { unit: "week", step: "1 week" },
  month: { unit: "month", step: "1 month" },
  quarter: { unit: "quarter", step: "3 months" },
  year: { unit: "year", step: "1 year" },
};

const COMPARE: Record<string, string> = { eq: "=", gt: ">", gte: ">=", lt: "<", lte: "<=" };

export interface PostgresDialectOptions {
  /**
   * Fetch one row more than the query's limit so callers can tell the result was cut off.
   * The engine slices the extra row away. Default true.
   */
  probeTruncation?: boolean;
}

/**
 * PostgreSQL dialect. Every value is a bound parameter; identifiers come from the
 * semantic model and are always quoted. Time buckets come back as local wall-clock strings
 * (`YYYY-MM-DD`, or `YYYY-MM-DDTHH:00` for hours) in the query's time zone.
 */
export function postgresDialect(options: PostgresDialectOptions = {}): Dialect {
  const probe = options.probeTruncation ?? true;
  return {
    name: "postgres",
    compile(query: ResolvedQuery): CompiledQuery {
      const params: unknown[] = [];
      const param = (value: unknown) => {
        params.push(value);
        return `$${params.length}`;
      };
      let tzRef: string | undefined;
      const tz = () => (tzRef ??= `${param(query.timezone)}::text`);

      const column = (alias: string, name: string) => `${quoteIdent(alias)}.${quoteIdent(name)}`;
      const fieldExpr = (alias: string, field: { column?: string; sql?: string }) => {
        if (field.sql) return `(${field.sql.replaceAll("${TABLE}", quoteIdent(alias))})`;
        return column(alias, field.column!);
      };
      /** The field as an absolute instant (timestamptz). */
      const instant = (alias: string, dim: Dimension) => {
        const e = fieldExpr(alias, dim);
        switch (dim.timeStorage ?? "instant") {
          case "utc":
            return `(${e}::timestamp AT TIME ZONE 'UTC')`;
          case "local":
            return `(${e}::timestamp AT TIME ZONE ${tz()})`;
          default:
            return e;
        }
      };
      /** A user-supplied time value as an instant: dates are read in the query's time zone. */
      const instantValue = (value: string) =>
        isDateOnly(value) ? `(${param(value)}::timestamp AT TIME ZONE ${tz()})` : `${param(value)}::timestamptz`;
      const measureExpr = (alias: string, m: Measure) => {
        const target = m.column || m.sql ? fieldExpr(alias, m) : undefined;
        switch (m.agg) {
          case undefined:
            return fieldExpr(alias, m);
          case "count":
            return target ? `count(${target})` : "count(*)";
          case "count_distinct":
            return `count(DISTINCT ${target})`;
          default:
            return `${m.agg}(${target})`;
        }
      };

      const select: string[] = [];
      let groupCount = 0;
      if (query.time) {
        const local = `(${instant(query.time.alias, query.time.dimension)} AT TIME ZONE ${tz()})`;
        const fmt = GRAIN_FORMAT[query.time.grain];
        select.push(`to_char(date_trunc('${query.time.grain}', ${local}), ${fmt}) AS ${quoteIdent(query.time.key)}`);
        groupCount++;
      }
      for (const d of query.dimensions) {
        select.push(`${fieldExpr(d.alias, d.dimension)} AS ${quoteIdent(d.key)}`);
        groupCount++;
      }
      for (const m of query.measures) {
        select.push(`${measureExpr(m.alias, m.measure)} AS ${quoteIdent(m.key)}`);
      }

      const from = [`FROM ${table(query.base)}`];
      for (const j of query.joins) {
        const on = j.on.map(([p, c]) => `${column(j.parentAlias, p)} = ${column(j.alias, c)}`).join(" AND ");
        from.push(`LEFT JOIN ${table(j)} ON ${on}`);
      }

      const where = query.where.map((c) => condition(c));
      const having = query.having.map((h: HavingCondition) => {
        const op = h.op === "neq" ? "<>" : COMPARE[h.op]!;
        return `${measureExpr(h.alias, h.measure)} ${op} ${param(h.value)}`;
      });

      function condition(c: Condition): string {
        if (c.kind === "scope") {
          const col = column(c.alias, c.column);
          return Array.isArray(c.value) ? `${col} = ANY(${param(c.value)})` : `${col} = ${param(c.value)}`;
        }
        if (c.kind === "time_range") {
          const [lo, hi] = rangeBounds(c.range);
          const ts = instant(c.alias, c.dimension);
          return `${ts} >= ${lo} AND ${ts} < ${hi}`;
        }
        const dim = c.dimension;
        const isTime = dim.type === "time";
        const e = isTime ? instant(c.alias, dim) : fieldExpr(c.alias, dim);
        const v = (value: FilterScalar) => (isTime ? instantValue(String(value)) : param(value));
        switch (c.op) {
          case "eq":
          case "gt":
          case "gte":
          case "lt":
          case "lte":
            return `${e} ${COMPARE[c.op]} ${v(c.value as FilterScalar)}`;
          case "neq":
            return `${e} IS DISTINCT FROM ${v(c.value as FilterScalar)}`;
          case "in":
            return isTime
              ? `${e} IN (${(c.value as FilterScalar[]).map(v).join(", ")})`
              : `${e} = ANY(${param(c.value)})`;
          case "not_in":
            return isTime
              ? `(${e} IS NULL OR ${e} NOT IN (${(c.value as FilterScalar[]).map(v).join(", ")}))`
              : `(${e} IS NULL OR NOT (${e} = ANY(${param(c.value)})))`;
          case "contains":
            return `${e} ILIKE ${param(`%${escapeLike(String(c.value))}%`)}`;
          case "starts_with":
            return `${e} ILIKE ${param(`${escapeLike(String(c.value))}%`)}`;
          case "is_null":
            return `${e} IS NULL`;
          case "is_not_null":
            return `${e} IS NOT NULL`;
        }
      }

      function rangeBounds(range: TimeRange): [string, string] {
        if (typeof range !== "string") return [instantValue(range.from), instantValue(range.to)];
        const nowLocal = `(now() AT TIME ZONE ${tz()})`;
        const day = `date_trunc('day', ${nowLocal})`;
        const at = (local: string) => `((${local}) AT TIME ZONE ${tz()})`;
        const lastDays = /^last_(\d+)_days$/.exec(range);
        if (lastDays) {
          const n = Number(lastDays[1]) - 1;
          return [at(`${day} - interval '${n} days'`), at(`${day} + interval '1 day'`)];
        }
        if (range === "today") return [at(day), at(`${day} + interval '1 day'`)];
        if (range === "yesterday") return [at(`${day} - interval '1 day'`), at(day)];
        const [which, unitName] = range.split("_") as ["this" | "last", string];
        const unit = PRESET_UNIT[unitName];
        if (!unit) throw new Error(`Unsupported time range "${range}".`);
        const start = `date_trunc('${unit.unit}', ${nowLocal})`;
        return which === "this"
          ? [at(start), at(`${start} + interval '${unit.step}'`)]
          : [at(`${start} - interval '${unit.step}'`), at(start)];
      }

      const parts = [`SELECT ${select.join(", ")}`, ...from];
      if (where.length) parts.push(`WHERE ${where.join(" AND ")}`);
      if (groupCount > 0) {
        parts.push(`GROUP BY ${Array.from({ length: groupCount }, (_, i) => i + 1).join(", ")}`);
      }
      if (having.length) parts.push(`HAVING ${having.join(" AND ")}`);
      if (query.orderBy.length) {
        parts.push(
          `ORDER BY ${query.orderBy.map((o) => `${quoteIdent(o.key)} ${o.dir === "desc" ? "DESC" : "ASC"} NULLS LAST`).join(", ")}`,
        );
      }
      const limit = Math.trunc(query.limit) + (probe ? 1 : 0);
      parts.push(`LIMIT ${limit}`);

      return { sql: parts.join("\n"), params };
    },
  };
}

function table(t: TableRef): string {
  const name = t.schema ? `${quoteIdent(t.schema)}.${quoteIdent(t.table)}` : quoteIdent(t.table);
  return `${name} AS ${quoteIdent(t.alias)}`;
}

function escapeLike(value: string): string {
  return value.replace(/[\\%_]/g, (m) => `\\${m}`);
}
