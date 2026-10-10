import json
from collections import Counter

import pytest
import yaml
from test_agent import ANSWER, ESTIMATE, StubToolbox, guard, reply, trace  # noqa: F401
from test_reliability import FakeGuard, Sleeps

from qre_agent.agent import run
from qre_agent.faults import KINDS, Injected, InjectedFault
from qre_agent.reliability import Reliable
from qre_agent.spend import Guard, read_log


class Counting:
    """A provider that counts the calls it actually receives, and replays `replies`."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0

    def complete(self, model, messages, max_tokens):
        self.calls += 1
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


@pytest.mark.parametrize("rate", [0.0, 0.1, 0.2, 0.3])
def test_injection_rate_hits_its_target(rate):
    inj, n = Injected(FakeGuard(), rate, seed=1), 20_000
    for _ in range(n):
        try:
            inj.complete(None, "fake", [], 1)
        except InjectedFault:
            pass
    injected = sum(inj.counts.values())
    assert inj.calls == n
    assert injected / n == pytest.approx(rate, abs=0.01)  # 3 sigma at 0.3 is 0.0097
    if rate:  # each kind gets about a quarter of the faults
        assert all(c / injected == pytest.approx(0.25, abs=0.03) for c in inj.counts.values())
        assert set(inj.counts) == set(KINDS)


def test_injection_is_reproducible_from_the_seed():
    def sequence(seed):
        inj = Injected(FakeGuard(), 0.3, seed)
        return [inj.draw() for _ in range(200)]

    assert sequence("0-task-0.3") == sequence("0-task-0.3") != sequence("1-task-0.3")


def test_injected_faults_never_reach_the_provider_and_cost_nothing(tmp_path):
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        yaml.safe_dump({"caps": {"dev": 1}, "prices": {"fake": {"input": 1.0, "output": 2.0}}})
    )
    spend = Guard("dev", budgets, tmp_path / "spend.jsonl")
    inj, provider = Injected(spend, 0.3, seed=2), Counting(reply("ok"))
    malformed = 0
    for _ in range(200):
        try:
            out = inj.complete(provider, "fake", [{"role": "user", "content": "hi"}], 10)
            malformed += out.faults == ("malformed_json",)
        except InjectedFault:
            pass
    injected = sum(inj.counts.values())
    assert injected > 0 and malformed == inj.counts["malformed_json"]
    assert provider.calls == 200 - injected
    log = read_log(spend.log_path)
    assert len(log) == provider.calls and all(r["status"] == "ok" for r in log)


def test_injected_errors_carry_the_kind_and_a_retry_after_for_429():
    inj = Injected(FakeGuard(), 1.0, seed=0, kinds=("rate_limit",))
    with pytest.raises(InjectedFault) as raised:
        inj.complete(None, "fake", [], 1)
    assert (raised.value.kind, raised.value.retry_after, raised.value.retryable) == (
        "rate_limit", 1.0, True,
    )  # fmt: skip
    with pytest.raises(ValueError, match="unknown fault kinds"):
        Injected(FakeGuard(), 0.1, 0, kinds=("meteor",))


ERRORS = ("timeout", "rate_limit", "server")


def go(wrapped, tmp_path, run_id):
    client = Counting(*ESTIMATE, reply(ANSWER))
    return run("Compare a 4-qubit QFT", client, wrapped, "fake", run_id, StubToolbox(),
               runs_dir=tmp_path), client  # fmt: skip


def first_fault(seed, rate):
    inj = Injected(FakeGuard(), rate, seed, ERRORS)
    return next(i for i in range(1000) if inj.draw())  # calls before the first fault


def longest_streak(seed, rate, n=40):
    inj, longest, streak = Injected(FakeGuard(), rate, seed, ERRORS), 0, 0
    for _ in range(n):
        streak = streak + 1 if inj.draw() else 0
        longest = max(longest, streak)
    return longest


def test_without_reliability_a_run_fails_on_the_first_injected_fault(guard, tmp_path):  # noqa: F811
    seed = next(s for s in range(100) if first_fault(s, 0.3) == 1)  # a fault on the second call
    inj = Injected(guard, 0.3, seed, ERRORS)
    with pytest.raises(InjectedFault):
        go(inj, tmp_path, "off")
    records = trace(tmp_path / "off.jsonl")
    assert sum(r["type"] == "llm_call" for r in records) == 1
    assert records[-1]["stop_reason"].startswith("error: InjectedFault")


def test_with_reliability_the_same_faults_are_retried_and_the_run_answers(guard, tmp_path):  # noqa: F811
    # a fault on the second call, and never four in a row (that would use up the 3 retries; with
    # no fallback configured here the run would then fail)
    seed = next(s for s in range(100) if first_fault(s, 0.3) == 1 and longest_streak(s, 0.3) < 4)
    inj = Injected(guard, 0.3, seed, ERRORS)
    result, client = go(Reliable(inj, sleep=Sleeps()), tmp_path, "on")
    assert result.verification["passed"] and client.calls == 3
    calls = [r for r in trace(result.trace_path) if r["type"] == "llm_call"]
    assert sum(c["retries"] for c in calls) == sum(inj.counts.values()) > 0
    assert [f for c in calls for f in c["faults"]] and not any(c["fallback"] for c in calls)
    spans = (tmp_path / "otel" / "on.jsonl").read_text().splitlines()
    events = [e["name"] for s in map(json.loads, spans) for e in s["events"]]
    assert Counter(events)["fault_injected"] == sum(inj.counts.values())


def test_a_malformed_tool_call_goes_back_to_the_agent_as_a_tool_error(guard, tmp_path):  # noqa: F811
    inj = Injected(guard, 0.0, seed=0)
    draws = iter(["malformed_json"])  # on the first call only
    inj.draw = lambda: next(draws, None)
    result, client = go(inj, tmp_path, "mal")
    assert result.verification["passed"] and client.calls == 3 and result.steps == 4
    tool = next(r for r in trace(result.trace_path) if r["type"] == "tool_call")
    assert tool["result"]["error"].startswith("arguments are not valid JSON")
    first = next(r for r in trace(result.trace_path) if r["type"] == "llm_call")
    assert (first["cost_usd"], first["faults"]) == (0.0, ["malformed_json"])
