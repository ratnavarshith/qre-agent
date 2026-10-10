import io
import json
import random
import time
import urllib.error
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
import yaml
from qiskit import QuantumCircuit
from test_agent import ANSWER, ESTIMATE, FakeClient, StubToolbox, reply, trace

from qre_agent.agent import run
from qre_agent.claude import Claude, to_anthropic
from qre_agent.claude import parse as parse_claude
from qre_agent.llm import OpenRouter, ProviderError, parse, retry_after
from qre_agent.reliability import Reliable, backoff, build
from qre_agent.spend import Guard, read_log
from qre_agent.tools import SCHEMAS, Toolbox, ToolTimeout, run_in_child


class FakeGuard:
    """Raises each error in `errors` in turn, then returns `ok`; counts calls per model."""

    def __init__(self, *errors, ok=None, phase="dev"):
        self.errors, self.ok, self.phase = list(errors), ok, phase
        self.prices, self.calls = {"fake": {}, "fake2": {}}, []

    def complete(self, client, model, messages, max_tokens):
        self.calls.append(model)
        if self.errors:
            raise self.errors.pop(0)
        return self.ok or reply("done")


def err(kind, retry_after=None):
    return ProviderError(kind, kind, retry_after=retry_after)


class Sleeps(list):
    def __call__(self, seconds):
        self.append(seconds)


def test_backoff_is_full_jitter_and_reproducible_with_a_seed():
    sleeps = Sleeps()
    r = Reliable(FakeGuard(*[err("server")] * 3), seed=5, sleep=sleeps)
    assert r.complete(None, "fake", [], 10).retries == 3
    rng = random.Random(5)  # what the waits must be: uniform(0, min(30, 2^k)), k = 0, 1, 2
    assert sleeps == [rng.uniform(0, 1), rng.uniform(0, 2), rng.uniform(0, 4)]
    again = Sleeps()
    Reliable(FakeGuard(*[err("server")] * 3), seed=5, sleep=again).complete(None, "fake", [], 10)
    assert again == sleeps
    assert all(0 <= backoff(k, random.Random(k)) <= min(30, 2**k) for k in range(10))
    assert backoff(20, random.Random(0)) <= 30  # capped


def test_retry_after_is_waited_when_longer_than_the_backoff():
    sleeps = Sleeps()
    r = Reliable(FakeGuard(err("rate_limit", 7.0), err("rate_limit", 0.01)), sleep=sleeps)
    out = r.complete(None, "fake", [], 10)
    assert sleeps[0] == 7.0  # the backoff at attempt 0 is at most 1 s
    assert 0.01 <= sleeps[1] <= 2  # a short Retry-After doesn't shorten the backoff
    assert (out.retries, out.faults) == (2, ("rate_limit", "rate_limit"))


def test_retry_after_header_in_seconds_or_as_a_date():
    assert retry_after("12") == 12.0 and retry_after(None) is None and retry_after("soon") is None
    assert retry_after("Thu, 01 Jan 1970 00:01:40 GMT", now=40.0) == pytest.approx(60.0)


def test_fallback_only_after_the_retries_are_used_up():
    primary = FakeGuard(*[err("timeout")] * 4)
    fallback = FakeGuard(ok=reply("from the fallback"))
    r = Reliable(primary, (fallback, None, "fake2"), sleep=Sleeps())
    out = r.complete(None, "fake", [], 10)
    assert primary.calls == ["fake"] * 4 and fallback.calls == ["fake2"]  # 1 try + 3 retries
    assert (out.text, out.model, out.fallback, out.retries) == (
        "from the fallback",
        "fake2",
        True,
        3,
    )

    primary = FakeGuard(*[err("timeout")] * 3)  # succeeds on the last retry
    fallback = FakeGuard()
    out = Reliable(primary, (fallback, None, "fake2"), sleep=Sleeps()).complete(None, "fake", [], 1)
    assert (out.model, out.fallback, fallback.calls) == ("fake", False, [])


def test_other_errors_are_not_retried_and_do_not_fall_back():
    fallback = FakeGuard()
    for error in (err("client"), err("connection"), RuntimeError("bug")):
        primary = FakeGuard(error)
        with pytest.raises(type(error)):
            Reliable(primary, (fallback, None, "fake2"), sleep=Sleeps()).complete(
                None, "fake", [], 1
            )
        assert primary.calls == ["fake"]
    assert fallback.calls == []


def test_when_everything_fails_the_last_error_says_what_was_tried():
    primary, fallback = FakeGuard(*[err("server")] * 4), FakeGuard(*[err("rate_limit")] * 4)
    with pytest.raises(ProviderError) as raised:
        Reliable(primary, (fallback, None, "fake2"), sleep=Sleeps()).complete(None, "fake", [], 1)
    e = raised.value
    assert (e.kind, e.fallback, e.retries) == ("rate_limit", True, 6)
    assert e.faults == ("server",) * 4 + ("rate_limit",) * 4


