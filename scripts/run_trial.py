"""Run the trial tasks once each through the agent and report, per task: correct vs expected,
verify, steps, tokens, our cost vs OpenRouter's, latency. Traces go to runs/<run_id>.jsonl and
the report to runs/<trial_id>.json.

  .venv/Scripts/python scripts/run_trial.py evals/trial.yaml --estimate   # worst case, no calls
  .venv/Scripts/python scripts/run_trial.py evals/trial.yaml
"""

import argparse
import json
import math
import re
import time
from pathlib import Path

import yaml

from qre_agent.agent import RUNS_DIR, run, system_prompt
from qre_agent.llm import OpenRouter
from qre_agent.spend import Guard, cost
from qre_agent.tools import SCHEMAS, Toolbox

# Worst-case growth of the prompt per step, for --estimate: a reply that fills max_tokens at
# OUT_CHARS chars per token, plus TOOL_CALLS tool results of TOOL_CHARS chars each (a bicycle
# result is about 2,000 chars).
OUT_CHARS, TOOL_CALLS, TOOL_CHARS = 6, 3, 4000
FIELDS = ("physical_qubits", "runtime_ns", "physical_qubit_seconds", "t_count")
P_WRITTEN = re.compile(r"1(?:\.0)?\s*[eE]-0?3|0\.001|10\^-3|10⁻³")


def worst_case(cfg, guard):
    """Dollars if every task uses every step and every call hits the guard's worst case."""
    price = guard.prices[cfg["model"]]
    tools = len(json.dumps([{"type": "function", "function": s} for s in SCHEMAS]))
    system = system_prompt(Toolbox().assumptions)
    total = 0.0
    for task in cfg["tasks"]:
        base = (
            len(
                json.dumps(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": task["prompt"]},
                    ]
                )
            )
            + tools
        )
        grow = OUT_CHARS * cfg["max_tokens"] + TOOL_CALLS * TOOL_CHARS
        total += sum(
            cost(price, base + k * grow, cfg["max_tokens"]) for k in range(cfg["max_steps"])
        )
    return total


def check(task, toolbox, result):
    """Compare the results the answer cites with the expected rows."""
    out = {}
    if result.answer:
        cited = {
            toolbox.results[e["result_id"]]["architecture"]: toolbox.results[e["result_id"]]
            for e in result.answer["estimates"]
        }
        for arch, expected in task["expected"].items():
            if arch == "row":
                continue
            got = cited.get(arch, {})
            out[arch] = {
                f: {
                    "got": got.get(f),
                    "expected": v,
                    "ok": got.get(f) is not None and math.isclose(got[f], v, rel_tol=1e-6),
                }
                for f, v in expected.items()
            }
            out[arch]["p"] = got.get("assumptions", {}).get("physical_error_rate")
    # Benchmark task: the cited circuit must come from build_benchmark with the expected family, n.
    circuit_ids = {r["circuit"]["circuit_id"] for r in cited.values()} if result.answer else set()
    built = [o for o in toolbox.outputs if o.get("circuit_id") in circuit_ids]
    benchmark = [(o.get("family"), o.get("n")) for o in built]
    row = task["expected"]["row"]
    fields_ok = all(c["ok"] for a in out.values() for c in a.values() if isinstance(c, dict))
    out["benchmark"] = {"got": benchmark, "ok": benchmark == [(row["family"], row["n"])]}
    correct = bool(result.answer) and fields_ok and out["benchmark"]["ok"]
    summary = result.answer["summary"] if result.answer else ""
    stated = None
    if "must_state_assumption" in task:
        stated = bool(re.search(r"assum", summary, re.IGNORECASE) and P_WRITTEN.search(summary))
    return {"correct": correct, "fields": out, "assumption_stated": stated}


def main(config_path, estimate_only):
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    guard = Guard(cfg["phase"])
    worst = worst_case(cfg, guard)
    print(
        f"worst case for the trial: ${worst:.4f}; {cfg['phase']} has ${guard.remaining():.4f} left"
    )
    print(
        f"(each call is also checked by the guard against the {cfg['phase']} cap before it is sent)"
    )
    if estimate_only:
        return

    trial_id = time.strftime("trial-%Y%m%d-%H%M%S")
    client = OpenRouter(SCHEMAS, seed=cfg["seed"])
    report = {"trial_id": trial_id, "config": cfg, "tasks": []}
    for task in cfg["tasks"]:
        toolbox = Toolbox()
        result = run(
            task["prompt"],
            client,
            guard,
            cfg["model"],
            f"{trial_id}-{task['id']}",
            toolbox,
            cfg["max_steps"],
            cfg["max_tokens"],
        )
        row = {
            "id": task["id"],
            "run_id": result.run_id,
            "stop_reason": result.stop_reason,
            "steps": result.steps,
            "verification": result.verification,
            "totals": result.totals,
            "answer": result.answer,
            **check(task, toolbox, result),
        }
        report["tasks"].append(row)
        t = result.totals
        v = result.verification
        print(
            f"{task['id']}: {result.stop_reason}, correct={row['correct']}, "
            f"verify={'pass' if v and v['passed'] else [f['check'] for f in v['failures']] if v else '-'}, "
            f"steps={result.steps}, tokens in/cached/out={t['input_tokens']}/{t['cached_tokens']}/"
            f"{t['output_tokens']}, cost ours ${t['cost_usd']:.5f} vs OpenRouter "
            f"${t['reported_cost_usd']:.5f}, LLM {t['llm_s']:.1f} s + tools {t['tool_s']:.1f} s"
        )
    path = RUNS_DIR / f"{trial_id}.json"
    path.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"report: {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--estimate", action="store_true", help="print the worst case and stop")
    args = parser.parse_args()
    main(args.config, args.estimate)
