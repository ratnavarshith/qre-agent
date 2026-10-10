import json

import pytest
import yaml
from test_agent import ANSWER, BICYCLE, ESTIMATE, SURFACE, FakeClient, StubToolbox, call, reply

from qre_agent import bicycle, surface
from qre_agent import cache as estimate_cache
from qre_agent.agent import SWITCHED
from qre_agent.cache import EstimateCache
from qre_agent.routing import run_routed
from qre_agent.spend import Guard
from qre_agent.tools import Toolbox

CALL_PRIMARY = 840 / 1e6
CALL_STRONG = 8400 / 1e6  # the strong model's prices are 10x
BENCH = (
    reply("", call("build_benchmark", {"family": "qft", "n": 4})),
    ESTIMATE[1],
)  # a task that starts on a benchmark circuit
WRONG = ANSWER.replace("1,226", "1,227")  # a number no tool produced: verify fails


@pytest.fixture
def guard(tmp_path):
    budgets = tmp_path / "budgets.yaml"
    prices = {
        "fake": {"input": 1.0, "cache_read": 0.1, "output": 2.0},
        "strong": {"input": 10.0, "cache_read": 1.0, "output": 20.0},
    }
    budgets.write_text(yaml.safe_dump({"caps": {"dev": 1}, "prices": prices}))
    return Guard("dev", budgets, tmp_path / "spend.jsonl")


def route(guard, tmp_path, primary, strong, make_toolbox=StubToolbox, max_steps=12):
    return run_routed(
        "Compare a 4-qubit QFT",
        (primary, "fake"),
        (strong, "strong"),
        guard,
        "t1",
        make_toolbox,
        max_steps,
        4096,
        tmp_path,
    )


