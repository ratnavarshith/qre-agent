"""Cost routing between a cheap primary and a strong model, simulated from stored eval runs with
no API calls. Each task and pass is routed by the policy: the primary's run is kept, or the task
escalates and the strong model's run on the same task and pass (same seed) is used instead. An
escalated task costs the primary's LLM calls up to the switch plus the strong model's whole run.
Rates are over all tasks, api errors included (as failures). Writes <results_dir>/config.yaml,
meta.json and summary.md.

  .venv/Scripts/python scripts/routing_sim.py evals/phase3-routing.yaml
"""

import argparse
import hashlib
import json
import shutil
import statistics
from pathlib import Path

import yaml

from qre_agent.agent import git_state
from qre_agent.spend import REPO_ROOT

RUNS_DIR = REPO_ROOT / "runs"
TYPES = ("standard", "free-form", "ambiguous")


def load(path):
    """runs-v2 records by (task, repeat), each with what the policies need from its trace."""
    runs = {}
    for line in (REPO_ROOT / path).read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        trace = [
            json.loads(t) for t in (RUNS_DIR / f"{rec['run_id']}.jsonl").open(encoding="utf-8")
        ]
        llm = [t for t in trace if t["type"] == "llm_call"]
        tools = [t for t in trace if t["type"] == "tool_call"]
        first = tools[0] if tools else None
        rec["verified"] = rec["verify_first_try"] or rec["verify_after_retry"]
        rec["built"] = any(t["name"] == "build_circuit" for t in tools)
        rec["first_build"] = bool(first) and first["name"] == "build_circuit"
        # the LLM calls up to the one that asked for the first tool call
        rec["cost_to_first_tool"] = sum(
            c["cost_usd"] for c in llm if first and c["step"] <= first["step"]
        )
        runs[rec["task"], rec["repeat"]] = rec
    return runs


POLICIES = {  # name -> the primary's cost up to the switch, or None when its run stands
    "a. Gemini only": lambda p: None,
    "b. Sonnet only": lambda p: 0.0,
    "c. Gemini, escalate if verify fails": lambda p: None if p["verified"] else p["cost_usd"],
    "d. Gemini, escalate if verify fails or it used build_circuit": (
        lambda p: None if p["verified"] and not p["built"] else p["cost_usd"]
    ),
    "e. Gemini, to Sonnet when its first tool call is build_circuit": (
        lambda p: p["cost_to_first_tool"] if p["first_build"] else None
    ),
}


def simulate(policy, primary, strong):
    """Per pass: (accuracy, accuracy by type, cost per task, share escalated)."""
    passes = []
    for repeat in sorted({r for _, r in primary}):
        rows = []
        for (task, r), p in primary.items():
            if r != repeat:
                continue
            spent = policy(p)
            if spent is None:
                rows.append((p["type"], p["correct"], p["cost_usd"], False))
            else:
                s = strong[task, r]
                rows.append((p["type"], s["correct"], spent + s["cost_usd"], True))
        by_type = {t: statistics.mean(ok for kind, ok, _, _ in rows if kind == t) for t in TYPES}
        passes.append((
            statistics.mean(ok for _, ok, _, _ in rows),
            by_type,
            statistics.mean(c for _, _, c, _ in rows),
            statistics.mean(e for *_, e in rows),
        ))  # fmt: skip
    return passes


def spread(values, scale, unit, digits):
    """mean ± sample std (min–max), as the eval summary writes it: "82% ± 6 (75–87)"."""
    m, sd, lo, hi = (scale * v for v in (statistics.mean(values), statistics.stdev(values),
                                         min(values), max(values)))  # fmt: skip
    if unit == "$":
        return f"${m:.{digits}f} ± {sd:.{digits}f} ({lo:.{digits}f}–{hi:.{digits}f})"
    return f"{m:.{digits}f}{unit} ± {sd:.{digits}f} ({lo:.{digits}f}–{hi:.{digits}f})"


def pct(values):
    return spread(values, 100, "%", 0)


def usd(values):
    return spread(values, 1, "$", 4)


def frontier(results):
    """Policies no other policy beats on mean accuracy and mean cost (at least as good on both,
    better on one)."""
    means = {
        name: (statistics.mean(p[0] for p in ps), statistics.mean(p[2] for p in ps))
        for name, ps in results.items()
    }
    return [
        n
        for n, (acc, cost) in means.items()
        if not any(
            (a >= acc and c <= cost) and (a > acc or c < cost)
            for m, (a, c) in means.items()
            if m != n
        )
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    args = parser.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    primary, strong = load(cfg["primary"]), load(cfg["strong"])
    if primary.keys() != strong.keys():
        raise ValueError("the two evals have different tasks or passes")
    results = {name: simulate(policy, primary, strong) for name, policy in POLICIES.items()}
    on_frontier = frontier(results)

    out = REPO_ROOT / cfg["results_dir"]
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(args.config, out / "config.yaml")
    inputs = {
        p: hashlib.sha256((REPO_ROOT / p).read_bytes()).hexdigest()
        for p in (cfg["primary"], cfg["strong"])
    }
    meta = {"inputs_sha256": inputs, "git": git_state(), "config": cfg}
    (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")

    n_tasks = len({t for t, _ in primary})
    n_passes = len({r for _, r in primary})
    lines = [
        "# Cost routing, simulated from the four-model eval",
        "",
        (
            f"Generated by `scripts/routing_sim.py` from the stored runs (grader v2) of "
            f"gemini-2.5-flash and claude-sonnet-5: {n_tasks} tasks × {n_passes} passes, no API "
            "calls. Pass i of Gemini is paired with pass i of Sonnet (seed i). An escalated task "
            "is graded on Sonnet's run and costs Gemini's LLM calls up to the switch plus "
            "Sonnet's whole run (a fresh start). Costs are ours (`budgets.yaml` prices, as in "
            "the eval). Values are mean ± sample standard deviation across the passes "
            "(min–max). Unlike the eval summary, Gemini's one api-error run counts as a "
            "failure (escalated by c and d)."
        ),
        "",
        "| policy | correct | cost per task | escalated | frontier |",
        "|---|---|---|---|---|",
    ]
    for name, ps in results.items():
        lines.append(
            f"| {name} | {pct([p[0] for p in ps])} | {usd([p[2] for p in ps])} "
            f"| {pct([p[3] for p in ps])} | {'yes' if name in on_frontier else ''} |"
        )
    lines += ["", "Correct by task type:", "", "| policy | " + " | ".join(TYPES) + " |",
              "|---|" + "---|" * len(TYPES)]  # fmt: skip
    for name, ps in results.items():
        cells = [pct([p[1][t] for p in ps]) for t in TYPES]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += [
        "",
        (
            "Frontier: no other policy has at least the mean accuracy at no more mean cost "
            "(better on one). Policies: c escalates when Gemini's final verify did not pass "
            "(after its one retry; no answer and api errors included) and pays Gemini's whole "
            "run; d also escalates any run that called build_circuit, paying the whole run; e "
            "switches as soon as Gemini's first tool call is build_circuit and pays only the LLM "
            "calls up to that one."
        ),
        "",
    ]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
