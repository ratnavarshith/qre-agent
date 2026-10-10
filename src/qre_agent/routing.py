"""Cost routing, policy f: a task starts on the cheap primary model and restarts from scratch on
the strong one when the primary's first tool call is build_circuit (custom code), or when its
final answer fails verify (after the agent's own retry), runs out of steps or hits an error.
Each leg is a normal run with its own trace; the answering leg's trace gets a `routing` record.
See docs/agent-design.md (Cost routing)."""

import json
import statistics
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from .agent import SWITCHED, run
from .eval import run_from_trace
from .spend import BudgetError

POLICIES = ("none", "f")
REASONS = ("first_build", "verify_failed", "no_answer", "primary_error")


@dataclass
class Routed:
    run: object  # the answering leg's Run, with steps and totals summed over both legs
    toolbox: object  # the answering leg's
    routing: dict
    refused: str | None  # why the budget guard refused a call, if it did


def _totals(a, b):
    return {k: a[k] + b.get(k, 0) for k in a}


def _leg(task, client, guard, model, run_id, toolbox, runs_dir, switch, routing, **kw):
    """(Run, error): a leg that raised is a Run rebuilt from its trace, as the eval does."""
    try:
        result = run(task, client, guard, model, run_id, toolbox, runs_dir=runs_dir,
                     switch_on_first_build=switch, routing=routing, **kw)  # fmt: skip
        return result, None
    except Exception as e:  # noqa: BLE001 (an api error, or a budget stop)
        return run_from_trace(run_id, Path(runs_dir) / f"{run_id}.jsonl"), e


def reason(result, error):
    """Why the primary's leg is not the answer (policy f), or None when it stands."""
    if result.stop_reason == SWITCHED:
        return "first_build"
    if error is not None:
        return "primary_error"
    if result.verification is None:
        return "no_answer"
    return None if result.verification["passed"] else "verify_failed"


def run_routed(task, primary, strong, guard, run_id, make_toolbox, max_steps, max_tokens, runs_dir):
    """`primary` and `strong` are (client, model). The primary runs as `run_id`, the strong model
    as `<run_id>-escalated`; `make_toolbox()` gives each leg a fresh session. A budget refusal
    ends the task on the leg that was refused, with no escalation."""
    (p_client, p_model), (s_client, s_model) = primary, strong
    kw = {"max_steps": max_steps, "max_tokens": max_tokens}
    p_toolbox = make_toolbox()
    p_run, p_error = _leg(task, p_client, guard, p_model, run_id, p_toolbox, runs_dir, True,
                          {"policy": "f", "role": "primary"}, **kw)  # fmt: skip
    why = None if isinstance(p_error, BudgetError) else reason(p_run, p_error)
    routing = {
        "policy": "f",
        "primary_model": p_model,
        "strong_model": s_model,
        "escalated": why is not None,
        "reason": why,
        "primary_run_id": run_id,
        "cost_before_switch_usd": p_run.totals["cost_usd"],
        "cost_after_switch_usd": 0.0,
        "answered_by": p_model,
    }
    answer, toolbox, error = p_run, p_toolbox, p_error
    if why:
        s_id, toolbox = f"{run_id}-escalated", make_toolbox()
        extra = {"policy": "f", "role": "strong", "escalated_from": run_id, "reason": why,
                 "cost_before_switch_usd": p_run.totals["cost_usd"]}  # fmt: skip
        answer, error = _leg(task, s_client, guard, s_model, s_id, toolbox, runs_dir, False,
                             extra, **kw)  # fmt: skip
        routing |= {
            "answered_by": s_model,
            "escalated_run_id": s_id,
            "cost_after_switch_usd": answer.totals["cost_usd"],
        }
    routing["cost_usd"] = routing["cost_before_switch_usd"] + routing["cost_after_switch_usd"]
    if p_toolbox.cache:
        used = [p_toolbox, toolbox] if why else [p_toolbox]
        routing["cache_hits"] = sum(t.cache_hits for t in used)
        routing["cache_misses"] = sum(t.cache_misses for t in used)
    if why:  # the answer's totals cover both legs
        answer = replace(answer, totals=_totals(answer.totals, p_run.totals),
                         steps=answer.steps + p_run.steps)  # fmt: skip
    with answer.trace_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "routing", "time": time.time(), **routing}) + "\n")
    refused = str(error) if isinstance(error, BudgetError) else None
    return Routed(answer, toolbox, routing, refused)


