import json

import pytest

from qre_agent.llm import OpenRouter, parse
from qre_agent.tools import SCHEMAS

KEY = "sk-or-v1-test-0123456789abcdef"
COMPLETION = {
    "choices": [
        {
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "build_circuit", "arguments": '{"code": "x"}'},
                    }
                ],
            },
        }
    ],
    "usage": {
        "prompt_tokens": 1200,
        "completion_tokens": 300,
        "prompt_tokens_details": {"cached_tokens": 1000},
        "completion_tokens_details": {"reasoning_tokens": 250},
        "cost": 0.00123,
    },
}


def test_parse_normalizes_tokens_cost_and_the_message():
    r = parse(COMPLETION)
    assert (r.input_tokens, r.cached_tokens, r.output_tokens, r.reasoning_tokens) == (
        1200,
        1000,
        300,
        250,
    )
    assert r.reported_cost_usd == 0.00123 and r.finish_reason == "tool_calls"
    assert r.text == "" and r.tool_calls[0]["function"]["name"] == "build_circuit"
    assert r.message == {"role": "assistant", "content": "", "tool_calls": r.tool_calls}


def test_parse_without_cache_or_cost_details():
    data = {
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }
    r = parse(data)
    assert (r.cached_tokens, r.reported_cost_usd, r.tool_calls) == (0, None, [])


def test_parse_raises_on_an_error_body():
    with pytest.raises(RuntimeError, match="rate limited"):
        parse({"error": {"message": "rate limited"}})


def test_body_asks_for_usage_and_carries_tools_and_seed_but_not_the_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setattr("qre_agent.spend.load_keys", lambda: None)  # keep .env out of the tests
    client = OpenRouter(SCHEMAS, seed=7)
    body = client.body("m", [{"role": "user", "content": "hi"}], 100)
    assert body["usage"] == {"include": True} and body["seed"] == 7 and body["max_tokens"] == 100
    assert [t["function"]["name"] for t in body["tools"]] == [s["name"] for s in SCHEMAS]
    assert KEY not in json.dumps(body) and KEY not in repr(client)


def test_missing_key_is_reported_by_name_only(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setattr("qre_agent.spend.load_keys", lambda: None)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is missing or malformed"):
        OpenRouter()
