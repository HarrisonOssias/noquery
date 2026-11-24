export type { ColumnInfo, ColumnKind, ForeignKeyInfo, SchemaAdapter, SchemaGraph, TableInfo } from "./schema.js";
export type {
  Aggregation,
  Dimension,
  DimensionType,
  Entity,
  EntityOverlay,
  FieldPolicy,
  Join,
  Measure,
  MeasureFormat,
  ModelIssue,
  Relationship,
  RowScope,
  SemanticModel,
  SemanticOverlay,
  TimeStorage,
} from "./semantic.js";
export { checkSemantics, defineSemantics, mergeSemantics } from "./semantic.js";
export type { DraftOptions } from "./draft.js";
export { draftSemantics, humanize } from "./draft.js";
export type { Filter, FilterOp, FilterScalar, OrderBy, QueryIR, TimeGrain, TimePreset, TimeRange, TimeSpec } from "./ir.js";
export { FILTER_OPS, TIME_GRAINS, TIME_PRESETS } from "./ir.js";
export type {
  AuthContext,
  Condition,
  HavingCondition,
  JoinStep,
  OutputColumn,
  OutputRole,
  QueryError,
  QueryErrorCode,
  ResolvedQuery,
  ResolveOptions,
  ResolveResult,
  SelectedDimension,
  SelectedMeasure,
  SelectedTime,
  TableRef,
} from "./resolve.js";
export { hasAccess, isDateOnly, isTimezone, planJoins, resolveQuery } from "./resolve.js";
export type { CompiledQuery, Dialect, ExecuteOptions, ExecuteResult, Executor } from "./runtime.js";
export { explainQuery } from "./explain.js";
export type { ChartSpec, ChartType } from "./chart.js";
export { suggestChart } from "./chart.js";
export type { Catalog, CatalogDimension, CatalogEntity, CatalogField } from "./catalog.js";
export { buildCatalog, catalogToText } from "./catalog.js";
export type {
  ChatMessage,
  JsonSchema,
  LLMProvider,
  LLMRequest,
  PlanInput,
  Planner,
  PlannerExample,
  PlannerOptions,
  PlannerResult,
  PlannerTurn,
} from "./planner.js";
export { createPlanner, PlannerError, plannerSchema, toPlannerResult } from "./planner.js";
export type { AskOptions, AskResult, AuditEvent, CallOptions, Engine, EngineOptions, RunResult } from "./engine.js";
export { createEngine, QueryValidationError } from "./engine.js";
