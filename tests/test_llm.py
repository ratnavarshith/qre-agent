import json

import pytest

from qre_agent.llm import OpenRouter, Paced, Pacer, parse
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


def test_cache_marks_system_prompt_and_last_tool_for_anthropic_only(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setattr("qre_agent.spend.load_keys", lambda: None)
    client = OpenRouter(SCHEMAS, cache=True)
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    mark = {"type": "ephemeral"}

    body = client.body("anthropic/claude-haiku-4.5", messages, 100)
    assert body["messages"][0]["content"] == [
        {"type": "text", "text": "rules", "cache_control": mark}
    ]
    assert body["messages"][1] == messages[1]
    assert [bool(t.get("cache_control")) for t in body["tools"]] == [False] * (len(SCHEMAS) - 1) + [
        True
    ]
    assert (
        messages[0]["content"] == "rules" and "cache_control" not in client.tools[-1]
    )  # untouched

    for model in ("google/gemini-2.5-flash", "deepseek/deepseek-v3.2"):
        other = client.body(model, messages, 100)
        assert other["messages"] == messages and other["tools"] == client.tools


def test_cache_is_off_by_default(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setattr("qre_agent.spend.load_keys", lambda: None)
    messages = [{"role": "system", "content": "rules"}]
    assert (
        OpenRouter(SCHEMAS).body("anthropic/claude-haiku-4.5", messages, 100)["messages"]
        == messages
    )


def test_parse_reads_cache_write_tokens():
    data = {
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 2893,
            "completion_tokens": 125,
            "prompt_tokens_details": {"cached_tokens": 1830, "cache_write_tokens": 964},
        },
    }
    r = parse(data)
    assert (r.input_tokens, r.cached_tokens, r.cache_write_tokens) == (2893, 1830, 964)
    assert parse(COMPLETION).cache_write_tokens == 0


class Clock:
    """A fake clock whose sleep advances it."""

    def __init__(self):
        self.now = 100.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_pacer_spaces_calls_at_least_a_minute_over_max_rpm_apart():
    clock = Clock()
    pacer = Pacer(15, clock.time, clock.sleep)  # 4 s apart
    assert pacer.wait() == 0  # the first call goes at once
    assert pacer.wait() == pytest.approx(4)  # an immediate second call waits the full gap
    clock.now += 1.5  # the call itself took 1.5 s
    assert pacer.wait() == pytest.approx(2.5)
    clock.now += 10  # a long call: no wait
    assert pacer.wait() == 0
    assert pacer.waited == pytest.approx(6.5)


def test_paced_client_waits_then_delegates_and_exposes_the_clients_attributes():
    class Inner:
        tools, seed = ["t"], 3

        def complete(self, model, messages, max_tokens):
            return ("reply", model, messages, max_tokens)

    clock = Clock()
    pacer = Pacer(30, clock.time, clock.sleep)
    client = Paced(Inner(), pacer)
    assert (client.tools, client.seed, client.pacer) == (["t"], 3, pacer)
    assert client.complete("m", [1], 5) == ("reply", "m", [1], 5)
    client.complete("m", [1], 5)
    assert pacer.waited == pytest.approx(2)  # 30 rpm: 2 s apart
