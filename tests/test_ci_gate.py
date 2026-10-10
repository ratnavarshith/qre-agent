import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest
import yaml
from test_run_eval import TASK, Scripted

from qre_agent.cache import EstimateCache
from qre_agent.spend import Guard

SCRIPT = Path(__file__).parents[1] / "scripts" / "ci_gate.py"
KEY = "sk-or-test-0123456789abcdef"
CFG = {"model": "m", "seed": 0, "run_cap": 0.2, "fail_fast": 3, "rerun_repeats": [1, 2]}
TASKS = [{"id": f"t{i}"} for i in range(10)]
CREDIT_402 = (
    'error: ProviderError: OpenRouter HTTP 402: {"error":{"code":402,"message":"Insufficient '
    'credits","metadata":{"limit_source":"openrouter_key_limit"}}}'
)
KEY_LIMIT_403 = (
    'error: ProviderError: OpenRouter HTTP 403: {"error":{"code":403,"message":"Budget limit '
    'exceeded","metadata":{"limit_source":"openrouter_key_limit"}}}'
)
IN_FLIGHT_402 = (
    'error: ProviderError: OpenRouter HTTP 402: {"error":{"code":402,"metadata":'
    '{"limit_source":"openrouter_in_flight_budget"}}}'
)


@pytest.fixture
def ci_gate():
    spec = importlib.util.spec_from_file_location("ci_gate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rec(task, repeat, correct=True, stop_reason="answered"):
    return {"task": task, "repeat": repeat, "correct": correct, "stop_reason": stop_reason,
            "category": None if correct else "wrong circuit", "cost_usd": 0.004,
            "failures": [] if correct else [{"category": "wrong circuit", "reason": "oracle"}]}  # fmt: skip


def scripted(outcomes, refuse=()):
    """run_task for run_gate: outcomes[(task, repeat)] is False or a stop_reason for a failure;
    runs not listed are correct."""

    calls = []

    def run_task(task, repeat):
        calls.append((task["id"], repeat))
        out = outcomes.get((task["id"], repeat), True)
        refused = "the budget guard refused a call" if (task["id"], repeat) in refuse else None
        if out is True:
            return rec(task["id"], repeat), refused
        return rec(task["id"], repeat, False, out or "answered"), refused

    run_task.calls = calls
    return run_task


def verdict(ci_gate, outcomes):
    run_task = scripted(outcomes)
    records = ci_gate.run_gate(CFG, TASKS, run_task)
    return ci_gate.decide(records, TASKS, CFG), run_task.calls


def test_all_tasks_passing_is_one_pass_of_ten_runs(ci_gate):
    (passed, _), calls = verdict(ci_gate, {})
    assert passed and len(calls) == 10


def test_a_first_pass_failure_is_rerun_twice_and_passes_if_both_reruns_pass(ci_gate):
    (passed, why), calls = verdict(ci_gate, {("t3", 0): False})
    assert passed and "1 first-pass failures" in why
    assert calls[10:] == [("t3", 1), ("t3", 2)]


def test_a_failed_rerun_fails_the_gate_and_stops_spending(ci_gate):
    (passed, why), calls = verdict(ci_gate, {("t3", 0): False, ("t3", 2): False, ("t5", 0): False})
    assert not passed and "failed again on a rerun: t3" in why
    assert calls[10:] == [("t3", 1), ("t3", 2)]  # t5 is not rerun once the gate has failed


def test_three_first_pass_failures_fail_at_once_without_reruns(ci_gate):
    bad = {("t1", 0): False, ("t4", 0): False, ("t8", 0): False}
    (passed, why), calls = verdict(ci_gate, bad)
    assert not passed and "3 of 10 tasks failed on the first pass" in why
    assert len(calls) == 10


def test_an_api_error_counts_as_a_failed_run(ci_gate):
    transient = "error: ProviderError: OpenRouter HTTP 502: bad gateway"
    (passed, _), calls = verdict(ci_gate, {("t2", 0): transient, ("t2", 1): transient})
    assert not passed and calls[10:] == [("t2", 1)]


@pytest.mark.parametrize(
    "stop_reason, message",
    [(CREDIT_402, "out of credit"), (KEY_LIMIT_403, "out of credit"),
     ("error: ProviderError: OpenRouter HTTP 401: invalid key", "rejected")],
)  # fmt: skip
def test_a_key_that_is_out_of_credit_or_rejected_stops_the_gate_with_exit_3(
    ci_gate, stop_reason, message
):
    run_task = scripted({("t4", 0): stop_reason})
    with pytest.raises(ci_gate.GateExit, match=message) as e:
        ci_gate.run_gate(CFG, TASKS, run_task)
    assert e.value.code == 3
    assert run_task.calls[-1] == ("t4", 0)  # nothing is run after it


def test_an_in_flight_budget_402_is_an_ordinary_failed_run(ci_gate):
    (passed, _), _ = verdict(ci_gate, {("t2", 0): IN_FLIGHT_402})
    assert passed  # the rerun succeeded


def test_the_run_cap_stopping_a_run_exits_4(ci_gate):
    with pytest.raises(ci_gate.GateExit, match="budget guard refused") as e:
        ci_gate.run_gate(CFG, TASKS, scripted({}, refuse={("t6", 0)}))
    assert e.value.code == 4


def test_records_missing_for_a_pass_exit_3(ci_gate):
    records = [rec(t["id"], 0) for t in TASKS[:-1]]  # t9 never ran
    with pytest.raises(ci_gate.GateExit, match="records missing for: t9 r0") as e:
        ci_gate.decide(records, TASKS, CFG)
    assert e.value.code == 3
    records = [rec(t["id"], 0, t["id"] != "t1") for t in TASKS] + [rec("t1", 1)]
    with pytest.raises(ci_gate.GateExit, match="t1 r2"):
        ci_gate.decide(records, TASKS, CFG)  # the second rerun is missing


def test_runs_jsonl_must_hold_the_records_the_gate_decided_on(ci_gate, tmp_path):
    records = [rec("t0", 0), rec("t1", 0)]
    path = tmp_path / "runs.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    ci_gate.check_written(path, records)
    path.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")  # truncated
    with pytest.raises(ci_gate.GateExit, match="does not hold the 2 runs") as e:
        ci_gate.check_written(path, records)
    assert e.value.code == 3
    with pytest.raises(ci_gate.GateExit):
        ci_gate.check_written(tmp_path / "missing.jsonl", records)


def test_a_missing_secret_exits_3_naming_the_variable_and_no_value(ci_gate, monkeypatch):
    def no_key(name):
        raise RuntimeError(f"{name} is missing or malformed")

    monkeypatch.setattr(ci_gate, "api_key", no_key)
    with pytest.raises(ci_gate.GateExit, match="OPENROUTER_API_KEY is missing") as e:
        ci_gate.preflight(CFG, fetch=lambda key: pytest.fail("no call without a key"))
    assert e.value.code == 3


def info(**data):
    return {"limit": 3, "limit_remaining": 2.5, "effective_budget": None, "label": KEY, **data}


def test_preflight_passes_with_credit_and_prints_only_dollars(ci_gate, monkeypatch, capsys):
    monkeypatch.setattr(ci_gate, "api_key", lambda name: KEY)
    assert ci_gate.preflight(CFG, fetch=lambda key: info()) == 2.5
    assert capsys.readouterr().out == "key: $2.50 left\n"  # not the label, which holds the key


def test_preflight_warns_when_the_key_covers_few_gate_runs(ci_gate, monkeypatch, capsys):
    monkeypatch.setattr(ci_gate, "api_key", lambda name: KEY)
    ci_gate.preflight(CFG, fetch=lambda key: info(limit_remaining=0.5))
    assert "::warning::the key has $0.50 left" in capsys.readouterr().out


@pytest.mark.parametrize(
    "data, message",
    [(info(limit_remaining=0.1), r"\$0.10 left, below the \$0.20 run cap"),
     (info(limit_remaining=2.5, effective_budget={"remaining_usd": -1.0}), "left, below"),
     (info(limit_remaining=None), "no credit limit"),
     ({"unexpected": 1}, "no credit limit"),
     (["not a dict"], "no credit limit")],
)  # fmt: skip
def test_preflight_exits_3_when_the_key_is_low_unlimited_or_unreadable(
    ci_gate, monkeypatch, capsys, data, message
):
    monkeypatch.setattr(ci_gate, "api_key", lambda name: KEY)
    with pytest.raises(ci_gate.GateExit, match=message) as e:
        ci_gate.preflight(CFG, fetch=lambda key: data)
    assert e.value.code == 3 and KEY not in str(e.value) + capsys.readouterr().out


def test_key_check_errors_name_the_status_never_the_reply(ci_gate, monkeypatch):
    def urlopen(request, timeout):
        assert request.get_header("Authorization") == f"Bearer {KEY}"
        raise urllib.error.HTTPError(request.full_url, 401, f"bad key {KEY}", {}, None)

    monkeypatch.setattr(ci_gate.urllib.request, "urlopen", urlopen)
    with pytest.raises(ci_gate.GateExit) as e:
        ci_gate.fetch_key_info(KEY)
    assert (e.value.code, str(e.value)) == (3, "the key check failed: HTTP 401")

    def offline(request, timeout):
        raise TimeoutError(KEY)

    monkeypatch.setattr(ci_gate.urllib.request, "urlopen", offline)
    with pytest.raises(ci_gate.GateExit, match="TimeoutError$") as e:
        ci_gate.fetch_key_info(KEY)
    assert KEY not in str(e.value)


def test_the_gate_runs_a_task_end_to_end_and_writes_its_results(ci_gate, tmp_path, monkeypatch):
    ci_ate = ci_gate.run_eval
    monkeypatch.setattr(ci_ate, "OpenRouter", Scripted)
    monkeypatch.setattr(ci_ate, "EstimateCache", lambda: EstimateCache(tmp_path / "e"))
    monkeypatch.setattr(ci_ate, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(ci_gate, "api_key", lambda name: KEY)
    budgets = tmp_path / "budgets.yaml"
    prices = {"fake": {"input": 1.0, "output": 2.0}}
    budgets.write_text(yaml.safe_dump({"caps": {"ci": 3}, "prices": prices}))
    monkeypatch.setattr(
        ci_gate,
        "Guard",
        lambda phase, run_cap: Guard(phase, budgets, tmp_path / "s.jsonl", run_cap),
    )
    monkeypatch.setattr(ci_gate, "fetch_key_info", lambda key: info())
    summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    Scripted.failures = 0
    cfg = {**CFG, "model": "fake", "phase": "ci", "tasks": "evals/tasks.yaml", "only": [TASK],
           "max_steps": 6, "max_tokens": 100, "estimate_cache": True,
           "results_dir": str(tmp_path / "gate")}  # fmt: skip
    config = tmp_path / "ci-gate.yaml"
    config.write_text(yaml.safe_dump(cfg))

    assert ci_gate.gate(config, cfg) == 0
    (out,) = (tmp_path / "gate").iterdir()
    (run,) = [json.loads(x) for x in (out / "runs.jsonl").read_text("utf-8").splitlines()]
    assert run["correct"] and run["cache"] == {"hits": 0, "misses": 2}
    assert json.loads((out / "meta.json").read_text("utf-8"))["key_left_usd"] == 2.5
    assert (out / "config.yaml").exists() and "# CI eval gate: passed" in summary.read_text("utf-8")
