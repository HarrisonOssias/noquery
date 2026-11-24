"""Planner provider backed by Claude.

Install with `pip install "noquery[anthropic]"`. Uses structured outputs, so the answer always
parses and only names fields from the catalog.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

import anthropic

from ..errors import PlannerFailedError, PlannerRefusalError

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    """Create one with `anthropic_provider()`."""

    def __init__(
        self,
        client: Optional[Any] = None,
        model: str = DEFAULT_MODEL,
        effort: Optional[str] = None,
        max_tokens: int = 16000,
        fallbacks: bool = True,
    ):
        self.client = client if client is not None else anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks
        self.name = f"anthropic:{model}"

    def generate_json(self, system: str, messages: Sequence[Mapping[str, str]], schema: Mapping[str, Any]) -> Any:
        output_config: Dict[str, Any] = {"format": {"type": "json_schema", "schema": dict(schema)}}
        if self.effort:
            output_config["effort"] = self.effort
        request: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            # The system prompt holds the instructions and catalog: stable per caller, so cache it.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "output_config": output_config,
        }
        if self.fallbacks:
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"
        try:
            response = self.client.beta.messages.create(**request)
        except anthropic.AuthenticationError:
            raise PlannerFailedError.from_reason(
                "authentication", "Claude rejected the API credentials (401). Check ANTHROPIC_API_KEY where noquery runs."
            ) from None
        except anthropic.PermissionDeniedError:
            raise PlannerFailedError.from_reason("permission_denied", f"This API key may not use {self.model} (403).") from None
        except anthropic.NotFoundError:
            raise PlannerFailedError.from_reason(
                "not_found", f'Claude API returned 404. Check the model name "{self.model}".'
            ) from None
        except anthropic.RateLimitError:
            raise PlannerFailedError.from_reason(
                "rate_limited", "Claude API rate limit reached (429). Try again shortly.", True
            ) from None
        except anthropic.APIStatusError as error:
            status = getattr(error, "status_code", None)
            retryable = isinstance(status, int) and status >= 500
            raise PlannerFailedError.from_reason(
                "api_error", f"Claude API error ({status})." if status else "Claude API error.", retryable
            ) from None
        except anthropic.APIConnectionError:
            raise PlannerFailedError.from_reason(
                "unreachable", "Could not reach the Claude API. Check network access where noquery runs.", True
            ) from None

        except (TypeError, getattr(anthropic, "CredentialsError", TypeError)) as error:
            # The SDK raises a bare TypeError when it finds no API key, token or profile, and
            # CredentialsError when a stored profile cannot be read.
            if isinstance(error, TypeError) and "authentication method" not in str(error):
                raise
            raise PlannerFailedError.from_reason(
                "authentication",
                "No Anthropic credentials found. Set ANTHROPIC_API_KEY where noquery runs, or pass a client to anthropic_provider().",
            ) from None

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise PlannerRefusalError(getattr(details, "category", None))
        if response.stop_reason == "max_tokens":
            raise RuntimeError(f"The planner ran out of output tokens (max_tokens={self.max_tokens}).")
        text = "".join(block.text for block in response.content if block.type == "text")
        return json.loads(text)


def anthropic_provider(
    client: Optional[Any] = None,
    model: str = DEFAULT_MODEL,
    effort: Optional[str] = None,
    max_tokens: int = 16000,
    fallbacks: bool = True,
) -> AnthropicProvider:
    """Planner provider backed by Claude.

    Args:
        client: An existing `anthropic.Anthropic` client. Defaults to `anthropic.Anthropic()`, which
            reads credentials from the environment.
        model: Default "claude-opus-5-5".
        effort: Reasoning depth: "low", "medium", "high", "xhigh" or "max". Planning a query is
            usually fine at the model's default or "low".
        max_tokens: Default 16000.
        fallbacks: Re-run a declined request on a fallback model inside the same call (server-side
            fallback). Turn off on platforms that do not support it (Bedrock, Vertex, Foundry).
    """
    return AnthropicProvider(client=client, model=model, effort=effort, max_tokens=max_tokens, fallbacks=fallbacks)
