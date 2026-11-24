import json
from types import SimpleNamespace

import pytest

anthropic = pytest.importorskip("anthropic", reason="needs the anthropic extra")

import httpx

from noquery import PlannerFailedError, PlannerRefusalError, create_planner
from noquery.integrations.anthropic import AnthropicProvider, anthropic_provider

from conftest import AUTH_A

ANSWER = {"kind": "unanswerable", "query": None, "clarification": None, "reason": "No battery data."}
SCHEMA = {"type": "object", "properties": {"kind": {"type": "string"}}, "required": ["kind"], "additionalProperties": False}
MESSAGES = [{"role": "user", "content": "Question: battery?"}]


def response(text=None, stop_reason="end_turn", stop_details=None, content=None):
    if content is None:
        content = [SimpleNamespace(type="text", text=text if text is not None else json.dumps(ANSWER))]
    return SimpleNamespace(content=content, stop_reason=stop_reason, stop_details=stop_details)


class FakeClient:
    def __init__(self, result):
        self.requests = []
        self._result = result
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **request):
        self.requests.append(request)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def status_error(cls, status):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("sk-ant-secret rejected", response=httpx.Response(status, request=request), body=None)


def test_sends_a_cached_system_prompt_structured_output_and_fallbacks():
    client = FakeClient(response())
    provider = anthropic_provider(client=client)
    assert provider.name == "anthropic:claude-opus-5-5"
    assert provider.generate_json(system="You plan queries.", messages=MESSAGES, schema=SCHEMA) == ANSWER
    assert client.requests == [
        {
            "model": "claude-opus-5-5",
            "max_tokens": 16000,
            "system": [{"type": "text", "text": "You plan queries.", "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": "Question: battery?"}],
            "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
    ]


def test_options():
    client = FakeClient(response())
    provider = anthropic_provider(client=client, model="claude-haiku-5-5", effort="low", max_tokens=2000, fallbacks=False)
    provider.generate_json(system="s", messages=MESSAGES, schema=SCHEMA)
    request = client.requests[0]
    assert request["model"] == "claude-haiku-5-5" and request["max_tokens"] == 2000
    assert request["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "low"}
    assert "betas" not in request and "fallbacks" not in request
    assert provider.name == "anthropic:claude-haiku-5-5"


def test_joins_text_blocks_and_skips_other_blocks():
    text = json.dumps(ANSWER)
    content = [
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="text", text=text[:10]),
        SimpleNamespace(type="fallback"),
        SimpleNamespace(type="text", text=text[10:]),
    ]
    provider = AnthropicProvider(client=FakeClient(response(content=content)))
    assert provider.generate_json(system="s", messages=MESSAGES, schema=SCHEMA) == ANSWER


def test_refusals_and_truncation():
    refused = AnthropicProvider(client=FakeClient(response(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"))))
    with pytest.raises(PlannerRefusalError) as raised:
        refused.generate_json(system="s", messages=MESSAGES, schema=SCHEMA)
    assert raised.value.category == "cyber" and raised.value.status == 422 and raised.value.code == "planner_refused"
    plain = AnthropicProvider(client=FakeClient(response(stop_reason="refusal")))
    with pytest.raises(PlannerRefusalError) as raised:
        plain.generate_json(system="s", messages=MESSAGES, schema=SCHEMA)
    assert raised.value.category is None and raised.value.message == "The model declined to plan this question."
    truncated = AnthropicProvider(client=FakeClient(response(text='{"kind"', stop_reason="max_tokens")), max_tokens=64)
    with pytest.raises(RuntimeError, match="max_tokens=64"):
        truncated.generate_json(system="s", messages=MESSAGES, schema=SCHEMA)


@pytest.mark.parametrize(
    "error, reason, status, retryable",
    [
        (status_error(anthropic.AuthenticationError, 401), "authentication", 502, False),
        (status_error(anthropic.PermissionDeniedError, 403), "permission_denied", 502, False),
        (status_error(anthropic.NotFoundError, 404), "not_found", 502, False),
        (status_error(anthropic.RateLimitError, 429), "rate_limited", 503, True),
        (status_error(anthropic.BadRequestError, 400), "api_error", 502, False),
        (status_error(anthropic.InternalServerError, 529), "api_error", 503, True),
        (anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")), "unreachable", 503, True),
    ],
    ids=["401", "403", "404", "429", "400", "529", "network"],
)
def test_sdk_errors_become_planner_errors_with_safe_messages(error, reason, status, retryable):
    provider = AnthropicProvider(client=FakeClient(error))
    with pytest.raises(PlannerFailedError) as raised:
        provider.generate_json(system="s", messages=MESSAGES, schema=SCHEMA)
    failure = raised.value
    assert (failure.reason, failure.status, failure.retryable, failure.code) == (reason, status, retryable, "planner_failed")
    assert "sk-ant-secret" not in failure.message and "sk-ant-secret" not in str(failure)
    assert failure.__cause__ is None


NO_CREDENTIALS = TypeError(
    '"Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set. '
    'Or for the `Authorization` header to be explicitly omitted"'
)


@pytest.mark.parametrize(
    "error",
    [NO_CREDENTIALS, getattr(anthropic, "CredentialsError", TypeError)("Could not resolve authentication method: config file not found at /home/me/.config")],
    ids=["no-key", "broken-profile"],
)
def test_missing_credentials_become_an_authentication_error(error):
    with pytest.raises(PlannerFailedError) as raised:
        AnthropicProvider(client=FakeClient(error)).generate_json(system="s", messages=MESSAGES, schema=SCHEMA)
    assert (raised.value.reason, raised.value.status, raised.value.retryable) == ("authentication", 502, False)
    assert "ANTHROPIC_API_KEY" in raised.value.message and "/home/me" not in raised.value.message


def test_unrelated_type_errors_pass_through():
    provider = AnthropicProvider(client=FakeClient(TypeError("unexpected keyword argument")))
    with pytest.raises(TypeError, match="unexpected keyword"):
        provider.generate_json(system="s", messages=MESSAGES, schema=SCHEMA)


def test_other_errors_pass_through():
    provider = AnthropicProvider(client=FakeClient(KeyError("unexpected")))
    with pytest.raises(KeyError):
        provider.generate_json(system="s", messages=MESSAGES, schema=SCHEMA)


def test_works_as_the_provider_of_a_planner(fleet):
    from datetime import datetime, timezone

    from noquery import build_catalog

    raw = {
        "kind": "query",
        "query": {"measures": ["trips.count"], "dimensions": [], "time": None, "filters": [{"field": "trips.distance_m", "op": "gt", "values": ["100"]}], "order_by": [], "limit": 5},
        "clarification": None,
        "reason": None,
    }
    client = FakeClient(response(text=json.dumps(raw)))
    planner = create_planner(anthropic_provider(client=client))
    catalog = build_catalog(fleet, AUTH_A)
    plan = planner.plan(catalog=catalog, question="Long trips?", timezone="UTC", now=datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert plan == {"kind": "query", "ir": {"version": 1, "measures": ["trips.count"], "filters": [{"field": "trips.distance_m", "op": "gt", "value": 100}], "limit": 5}}
    request = client.requests[0]
    assert request["messages"] == [{"role": "user", "content": "Today is Sunday, 2026-03-01 in UTC.\n\nQuestion: Long trips?"}]
    assert "trips.count" in request["system"][0]["text"]
    assert "trips.count" in request["output_config"]["format"]["schema"]["properties"]["query"]["anyOf"][0]["properties"]["measures"]["items"]["enum"]


@pytest.mark.skipif(
    __import__("os").environ.get("NOQUERY_LIVE_ANTHROPIC") != "1",
    reason="set NOQUERY_LIVE_ANTHROPIC=1 to call the real Claude API (billed to your Anthropic account)",
)
def test_live_planner_returns_a_valid_query(fleet):
    from noquery import Noquery

    from support import FakeDialect, FakeExecutor

    nq = Noquery(
        model=fleet,
        dialect=FakeDialect(),
        executor=FakeExecutor(rows=[{"devices.model": "X1", "trips.count": 3}]),
        planner=create_planner(anthropic_provider(effort="low")),
    )
    result = nq.ask("How many trips did each device model make last week?", AUTH_A, timezone="America/Toronto")
    assert result.type == "answer"
    assert result.ir["measures"] == ["trips.count"] and result.ir["dimensions"] == ["devices.model"]
    assert result.ir["time"]["range"] == "last_week"
