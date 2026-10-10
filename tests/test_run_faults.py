import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml
from test_run_eval import TASK, Scripted

from qre_agent.faults import Injected
from qre_agent.reliability import Reliable
from qre_agent.spend import Guard

SCRIPTS = Path(__file__).parents[1] / "scripts"


@pytest.fixture
def run_faults(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("run_faults", SCRIPTS / "run_faults.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "OUT_DIR", tmp_path / "faults")
    monkeypatch.setattr(sys.modules["run_eval"], "RUNS_DIR", tmp_path / "runs")
    budgets = tmp_path / "budgets.yaml"
    prices = {"fake": {"input": 1.0, "output": 2.0}, "fake2": {"input": 3.0, "output": 4.0}}
    budgets.write_text(yaml.safe_dump({"caps": {"phase3": 1}, "prices": prices}))
    monkeypatch.setattr(
        module, "Guard", lambda phase: Guard(phase, budgets, tmp_path / "spend.jsonl")
    )
    return module


CFG = {"model": "fake", "phase": "phase3", "tasks": "evals/tasks.yaml", "runs": 1, "seed": 0,
       "max_steps": 6, "max_tokens": 100, "injection_rates": [0.0, 1.0],
       "fault_kinds": ["server"], "reliability": [False, True],
       "retry": {"max_retries": 3, "base_s": 0.0, "cap_s": 0.0},
       "fallback": {"model": "fake2", "phase": "phase3"}, "stop": {"cost_factor": 100}}  # fmt: skip


def test_the_stack_follows_the_config(run_faults):
    task = {"id": "t"}
    guard, injector = run_faults.stack(CFG, task, 0.0, False, None)
    assert isinstance(guard, Guard) and injector is None
    wrapped, injector = run_faults.stack(CFG, task, 0.2, False, None)
    assert isinstance(wrapped, Injected) and wrapped is injector and wrapped.rate == 0.2
    wrapped, injector = run_faults.stack(CFG, task, 0.2, True, "claude")
    assert isinstance(wrapped, Reliable) and wrapped.guard is injector
    fb_injected, fb_client, fb_model = wrapped.fallback
    assert (fb_client, fb_model, fb_injected.rate) == ("claude", "fake2", 0.2)
    # off and on draw the same faults for a task and rate; the fallback draws its own
    again, _ = run_faults.stack(CFG, task, 0.2, False, None)
    assert [again.draw() for _ in range(50)] == [injector.draw() for _ in range(50)]
    assert fb_injected.rng.random() != Injected(None, 0.2, "0-t-0.2").rng.random()


@pytest.mark.compiler
def test_every_cell_runs_and_is_summarized(run_faults, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(CFG))
    suite = run_faults.load_suite(run_faults.REPO_ROOT / CFG["tasks"])
    tasks = [t for t in suite["tasks"] if t["id"] == TASK]
    Scripted.failures = 0
    stopped = run_faults.run_experiment(config, CFG, tasks, suite, 1.0, Scripted(), Scripted())
    assert stopped is None
    (out,) = (tmp_path / "faults").iterdir()
    records = {(r["rate"], r["reliability"]): r
               for r in map(json.loads, (out / "runs.jsonl").read_text("utf-8").splitlines())}  # fmt: skip
    assert records[0.0, False]["correct"] and records[0.0, True]["correct"]
    assert (records[0.0, True]["retries"], records[0.0, True]["fallbacks"]) == (0, 0)
    off, on = records[1.0, False], records[1.0, True]  # every call faults, the fallback's too
    assert (off["category"], off["retries"], off["injected"]) == ("api error", 0, {"server": 1})
    assert (on["category"], on["retries"], on["fallbacks"]) == ("api error", 6, 1)
    assert on["injected"] == {"server": 4}  # the primary's; the fallback's 4 are on its injector
    summary = (out / "summary.md").read_text("utf-8")
    assert summary.count("\n| ") == 5  # header and four cells
    assert "| 0% | on | 1 | 100% |" in summary and "| 100% | off | 1 | 0% |" in summary


def test_the_fallback_can_be_kept_up_while_the_primary_is_forced_down(run_faults):
    cfg = CFG | {"fallback_injection_rate": 0.0}
    wrapped, injector = run_faults.stack(cfg, {"id": "t"}, 1.0, True, "claude")
    assert injector.rate == 1.0 and wrapped.fallback[0] is not injector
    assert not isinstance(wrapped.fallback[0], Injected)  # the guard itself


@pytest.mark.compiler
def test_with_the_primary_down_every_call_is_answered_by_the_fallback(run_faults, tmp_path):
    cfg = CFG | {"injection_rates": [1.0], "reliability": [True], "fallback_injection_rate": 0.0,
                 "fault_kinds": ["timeout", "rate_limit", "server", "malformed_json"]}  # fmt: skip
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(cfg))
    suite = run_faults.load_suite(run_faults.REPO_ROOT / cfg["tasks"])
    tasks = [t for t in suite["tasks"] if t["id"] == TASK]
    Scripted.failures = 0
    primary, fallback = Scripted(), Scripted()
    stopped = run_faults.run_experiment(config, cfg, tasks, suite, 1.0, primary, fallback)
    assert stopped is None
    (out,) = (tmp_path / "faults").iterdir()
    (rec,) = map(json.loads, (out / "runs.jsonl").read_text("utf-8").splitlines())
    assert rec["correct"] and rec["steps"] == 3
    assert rec["answered"] == rec["fallback_answered"] == 3 and rec["models"] == {"fake2": 3}
    assert rec["fallbacks"] == 3 and rec["retries"] >= 9  # 3 retries before each fallback
    assert "| 3 of 3 |" in (out / "summary.md").read_text("utf-8")