def _percentile(values, q):
    """Linear interpolation, as scripts/eval_summary.py."""
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


def pct(x):
    return f"{100 * x:.0f}%"


def usd(x):
    return f"${x:.4f}"


def _range(label, cfg, fmt):
    low, mean, high = (fmt(x) for x in cfg[label])
    return f"{mean} ({low}–{high})"


def summary(records, prediction):
    """Markdown section for a routed eval: accuracy, cost, escalations and reasons, latency and
    cache use, next to the simulation's `prediction` ({correct, cost, escalated}: [min, mean,
    max] over its three passes). One pass, so no spread: the simulation's range is the yardstick."""
    n = len(records)
    routed = [r["routing"] for r in records]
    kept = [r for r in records if not r["routing"]["escalated"]]
    sent = [r for r in records if r["routing"]["escalated"]]
    reasons = Counter(x["reason"] for x in routed if x["escalated"])
    correct = statistics.mean(r["correct"] for r in records)
    cost = statistics.mean(r["cost_usd"] for r in records)
    lat = [r["latency_s"] for r in records]

    def row(name, group):
        if not group:
            return f"| {name} | 0 | | | | |"
        ok = statistics.mean(r["correct"] for r in group)
        t = [r["latency_s"] for r in group]
        return (
            f"| {name} | {len(group)} | {pct(ok)} | {usd(statistics.mean(r['cost_usd'] for r in group))} "
            f"| {statistics.median(t):.0f} s | {max(t):.0f} s |"
        )

    lines = [
        "",
        "## Routing (policy f)",
        "",
        (
            f"One pass of {n} tasks, so no spread across passes; api errors count as failures. "
            "Cost is ours (`budgets.yaml` prices) and covers both legs of an escalated task."
        ),
        "",
        "| | this run | simulation, policy f (mean of 3 passes, range) |",
        "|---|---|---|",
        f"| correct | {pct(correct)} | {_range('correct', prediction, pct)} |",
        f"| cost per task | {usd(cost)} | {_range('cost', prediction, usd)} |",
        f"| escalated | {pct(len(sent) / n)} | {_range('escalated', prediction, pct)} |",
        "",
        "Escalations by reason: "
        + (", ".join(f"{k} {reasons[k]}" for k in REASONS if reasons[k]) or "none")
        + f". Cost before the switch ${sum(x['cost_before_switch_usd'] for x in routed):.4f}, "
        f"after ${sum(x['cost_after_switch_usd'] for x in routed):.4f} (total over the pass).",
        "",
        "| group | tasks | correct | cost per task | latency p50 | max |",
        "|---|---|---|---|---|---|",
        row("answered by the primary", kept),
        row("escalated", sent),
        *(
            row(f"escalated: {k}", [r for r in sent if r["routing"]["reason"] == k])
            for k in reasons
        ),
        "",
        (
            f"Latency per task (both legs, pacer wait left out): p50 "
            f"{statistics.median(lat):.0f} s, p95 {_percentile(lat, 95):.0f} s, "
            f"max {max(lat):.0f} s."
        ),
    ]
    if any("cache_hits" in x for x in routed):
        hits = sum(x.get("cache_hits", 0) for x in routed)
        misses = sum(x.get("cache_misses", 0) for x in routed)
        lines += ["", f"Estimate cache: {hits} hits, {misses} misses."]
    return "\n".join(lines) + "\n"