def trace(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_verified_answer_stays_on_the_primary(guard, tmp_path):
    strong = FakeClient(reply(ANSWER))
    r = route(guard, tmp_path, FakeClient(*BENCH, reply(ANSWER)), strong)
    assert r.run.run_id == "t1" and r.run.verification["passed"] and r.refused is None
    assert strong.sent == []  # never called
    assert r.routing | {"cost_usd": 0} == {
        "policy": "f",
        "primary_model": "fake",
        "strong_model": "strong",
        "escalated": False,
        "reason": None,
        "primary_run_id": "t1",
        "cost_before_switch_usd": pytest.approx(3 * CALL_PRIMARY),
        "cost_after_switch_usd": 0.0,
        "answered_by": "fake",
        "cost_usd": 0,
    }
    records = trace(r.run.trace_path)
    assert records[-1]["type"] == "routing" and records[-1]["answered_by"] == "fake"
    assert not (tmp_path / "t1-escalated.jsonl").exists()


def test_a_first_tool_call_of_build_circuit_switches_before_any_tool_runs(guard, tmp_path):
    primary = FakeClient(*ESTIMATE, reply(ANSWER))  # ESTIMATE starts with build_circuit
    toolboxes = []

    def make():
        toolboxes.append(StubToolbox())
        return toolboxes[-1]

    r = route(guard, tmp_path, primary, FakeClient(*BENCH, reply(ANSWER)), make)
    assert len(primary.sent) == 1  # one LLM call, then the switch
    assert toolboxes[0].outputs == []  # its build_circuit never ran
    assert r.toolbox is toolboxes[1] and r.run.run_id == "t1-escalated"
    assert r.run.verification["passed"] and r.run.steps == 1 + 3
    assert r.routing["escalated"] and r.routing["reason"] == "first_build"
    assert r.routing["answered_by"] == "strong" and r.routing["escalated_run_id"] == "t1-escalated"
    assert r.routing["cost_before_switch_usd"] == pytest.approx(CALL_PRIMARY)
    assert r.routing["cost_after_switch_usd"] == pytest.approx(3 * CALL_STRONG)
    assert r.run.totals["cost_usd"] == pytest.approx(CALL_PRIMARY + 3 * CALL_STRONG)
    assert r.run.totals["input_tokens"] == 4000

    first = trace(tmp_path / "t1.jsonl")
    assert first[-1]["type"] == "final" and first[-1]["stop_reason"] == SWITCHED
    assert [x["type"] for x in first].count("tool_call") == 0
    assert first[0]["routing"] == {"policy": "f", "role": "primary"}
    second = trace(tmp_path / "t1-escalated.jsonl")
    assert second[0]["routing"] == {
        "policy": "f",
        "role": "strong",
        "escalated_from": "t1",
        "reason": "first_build",
        "cost_before_switch_usd": pytest.approx(CALL_PRIMARY),
    }
    assert second[-1]["type"] == "routing" and second[-1]["reason"] == "first_build"
    assert "routing" not in {x["type"] for x in first}  # the abandoned leg has no routing record


def test_a_later_build_circuit_does_not_switch(guard, tmp_path):
    later = (BENCH[0], reply("", call("build_circuit", {"code": "qft"})), BENCH[1])
    r = route(guard, tmp_path, FakeClient(*later, reply(ANSWER)), FakeClient(reply(ANSWER)))
    assert not r.routing["escalated"] and r.run.run_id == "t1"


def test_a_final_answer_that_fails_verify_after_its_retry_escalates(guard, tmp_path):
    primary = FakeClient(*BENCH, reply(WRONG))  # the same wrong answer after the retry
    r = route(guard, tmp_path, primary, FakeClient(*BENCH, reply(ANSWER)))
    assert r.routing["escalated"] and r.routing["reason"] == "verify_failed"
    assert r.run.run_id == "t1-escalated" and r.run.verification["passed"]
    assert r.routing["cost_before_switch_usd"] == pytest.approx(4 * CALL_PRIMARY)
    first = trace(tmp_path / "t1.jsonl")
    assert [x["attempt"] for x in first if x["type"] == "auto_verify"] == [1, 2]


def test_an_answer_that_passes_verify_on_the_retry_is_not_escalated(guard, tmp_path):
    primary = FakeClient(*BENCH, reply(WRONG), reply(ANSWER))
    r = route(guard, tmp_path, primary, FakeClient(reply(ANSWER)))
    assert not r.routing["escalated"] and r.run.verify_passed_after_retry


def test_no_answer_escalates(guard, tmp_path):
    primary = FakeClient(BENCH[0])  # calls a tool forever
    r = route(guard, tmp_path, primary, FakeClient(*BENCH, reply(ANSWER)), max_steps=3)
    assert r.routing["escalated"] and r.routing["reason"] == "no_answer"
    assert r.run.verification["passed"]


def test_an_error_on_the_primary_escalates(guard, tmp_path):
    class Down:
        def complete(self, model, messages, max_tokens):
            raise RuntimeError("provider down")

    r = route(guard, tmp_path, Down(), FakeClient(*BENCH, reply(ANSWER)))
    assert r.routing["escalated"] and r.routing["reason"] == "primary_error"
    assert r.run.verification["passed"] and r.routing["cost_before_switch_usd"] == 0.0


def test_a_budget_refusal_ends_the_task_without_escalating(tmp_path):
    budgets = tmp_path / "budgets.yaml"
    prices = {"fake": {"input": 1.0, "output": 2.0}, "strong": {"input": 10.0, "output": 20.0}}
    budgets.write_text(yaml.safe_dump({"caps": {"dev": 1e-9}, "prices": prices}))
    strong = FakeClient(reply(ANSWER))
    r = route(
        Guard("dev", budgets, tmp_path / "spend.jsonl"), tmp_path, FakeClient(reply("x")), strong
    )
    assert "worst case" in r.refused and not r.routing["escalated"] and strong.sent == []


@pytest.fixture
def fake_estimators(monkeypatch):
    """Both estimators replaced by fakes that count their calls; no measurement table needed."""
    calls = []
    monkeypatch.setattr(surface, "estimate_surface", lambda c, a: calls.append("s") or SURFACE)
    monkeypatch.setattr(bicycle, "estimate_bicycle", lambda c, a: calls.append("b") or BICYCLE)
    monkeypatch.setattr(estimate_cache, "measurement_table", lambda a, code: code)
    monkeypatch.setattr(estimate_cache, "table_sha256", lambda path: "table")
    return calls


def test_cache_hits_are_recorded_in_the_trace(guard, tmp_path, fake_estimators):
    cache = EstimateCache(tmp_path / "cache")

    def make():
        return Toolbox(timeout=None, cache=cache)

    def go(run_id):
        client = FakeClient(*BENCH, reply(ANSWER))
        return run_routed("Compare a 4-qubit QFT", (client, "fake"), (client, "strong"), guard,
                          run_id, make, 12, 4096, tmp_path)  # fmt: skip

    first, second = go("t1"), go("t2")
    assert fake_estimators == ["s", "b"]  # the second run estimated nothing
    assert (first.routing["cache_hits"], first.routing["cache_misses"]) == (0, 2)
    assert (second.routing["cache_hits"], second.routing["cache_misses"]) == (2, 0)
    for run_id, hit in (("t1", False), ("t2", True)):
        records = trace(tmp_path / f"{run_id}.jsonl")
        estimates = [r for r in records if r["type"] == "tool_call" and r["name"].startswith("est")]
        assert [r["cache_hit"] for r in estimates] == [hit, hit]
        assert records[-2]["cache"] == {"hits": 2 * hit, "misses": 2 * (not hit)}
        built = [r for r in records if r["type"] == "tool_call" and r["name"] == "build_benchmark"]
        assert "cache_hit" not in built[0]
    assert first.run.verification == second.run.verification == {"passed": True, "failures": []}
