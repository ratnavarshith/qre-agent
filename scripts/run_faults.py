"""Phase 3 reliability experiment: every task in the config at each fault-injection rate, with
the reliability layer (retries, fallback) off and on, graded like the eval. Writes
results/phase3/faults/<date>[-name]/ (or the config's results_dir) with the config, meta.json, runs.jsonl (one graded line per
run) and summary.md. Traces go to runs/. Spend phase: the config's (phase3).

  .venv/Scripts/python scripts/run_faults.py evals/phase3-faults.yaml --estimate   # cost, no API calls
  .venv/Scripts/python scripts/run_faults.py evals/phase3-faults.yaml
  .venv/Scripts/python scripts/run_faults.py --summarize results/phase3/faults/<date>
"""

import argparse
import json
import math
import shutil
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import run_eval

from qre_agent.agent import environment
from qre_agent.claude import Claude
from qre_agent.eval import expected_cost, load_suite, past_runs, worst_case
from qre_agent.faults import Injected
from qre_agent.llm import OpenRouter
from qre_agent.reliability import build
from qre_agent.spend import REPO_ROOT, Guard
from qre_agent.tools import SCHEMAS, Toolbox
from qre_agent.tracing import read_spans

OUT_DIR = REPO_ROOT / "results" / "phase3" / "faults"


def cells(cfg):
    return [(rate, on) for rate in cfg["injection_rates"] for on in cfg["reliability"]]


def estimate(cfg, tasks, guard):
    """Prints the expected and worst-case cost; returns the expected cost.

    Per cell, with reliability on, a call reaches the fallback when all max_retries + 1 attempts
    draw a fault, rate^(max_retries + 1), and every fault kind is retried. A run's expected cost
    mixes the two providers by that share: (1 - p) x the primary's per-run cost + p x the
    fallback's. Each is the mean cost of earlier runs of a profile model (cfg["profile"],
    cfg["fallback_profile"]; the fallback's at its own prices), and the primary's is raised by
    1 / (1 - rate x the malformed share) for the call each malformed reply adds. Runs without
    reliability that end early are counted in full. Faults injected on the fallback are ignored.
    Worst case: every run uses every step at the guard's worst case on the primary (none when
    the primary is forced down: injected faults are free) and, with reliability on, also on the
    fallback; real failures logged at worst case are bounded by the phase cap."""
    prices, n_tasks = guard.prices, len(tasks)
    past = past_runs(cfg["profile"]["model"])
    per_run = expected_cost(prices[cfg["model"]], past)
    steps = statistics.mean(r["steps"] for r in past)
    fb = cfg["fallback"]["model"]
    fb_past = past_runs(cfg["fallback_profile"]["model"])
    fb_run = expected_cost(prices[fb], fb_past)
    kinds = cfg["fault_kinds"]
    malformed = kinds.count("malformed_json") / len(kinds)
    attempts = cfg["retry"]["max_retries"] + 1
    n = n_tasks * cfg["runs"]

    def worst(model):
        return sum(worst_case(t["prompt"], prices[model], cfg["max_steps"], cfg["max_tokens"])
                   for t in tasks)  # fmt: skip

    expected = worst_total = 0.0
    print(f"{n_tasks} tasks x {cfg['runs']} runs x {len(cells(cfg))} cells = "
          f"{n * len(cells(cfg))} runs on {cfg['model']}, fallback {fb}")  # fmt: skip
    print("rate reliability  expected   worst")
    for rate, on in cells(cfg):
        p_fb = rate**attempts if on else 0.0
        primary = per_run / (1 - rate * malformed) if rate < 1 else 0.0
        cell = n * ((1 - p_fb) * primary + p_fb * fb_run)
        bound = cfg["runs"] * ((0 if rate == 1 and on else worst(cfg["model"]))
                               + (worst(fb) if on else 0.0))  # fmt: skip
        expected, worst_total = expected + cell, worst_total + bound
        print(f"{rate:4.1f} {'on ' if on else 'off'}         ${cell:8.4f}  ${bound:8.2f}")
    print(f"expected: ${expected:.4f} (primary ${per_run:.5f} per run, mean of {len(past)} earlier "
          f"{cfg['profile']['model']} runs, {steps:.1f} LLM calls each; fallback ${fb_run:.5f} per "
          f"run, mean of {len(fb_past)} earlier {cfg['fallback_profile']['model']} runs); "
          f"the run stops and asks above ${cfg['stop']['cost_factor'] * expected:.4f}")  # fmt: skip
    print(
        f"worst case: ${worst_total:.2f} (every run uses all {cfg['max_steps']} steps, each "
        "call at the guard's worst case, and with reliability on each also on the fallback)"
    )
    print(f"phase {cfg['phase']} has ${guard.remaining():.4f} left of ${guard.cap}; "
          "the guard refuses any call that could go over")  # fmt: skip
    return expected


