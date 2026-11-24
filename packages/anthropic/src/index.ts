import Anthropic from "@anthropic-ai/sdk";
import type { LLMProvider, LLMRequest } from "@noquery/core";
import { PlannerError } from "@noquery/core";

export interface AnthropicProviderOptions {
  /** An existing client. Defaults to `new Anthropic()`, which reads credentials from the environment. */
  client?: Anthropic;
  /** Default "claude-opus-5-5". */
  model?: string;
  /** Reasoning depth. Planning a query is usually fine at "medium" (the model's default) or "low". */
  effort?: "low" | "medium" | "high" | "xhigh" | "max";
  /** Default 16000. */
  maxTokens?: number;
  /**
   * Re-run a declined request on a fallback model inside the same call (server-side fallback).
   * Default true. Turn off on platforms that don't support it (Bedrock, Vertex, Foundry).
   */
  fallbacks?: boolean;
}

export class PlannerRefusalError extends Error {
  constructor(readonly category: string | null) {
    super(`The model declined to plan this question${category ? ` (${category})` : ""}.`);
    this.name = "PlannerRefusalError";
  }
}

/**
 * Planner provider backed by Claude. Uses structured outputs, so the answer always
 * parses and only names fields from the catalog.
 */
export function anthropicProvider(options: AnthropicProviderOptions = {}): LLMProvider {
  const client = options.client ?? new Anthropic();
  const model = options.model ?? "claude-opus-5-5";
  const maxTokens = options.maxTokens ?? 16000;
  const useFallbacks = options.fallbacks ?? true;

  return {
    name: `anthropic:${model}`,
    async generateJson(request: LLMRequest): Promise<unknown> {
      let response;
      try {
        response = await client.beta.messages.create({
          model,
          max_tokens: maxTokens,
          // The system prompt holds the instructions and catalog: stable per caller, so cache it.
          system: [{ type: "text", text: request.system, cache_control: { type: "ephemeral" } }],
          messages: request.messages.map((m) => ({ role: m.role, content: m.content })),
          output_config: {
            format: { type: "json_schema", schema: request.schema },
            ...(options.effort ? { effort: options.effort } : {}),
          },
          ...(useFallbacks ? { betas: ["server-side-fallback-2026-07-01"], fallbacks: "default" } : {}),
        });
      } catch (error) {
        throw toPlannerError(error, model);
      }

      if (response.stop_reason === "refusal") {
        throw new PlannerRefusalError(response.stop_details?.category ?? null);
      }
      if (response.stop_reason === "max_tokens") {
        throw new Error(`The planner ran out of output tokens (max_tokens=${maxTokens}).`);
      }
      const text = response.content
        .filter((b): b is Anthropic.Beta.BetaTextBlock => b.type === "text")
        .map((b) => b.text)
        .join("");
      return JSON.parse(text);
    },
  };
}

/** Maps SDK errors to PlannerError with messages that are safe to show callers. */
function toPlannerError(error: unknown, model: string): unknown {
  if (error instanceof Anthropic.AuthenticationError) {
    return new PlannerError("authentication", "Claude rejected the API credentials (401). Check ANTHROPIC_API_KEY on the noquery server.");
  }
  if (error instanceof Anthropic.PermissionDeniedError) {
    return new PlannerError("permission_denied", `This API key may not use ${model} (403).`);
  }
  if (error instanceof Anthropic.NotFoundError) {
    return new PlannerError("not_found", `Claude API returned 404. Check the model name "${model}".`);
  }
  if (error instanceof Anthropic.RateLimitError) {
    return new PlannerError("rate_limited", "Claude API rate limit reached (429). Try again shortly.", true);
  }
  if (error instanceof Anthropic.APIConnectionError) {
    return new PlannerError("unreachable", "Could not reach the Claude API. Check the server's network access.", true);
  }
  if (error instanceof Anthropic.APIError) {
    const retryable = typeof error.status === "number" && error.status >= 500;
    return new PlannerError("api_error", `Claude API error${error.status ? ` (${error.status})` : ""}.`, retryable);
  }
  return error;
}