def test_build_is_switched_by_the_config():
    guard = FakeGuard()
    assert build(guard, None) is guard and build(guard, {"enabled": False}) is guard
    r = build(guard, {"enabled": True, "max_retries": 2})
    assert isinstance(r, Reliable) and r.max_retries == 2 and r.phase == "dev"


@pytest.fixture
def guards(tmp_path):
    budgets = tmp_path / "budgets.yaml"
    prices = {"fake": {"input": 1.0, "output": 2.0}, "fake2": {"input": 10.0, "output": 20.0}}
    budgets.write_text(yaml.safe_dump({"caps": {"dev": 1, "phase3": 1}, "prices": prices}))
    return (Guard("dev", budgets, tmp_path / "spend.jsonl"),
            Guard("phase3", budgets, tmp_path / "spend.jsonl"))  # fmt: skip


class Down:
    """A client whose every call fails like a provider outage."""

    def __init__(self, error):
        self.error, self.calls = error, 0

    def complete(self, model, messages, max_tokens):
        self.calls += 1
        raise self.error


def test_failed_attempts_still_count_toward_spend(guards):
    guard, _ = guards
    with pytest.raises(ProviderError):
        Reliable(guard, sleep=Sleeps()).complete(Down(err("server")), "fake", [], 100)
    log = read_log(guard.log_path)
    assert [r["status"] for r in log] == ["failed"] * 4 and all(r["cost_usd"] > 0 for r in log)


def test_a_run_finishes_on_the_fallback_priced_at_its_own_rate(guards, tmp_path):
    guard, phase3 = guards
    fallback = (phase3, FakeClient(*ESTIMATE, reply(ANSWER)), "fake2")
    r = Reliable(guard, fallback, sleep=Sleeps())
    result = run("Compare a 4-qubit QFT", Down(err("server")), r, "fake", "t1", StubToolbox(),
                 runs_dir=tmp_path)  # fmt: skip
    assert result.verification["passed"]
    calls = [x for x in trace(result.trace_path) if x["type"] == "llm_call"]
    assert all((c["model"], c["fallback"], c["retries"]) == ("fake2", True, 3) for c in calls)
    # FakeClient replies 1000 in (400 cached, no cache price: full input), 100 out at fake2 prices
    assert result.totals["cost_usd"] == pytest.approx(3 * (1000 * 10 + 100 * 20) / 1e6)
    spans = [json.loads(line) for line in (tmp_path / "otel" / "t1.jsonl").read_text().splitlines()]
    llm = [s for s in spans if s["name"] == "llm_call"]
    assert llm[0]["attributes"]["fallback"] and llm[0]["attributes"]["retries"] == 3
    assert [e["name"] for e in llm[0]["events"]] == ["llm_retry"] * 4
    # each step: four failed attempts on the primary, logged at their worst case, then the fallback
    log = [(x["phase"], x["status"]) for x in read_log(guard.log_path)]
    assert log == ([("dev", "failed")] * 4 + [("phase3", "ok")]) * 3


# ---- providers: errors classified for the retry layer ----


