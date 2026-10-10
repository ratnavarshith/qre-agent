import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from qre_agent.llm import ChatReply, Pacer
from qre_agent.spend import Guard

SCRIPT = Path(__file__).parents[1] / "scripts" / "run_eval.py"
TASK = "std-qft4-1e4-gross"  # QFT 4, p = 1e-4, gross: estimates in about a second


@pytest.fixture
def run_eval(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("run_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "EVAL_DIR", tmp_path / "eval")
    monkeypatch.setattr(module, "RUNS_DIR", tmp_path / "runs")
    return module


def call(name, arguments, i):
    return {"id": f"c{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}  # fmt: skip


class Scripted:
    """Builds the benchmark, estimates it, answers. The first `fail` calls (over all instances)
    raise like a provider outage."""

    failures = 0

    def __init__(self, schemas=(), seed=None, cache=False):
        self.seed = seed

    def complete(self, model, messages, max_tokens):
        if Scripted.failures:
            Scripted.failures -= 1
            raise RuntimeError("OpenRouter HTTP 502: bad gateway")
        steps = sum(m["role"] == "assistant" for m in messages)
        if steps == 0:
            calls = [call("build_benchmark", {"family": "qft", "n": 4}, 1)]
        elif steps == 1:
            calls = [
                call("estimate_surface", {"circuit_id": "c1", "physical_error_rate": 1e-4}, 2),
                call("estimate_bicycle",
                     {"circuit_id": "c1", "code": "gross", "physical_error_rate": "1e-4"}, 3),
            ]  # fmt: skip
        else:
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"][-2:]
            keys = ("result_id", "architecture", "physical_qubits", "runtime_ns",
                    "physical_qubit_seconds")  # fmt: skip
            answer = {"summary": "Assuming p = 1e-4, both estimates pass the budget.",
                      "estimates": [{k: r[k] for k in keys} for r in results]}  # fmt: skip
            text = json.dumps(answer)
            return ChatReply(text, 100, 10, message={"role": "assistant", "content": text})
        message = {"role": "assistant", "content": "", "tool_calls": calls}
        return ChatReply("", 100, 10, message=message)


def test_api_errors_are_recorded_then_rerun_once(run_eval, tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "OpenRouter", Scripted)
    Scripted.failures = 1  # the first run's first call fails
    budgets = tmp_path / "budgets.yaml"
    prices = {"fake": {"input": 1.0, "output": 2.0}}
    budgets.write_text(yaml.safe_dump({"caps": {"eval": 1}, "prices": prices}))
    guard = Guard("eval", budgets, tmp_path / "spend.jsonl")
    cfg = {"model": "fake", "phase": "eval", "tasks": "evals/tasks.yaml", "runs": 2, "seed": 5,
           "max_steps": 6, "max_tokens": 100,
           "stop": {"cost_factor": 100, "api_error_rate": 1.0, "min_runs": 10}}  # fmt: skip
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(cfg))
    suite = run_eval.load_suite(run_eval.REPO_ROOT / cfg["tasks"])
    tasks = [t for t in suite["tasks"] if t["id"] == TASK]

    assert run_eval.run_eval(config, cfg, tasks, suite, guard, expected=1.0) is None
    (out,) = (tmp_path / "eval" / "fake").iterdir()
    records = [json.loads(line) for line in (out / "runs.jsonl").read_text("utf-8").splitlines()]
    assert [r["category"] for r in records] == ["api error", None]
    assert [r["seed"] for r in records] == [5, 6]
    assert "# run at git commit" in (out / "config.yaml").read_text("utf-8")
    assert "1 of 2 runs on the first attempt, 1 after" in (out / "summary.md").read_text("utf-8")

    assert run_eval.rerun_errors(out, guard) is None
    after = [json.loads(line) for line in (out / "runs.jsonl").read_text("utf-8").splitlines()]
    assert [r["category"] for r in after] == [None, None]
    assert after[0]["rerun_of"]["run_id"] == records[0]["run_id"]
    assert after[0]["run_id"].endswith("-r0-rerun") and after[0]["seed"] == 5  # same seed
    assert after[1] == records[1] and "rerun_of" not in after[1]
    first = (out / "runs-first-attempt.jsonl").read_text("utf-8").splitlines()
    assert [json.loads(line)["category"] for line in first] == ["api error", None]
    summary = (out / "summary.md").read_text("utf-8")
    assert "1 of 2 runs on the first attempt, 0 after rerunning" in summary
    (event,) = json.loads((out / "meta.json").read_text("utf-8"))["reruns"]
    assert (event["reason"], event["runs"]) == ("api error", [[TASK, 0]])

    assert run_eval.rerun_errors(out, guard) is None  # nothing left to rerun
    assert (out / "runs.jsonl").read_text("utf-8").count("\n") == 2


def test_a_rerun_that_fails_again_stays_an_api_error(run_eval, tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "OpenRouter", Scripted)
    Scripted.failures = 2
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        yaml.safe_dump({"caps": {"eval": 1}, "prices": {"fake": {"input": 1.0, "output": 2.0}}})
    )
    guard = Guard("eval", budgets, tmp_path / "spend.jsonl")
    cfg = {"model": "fake", "phase": "eval", "tasks": "evals/tasks.yaml", "runs": 1, "seed": 0,
           "max_steps": 6, "max_tokens": 100,
           "stop": {"cost_factor": 100, "api_error_rate": 1.0, "min_runs": 10}}  # fmt: skip
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(cfg))
    suite = run_eval.load_suite(run_eval.REPO_ROOT / cfg["tasks"])
    tasks = [t for t in suite["tasks"] if t["id"] == TASK]
    run_eval.run_eval(config, cfg, tasks, suite, guard, expected=1.0)
    (out,) = (tmp_path / "eval" / "fake").iterdir()
    run_eval.rerun_errors(out, guard)
    (record,) = [json.loads(line) for line in (out / "runs.jsonl").read_text("utf-8").splitlines()]
    assert record["category"] == "api error" and "rerun_of" in record
    assert "1 of 1 runs on the first attempt, 1 after" in (out / "summary.md").read_text("utf-8")


