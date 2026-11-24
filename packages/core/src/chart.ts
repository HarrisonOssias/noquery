import type { OutputColumn } from "./resolve.js";

export type ChartType = "kpi" | "line" | "bar" | "scatter" | "table";

export interface ChartSpec {
  type: ChartType;
  /** Column key on the x axis (category or time). */
  x?: string;
  /** Measure column keys plotted. */
  y: string[];
  /** Column key that splits the data into series. */
  series?: string;
}

const MAX_SERIES = 6;
const MAX_BARS = 30;

/** Picks a default chart from the shape of the result. Rule-based so it is predictable. */
export function suggestChart(columns: OutputColumn[], rows: Array<Record<string, unknown>> = []): ChartSpec {
  const measures = columns.filter((c) => c.role === "measure");
  const time = columns.find((c) => c.role === "time");
  const dims = columns.filter((c) => c.role === "dimension");
  const y = measures.map((m) => m.key);
  const table: ChartSpec = { type: "table", y };

  if (measures.length === 0) return table;

  if (!time && dims.length === 0) {
    return measures.length === 1 ? { type: "kpi", y } : table;
  }

  if (time && dims.length === 0) return { type: "line", x: time.key, y };

  if (time && dims.length === 1 && measures.length === 1) {
    const series = dims[0]!.key;
    const distinct = new Set(rows.map((r) => r[series])).size;
    return distinct <= MAX_SERIES ? { type: "line", x: time.key, y, series } : table;
  }

  if (!time && dims.length === 1) {
    if (measures.length === 2 && rows.length > MAX_BARS) {
      return { type: "scatter", x: measures[0]!.key, y: [measures[1]!.key] };
    }
    return rows.length <= MAX_BARS ? { type: "bar", x: dims[0]!.key, y } : table;
  }

  return table;
}