def test_openrouter_errors_are_classified(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test-0123456789abcdef")
    monkeypatch.setattr("qre_agent.spend.load_keys", lambda: None)
    client = OpenRouter(SCHEMAS)

    def raising(error):
        def urlopen(request, timeout):
            raise error

        return urlopen

    cases = [
        (urllib.error.HTTPError("u", 429, "", {"Retry-After": "5"}, io.BytesIO(b"slow")),
         "rate_limit", 5.0),
        (urllib.error.HTTPError("u", 502, "", {}, io.BytesIO(b"bad gateway")), "server", None),
        (urllib.error.HTTPError("u", 400, "", {}, io.BytesIO(b"bad")), "client", None),
        (TimeoutError("read timed out"), "timeout", None),
        (urllib.error.URLError(TimeoutError()), "timeout", None),
        (urllib.error.URLError("no route"), "connection", None),
    ]  # fmt: skip
    for error, kind, after in cases:
        monkeypatch.setattr("urllib.request.urlopen", raising(error))
        with pytest.raises(ProviderError) as raised:
            client.complete("m", [{"role": "user", "content": "hi"}], 10)
        assert (raised.value.kind, raised.value.retry_after) == (kind, after)


def test_openrouter_missing_usage_and_error_bodies_are_retryable():
    no_usage = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    with pytest.raises(ProviderError) as raised:
        parse(no_usage)
    assert raised.value.kind == "missing_usage" and raised.value.retryable
    with pytest.raises(ProviderError) as raised:
        parse({"error": {"code": 429, "message": "rate limited"}})
    assert raised.value.kind == "rate_limit"


REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


class FakeSDK:
    def __init__(self, result):
        self.result, self.sent = result, []
        self.messages = self

    def create(self, **kwargs):
        self.sent.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def message(*content, usage=True):
    u = SimpleNamespace(input_tokens=200, output_tokens=50, cache_read_input_tokens=1000,
                        cache_creation_input_tokens=None)  # fmt: skip
    return SimpleNamespace(
        content=list(content), usage=u if usage else None, stop_reason="tool_use"
    )


def test_claude_errors_are_classified():
    cases = [
        (anthropic.RateLimitError("slow", response=httpx2.Response(
            429, headers={"retry-after": "3"}, request=REQUEST), body=None), "rate_limit", 3.0),
        (anthropic.InternalServerError("overloaded", response=httpx2.Response(
            529, request=REQUEST), body=None), "server", None),
        (anthropic.BadRequestError("bad", response=httpx2.Response(400, request=REQUEST),
                                   body=None), "client", None),
        (anthropic.APITimeoutError(request=REQUEST), "timeout", None),
        (anthropic.APIConnectionError(request=REQUEST), "connection", None),
    ]  # fmt: skip
    for error, kind, after in cases:
        with pytest.raises(ProviderError) as raised:
            Claude(SCHEMAS, sdk=FakeSDK(error)).complete("claude-haiku-4-5", [], 10)
        assert (raised.value.kind, raised.value.retry_after) == (kind, after)
    with pytest.raises(ProviderError, match="no usage"):
        parse_claude(message(usage=False))


def test_claude_takes_over_an_openai_style_conversation():
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "reasoning_details": [{"x": 1}], "tool_calls": [
            {"id": "tool_0_build:circuit", "type": "function",
             "function": {"name": "build_circuit", "arguments": '{"code": "x"}'}},
            {"id": "call_2", "type": "function",
             "function": {"name": "estimate_surface", "arguments": "{not json"}}]},
        {"role": "tool", "tool_call_id": "tool_0_build:circuit", "content": '{"circuit_id": "c1"}'},
        {"role": "tool", "tool_call_id": "call_2", "content": '{"error": "bad"}'},
        {"role": "user", "content": "Your final answer could not be checked"},
    ]  # fmt: skip
    system, turns = to_anthropic(messages)
    assert system == "rules" and [t["role"] for t in turns] == ["user", "assistant", "user"]
    uses = turns[1]["content"]
    assert [b["type"] for b in uses] == ["tool_use", "tool_use"]  # the empty text is dropped
    assert uses[0] == {"type": "tool_use", "id": "tool_0_build_circuit", "name": "build_circuit",
                       "input": {"code": "x"}}  # fmt: skip
    assert uses[1]["input"] == {}  # invalid JSON arguments
    results = turns[2]["content"]
    assert [b.get("tool_use_id") for b in results] == ["tool_0_build_circuit", "call_2", None]
    assert results[2] == {"type": "text", "text": "Your final answer could not be checked"}

    sdk = FakeSDK(message(SimpleNamespace(type="text", text="ok"),
                          SimpleNamespace(type="tool_use", id="toolu_1", name="verify",
                                          input={"a": 1})))  # fmt: skip
    out = Claude(SCHEMAS, sdk=sdk).complete("claude-haiku-4-5", messages, 100)
    sent = sdk.sent[0]
    assert sent["model"] == "claude-haiku-4-5" and sent["system"] == "rules"
    assert [t["name"] for t in sent["tools"]] == [s["name"] for s in SCHEMAS]
    assert sent["tools"][0]["input_schema"] == SCHEMAS[0]["parameters"]
    assert (out.input_tokens, out.cached_tokens, out.output_tokens) == (1200, 1000, 50)
    assert out.tool_calls == [{"id": "toolu_1", "type": "function",
                               "function": {"name": "verify", "arguments": '{"a": 1}'}}]  # fmt: skip


# ---- tools ----


def test_a_child_is_killed_at_the_timeout():
    start = time.perf_counter()
    with pytest.raises(ToolTimeout, match="sleep timed out after 1 s"):
        run_in_child(time.sleep, 60, timeout=1)
    assert time.perf_counter() - start < 10


def test_a_child_returns_its_value_or_raises_its_error():
    assert run_in_child(pow, 2, 10, timeout=60) == 1024
    with pytest.raises(ValueError, match="invalid literal"):
        run_in_child(int, "x", timeout=60)


def test_an_estimate_that_times_out_is_an_error_and_stores_nothing():
    tb = Toolbox(timeout=0.01)  # too short even to start the child
    tb.circuits["c1"] = QuantumCircuit(2)
    out = json.loads(tb.call("estimate_surface", {"circuit_id": "c1"}))
    assert out == {"error": "ToolTimeout: estimate_surface timed out after 0.01 s"}
    assert tb.results == {}