def stack(cfg, task, rate, on, fallback_client):
    """(what the agent calls, the primary's injector or None): a Guard, behind an Injected when
    rate > 0, behind a Reliable when on. The fallback is injected at the config's
    fallback_injection_rate (default: the same rate). Faults on the primary and the fallback are
    drawn from separate seeds; off and on see the same draws until the first retry."""
    seed = f"{cfg['seed']}-{task['id']}-{rate}"
    kinds = tuple(cfg["fault_kinds"])
    guard = Guard(cfg["phase"])
    primary = Injected(guard, rate, seed, kinds) if rate else guard
    fallback = None
    if on and fallback_client is not None:
        fb_guard = Guard(cfg["fallback"]["phase"])
        fb_rate = cfg.get("fallback_injection_rate", rate)  # 0 keeps the fallback up
        fb = Injected(fb_guard, fb_rate, f"{seed}-fallback", kinds) if fb_rate else fb_guard
        fallback = (fb, fallback_client, cfg["fallback"]["model"])
    wrapped = build(primary, {"enabled": on, **cfg["retry"]}, fallback, seed)
    return wrapped, primary if rate else None


def reliability_stats(run_id, runs_dir):
    """Retries and fallbacks over a run's LLM calls, from its OpenTelemetry trace (which also has
    the call that raised): retries, calls sent to the fallback, calls answered, calls the
    fallback answered, and the answered calls by model."""
    path = Path(runs_dir) / "otel" / f"{run_id}.jsonl"
    llm = (
        [s["attributes"] for s in read_spans(path) if s["name"] == "llm_call"]
        if path.exists()
        else []
    )
    answered = [a for a in llm if "gen_ai.response.model" in a]
    return {"retries": sum(a.get("retries", 0) for a in llm),
            "fallbacks": sum(bool(a.get("fallback")) for a in llm),
            "answered": len(answered),
            "fallback_answered": sum(bool(a.get("fallback")) for a in answered),
            "models": dict(Counter(a["gen_ai.response.model"] for a in answered))}  # fmt: skip