def test_max_rpm_paces_calls_and_keeps_the_wait_out_of_the_latency(run_eval, tmp_path, monkeypatch):
    class Clock:  # only the pacer's sleeps move it, so each call after the first waits 2 s
        now = 0.0

    monkeypatch.setattr(run_eval, "OpenRouter", Scripted)
    monkeypatch.setattr(
        run_eval,
        "Pacer",
        lambda rpm: Pacer(rpm, lambda: Clock.now, lambda s: setattr(Clock, "now", Clock.now + s)),
    )
    Scripted.failures = 0
    budgets = tmp_path / "budgets.yaml"
    budgets.write_text(
        yaml.safe_dump({"caps": {"eval": 1}, "prices": {"fake": {"input": 1.0, "output": 2.0}}})
    )
    guard = Guard("eval", budgets, tmp_path / "spend.jsonl")
    cfg = {"model": "fake", "phase": "eval", "tasks": "evals/tasks.yaml", "runs": 2, "seed": 0,
           "max_steps": 6, "max_tokens": 100, "max_rpm": 30,
           "stop": {"cost_factor": 100, "api_error_rate": 1.0, "min_runs": 10}}  # fmt: skip
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(cfg))
    suite = run_eval.load_suite(run_eval.REPO_ROOT / cfg["tasks"])
    tasks = [t for t in suite["tasks"] if t["id"] == TASK]
    run_eval.run_eval(config, cfg, tasks, suite, guard, expected=1.0)
    (out,) = (tmp_path / "eval" / "fake").iterdir()
    first, second = [
        json.loads(line) for line in (out / "runs.jsonl").read_text("utf-8").splitlines()
    ]
    assert first["paced_wait_s"] == 4.0  # 3 calls: the first goes at once, two wait 2 s
    assert second["paced_wait_s"] == 6.0  # the pacer is shared: this run's first call waits too
    assert first["category"] is None and second["category"] is None


def test_selected_runs_are_rerun_with_the_reason_recorded(run_eval, tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "OpenRouter", Scripted)
    Scripted.failures = 0
    budgets = tmp_path / "budgets.yaml"
    prices = {"fake": {"input": 1.0, "output": 2.0}}
    budgets.write_text(yaml.safe_dump({"caps": {"eval": 1}, "prices": prices}))
    guard = Guard("eval", budgets, tmp_path / "spend.jsonl")
    cfg = {"model": "fake", "phase": "eval", "tasks": "evals/tasks.yaml", "runs": 2, "seed": 0,
           "max_steps": 6, "max_tokens": 100,
           "stop": {"cost_factor": 100, "api_error_rate": 1.0, "min_runs": 10}}  # fmt: skip
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(cfg))
    suite = run_eval.load_suite(run_eval.REPO_ROOT / cfg["tasks"])
    tasks = [t for t in suite["tasks"] if t["id"] == TASK]
    run_eval.run_eval(config, cfg, tasks, suite, guard, expected=1.0)
    (out,) = (tmp_path / "eval" / "fake").iterdir()
    before = [json.loads(line) for line in (out / "runs.jsonl").read_text("utf-8").splitlines()]

    assert run_eval.rerun(out, guard, {(TASK, 1)}, "harness bug") is None
    after = [json.loads(line) for line in (out / "runs.jsonl").read_text("utf-8").splitlines()]
    assert after[0] == before[0]
    assert after[1]["rerun_of"] == {"run_id": before[1]["run_id"], "stop_reason": "answered",
                                    "category": None, "reason": "harness bug"}  # fmt: skip
    assert after[1]["seed"] == 1 and after[1]["run_id"].endswith("-r1-rerun")
    meta = json.loads((out / "meta.json").read_text("utf-8"))
    assert meta["reruns"][-1]["reason"] == "harness bug"
    assert meta["reruns"][-1]["runs"] == [[TASK, 1]]
    assert "0 of 2 runs on the first attempt, 0 after" in (out / "summary.md").read_text("utf-8")
    with pytest.raises(SystemExit, match="not in this eval"):
        run_eval.rerun(out, guard, {("no-such-task", 0)}, "x")