def run_experiment(config_path, cfg, tasks, suite, expected, client, fallback_client):
    """Task by task, every cell, so a stop leaves the cells balanced. Returns why it stopped
    early, or None."""
    name = time.strftime("%Y-%m-%d") + (f"-{cfg['name']}" if cfg.get("name") else "")
    out = (REPO_ROOT / cfg["results_dir"] if "results_dir" in cfg else OUT_DIR) / name
    if out.exists():
        out = out.with_name(f"{out.name}-{time.strftime('%H%M%S')}")
    out.mkdir(parents=True)
    env = environment(Toolbox().assumptions)
    shutil.copy(config_path, out / "config.yaml")
    with (out / "config.yaml").open("a", encoding="utf-8") as f:
        f.write(f"\n# run at git commit {env['git']['commit']}, dirty: {env['git']['dirty']}\n")
    meta = {"model": cfg["model"], "date": time.strftime("%Y-%m-%d %H:%M:%S %z"), "config": cfg,
            "tasks": str(suite["path"].relative_to(REPO_ROOT)).replace("\\", "/"),
            "tasks_sha256": suite["sha256"], **env}  # fmt: skip
    (out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    stamp, records, stopped = time.strftime("%Y%m%d-%H%M%S"), [], None
    with (out / "runs.jsonl").open("w", encoding="utf-8") as f:
        for task in tasks:
            for rate, on in cells(cfg):
                wrapped, injector = stack(cfg, task, rate, on, fallback_client)
                run_id = f"faults-{stamp}-{task['id']}-p{int(rate * 100)}-{'on' if on else 'off'}"
                rec, refused = run_eval.run_one(cfg, task, 0, run_id, client, wrapped, suite)
                rec |= {"rate": rate, "reliability": on,
                        "injected": dict(injector.counts) if injector else {},
                        **reliability_stats(run_id, run_eval.RUNS_DIR)}  # fmt: skip
                records.append(rec)
                f.write(json.dumps(rec) + "\n")
                f.flush()
                spent = sum(r["cost_usd"] for r in records)
                if not refused and spent > cfg["stop"]["cost_factor"] * expected:
                    refused = (f"cost ${spent:.4f} is over {cfg['stop']['cost_factor']:g}x the "
                               f"expected ${expected:.4f} after {len(records)} runs")  # fmt: skip
                stopped = refused
                if stopped:
                    break
            if stopped:
                break
    (out / "summary.md").write_text(summarize(records, meta), encoding="utf-8")
    print(f"results: {out}")
    return stopped


def p95(values):
    """Nearest-rank 95th percentile."""
    values = sorted(values)
    return values[max(0, math.ceil(0.95 * len(values)) - 1)]


def wilson(k, n, z=1.96):
    """95% Wilson interval for k successes out of n."""
    if not n:
        return 0.0, 0.0
    p, d = k / n, 1 + z * z / n
    centre, half = (
        (p + z * z / (2 * n)) / d,
        z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d,
    )
    return max(0.0, centre - half), min(1.0, centre + half)


def summarize(records, meta, findings=""):
    """The summary table; `findings` is hand-written text (findings.md next to the results)."""
    cfg = meta["config"]
    lines = [
        "# Fault-injection experiment",
        "",
        (
            f"{meta['model']}, {meta['date']}, git {meta['git']['commit']} (dirty: {meta['git']['dirty']}). "
            f"Fallback {cfg['fallback']['model']}; retries {cfg['retry']}; faults {cfg['fault_kinds']}, "
            f"drawn per LLM call. One pass per cell, so the spread shown is a 95% Wilson interval on the "
            "success rate, not a spread across seeds. A run succeeds when it is graded correct; "
            "unlike the eval, runs ended by an API error count as failures. Latency is the run's wall "
            "time (p95: nearest rank). Retries and fallbacks are per run."
        ),
        "",
        (
            "| rate | reliability | runs | correct | 95% CI | api errors | cost $ | $/run "
            "| p50 s | p95 s | retries/run | fallbacks/run | injected/run | answered by fallback |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for rate, on in cells(cfg):
        rs = [r for r in records if r["rate"] == rate and r["reliability"] == on]
        if not rs:
            continue
        n, k = len(rs), sum(r["correct"] for r in rs)
        lo, hi = wilson(k, n)
        latency = [r["latency_s"] for r in rs]
        total = sum(r["cost_usd"] for r in rs)
        lines.append(
            f"| {rate:.0%} | {'on' if on else 'off'} | {n} | {k / n:.0%} | {lo:.0%}–{hi:.0%} "
            f"| {sum(r['category'] == 'api error' for r in rs)} | {total:.4f} | {total / n:.5f} "
            f"| {statistics.median(latency):.1f} | {p95(latency):.1f} "
            f"| {statistics.mean(r['retries'] for r in rs):.2f} "
            f"| {statistics.mean(r['fallbacks'] for r in rs):.2f} "
            f"| {statistics.mean(sum(r['injected'].values()) for r in rs):.2f} "
            f"| {sum(r['fallback_answered'] for r in rs)} of {sum(r['answered'] for r in rs)} |"
        )
    models = Counter()
    for r in records:
        models.update(r["models"])
    lines += [
        "",
        (
            f"LLM calls answered, by model: {dict(models)}. 'Fallbacks/run' counts calls sent to "
            "the fallback, answered or not; 'answered by fallback' counts the replies it gave."
        ),
        "",
        *(["## Findings", "", findings.strip(), ""] if findings.strip() else []),
        f"Versions: {json.dumps(meta['versions'])}",
        f"Hardware: {json.dumps(meta['hardware'])}",
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config", nargs="?")
    parser.add_argument("--estimate", action="store_true", help="print the cost and stop")
    parser.add_argument(
        "--summarize", metavar="DIR", help="rewrite DIR/summary.md, with DIR/findings.md"
    )
    args = parser.parse_args()
    if args.summarize:
        path = Path(args.summarize)
        records = [json.loads(x) for x in (path / "runs.jsonl").read_text("utf-8").splitlines()]
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        notes = path / "findings.md"
        findings = notes.read_text(encoding="utf-8") if notes.exists() else ""
        (path / "summary.md").write_text(summarize(records, meta, findings), encoding="utf-8")
        return
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    suite = load_suite(REPO_ROOT / cfg["tasks"])
    tasks = run_eval.selected(cfg, suite)
    guard = Guard(cfg["phase"])
    expected = estimate(cfg, tasks, guard)
    if args.estimate:
        return
    client = OpenRouter(SCHEMAS, seed=cfg["seed"])
    fallback = Claude(SCHEMAS) if any(cfg["reliability"]) else None
    if stopped := run_experiment(args.config, cfg, tasks, suite, expected, client, fallback):
        print(f"STOPPED: {stopped}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
