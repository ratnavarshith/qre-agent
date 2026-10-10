"""Run the eval: every task in the config, `runs` times each, graded by qre_agent.eval. Writes
results/eval/<model>/<date>[-name]/ with the config, runs.jsonl (one graded line per run) and
summary.md. Traces go to runs/<run_id>.jsonl. Spend phase: the config's (eval).

  .venv/Scripts/python scripts/run_eval.py evals/pilot.yaml --estimate   # cost, no API calls
  .venv/Scripts/python scripts/run_eval.py evals/pilot.yaml
  .venv/Scripts/python scripts/run_eval.py --rerun-errors results/eval/<model>/<date>  # api errors, once
  .venv/Scripts/python scripts/run_eval.py --rerun DIR --runs TASK:REPEAT ... --reason TEXT
  .venv/Scripts/python scripts/run_eval.py --regrade DIR ...  # current grader, from the traces
  .venv/Scripts/python scripts/run_eval.py --summarize results/eval/<model>/<date>  # redo summary.md
  .venv/Scripts/python scripts/run_eval.py --self-test   # grader self-test, no API calls
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import yaml

from qre_agent.agent import environment, git_state, run
from qre_agent.eval import (
    TYPES,
    expected_cost,
    grade,
    load_suite,
    past_runs,
    read_trace,
    record,
    regrade,
    replace_reruns,
    run_from_trace,
    self_test,
    stop_reason,
    summarize,
    worst_case,
)
from qre_agent.llm import OpenRouter, Paced, Pacer
from qre_agent.spend import REPO_ROOT, BudgetError, Guard
from qre_agent.tools import SCHEMAS, Toolbox

EVAL_DIR = REPO_ROOT / "results" / "eval"
RUNS_DIR = REPO_ROOT / "runs"  # traces


def selected(cfg, suite):
    only = cfg.get("only")
    if unknown := set(only or ()) - {t["id"] for t in suite["tasks"]}:
        raise SystemExit(f"unknown task ids in `only`: {sorted(unknown)}")
    return [t for t in suite["tasks"] if not only or t["id"] in only]


def estimate(cfg, tasks, guard):
    """Prints the worst-case and expected cost; returns the expected cost of the whole eval.
    Expected: mean cost of the earlier runs of the profile model (cfg["profile"]), with input
    tokens scaled to this model's tokenizer by the ratio of the two models' prompt tokens, and
    for a caching model its cached prefix read on every step."""
    price = guard.prices[cfg["model"]]
    n_runs = len(tasks) * cfg["runs"]
    worst = cfg["runs"] * sum(
        worst_case(t["prompt"], price, cfg["max_steps"], cfg["max_tokens"]) for t in tasks
    )
    profile = cfg["profile"]
    past = past_runs(profile["model"])
    same = cfg["model"] == profile["model"]
    ratio = 1 if same else cfg["prompt_tokens"] / profile["prompt_tokens"]
    prefix = 0 if same else cfg.get("cached_prefix_tokens", 0)
    per_run = expected_cost(price, past, ratio, prefix)
    expected = per_run * n_runs
    print(f"{len(tasks)} tasks x {cfg['runs']} runs = {n_runs} runs on {cfg['model']}")
    print(f"worst case: ${worst:.4f} (every run uses all {cfg['max_steps']} steps, each call at "
          "the guard's worst case)")  # fmt: skip
    print(f"expected: ${expected:.4f} (${per_run:.5f} per run: mean tokens of {len(past)} earlier "
          f"{profile['model']} runs, input x{ratio:.2f} for this model's tokenizer"
          + (f", {prefix} prompt tokens read from the cache on every step" if prefix else "")
          + f"); the eval stops and asks above ${cfg['stop']['cost_factor'] * expected:.4f}")  # fmt: skip
    print(f"phase {cfg['phase']} has ${guard.remaining():.4f} left of ${guard.cap}")
    return expected


def out_dir(cfg):
    name = time.strftime("%Y-%m-%d") + (f"-{cfg['name']}" if cfg.get("name") else "")
    path = EVAL_DIR / cfg["model"].replace("/", "_") / name
    if path.exists():
        path = path.with_name(f"{path.name}-{time.strftime('%H%M%S')}")
    path.mkdir(parents=True)
    return path


def make_client(cfg, seed, pacer):
    """The OpenRouter client for one repeat, behind the eval's pacer if it has one."""
    client = OpenRouter(SCHEMAS, seed=seed, cache=cfg.get("cache", False))
    return Paced(client, pacer) if pacer else client


def run_one(cfg, task, repeat, run_id, client, guard, suite, rerun_of=None):
    """One graded run. Returns its runs.jsonl record and why the budget guard refused a call, if
    it did."""
    toolbox, refused = Toolbox(), None
    pacer = getattr(client, "pacer", None)
    waited = pacer.waited if pacer else 0.0
    start = time.perf_counter()
    try:
        result = run(task["prompt"], client, guard, cfg["model"], run_id, toolbox,
                     cfg["max_steps"], cfg["max_tokens"], RUNS_DIR)  # fmt: skip
    except Exception as e:  # noqa: BLE001 (recorded as an api error, or a budget stop)
        result = run_from_trace(run_id, RUNS_DIR / f"{run_id}.jsonl")
        if isinstance(e, BudgetError):
            refused = f"the budget guard refused a call: {e}"
    wall = time.perf_counter() - start
    _, last = read_trace(result.trace_path)
    grading = grade(task, toolbox, result, suite, last)
    seed = cfg["seed"] + repeat
    paced = pacer.waited - waited if pacer else 0.0
    rec = record(task, toolbox, result, grading, cfg["model"], seed, repeat, wall, rerun_of, paced)
    print(f"r{repeat} {task['id']}: {'correct' if rec['correct'] else rec['category']}, "
          f"steps {rec['steps']}, ${rec['cost_usd']:.5f}, {wall:.0f} s")  # fmt: skip
    return rec, refused


def run_eval(config_path, cfg, tasks, suite, guard, expected):
    """Returns the reason the eval stopped early, or None."""
    out = out_dir(cfg)
    env = environment(Toolbox().assumptions)
    shutil.copy(config_path, out / "config.yaml")
    with (out / "config.yaml").open("a", encoding="utf-8") as f:  # the copy says what ran
        f.write(f"\n# run at git commit {env['git']['commit']}, dirty: {env['git']['dirty']}\n")
    meta = {
        "model": cfg["model"],
        "date": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "config": cfg,
        "tasks": str(suite["path"].relative_to(REPO_ROOT)).replace("\\", "/"),
        "tasks_sha256": suite["sha256"],
        **env,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    records, stopped = [], None
    pacer = Pacer(cfg["max_rpm"]) if cfg.get("max_rpm") else None
    with (out / "runs.jsonl").open("w", encoding="utf-8") as f:
        for repeat in range(cfg["runs"]):
            client = make_client(cfg, cfg["seed"] + repeat, pacer)
            for task in tasks:
                run_id = f"eval-{stamp}-{task['id']}-r{repeat}"
                rec, stopped = run_one(cfg, task, repeat, run_id, client, guard, suite)
                records.append(rec)
                f.write(json.dumps(rec) + "\n")
                f.flush()
                stopped = stopped or stop_reason(records, expected, cfg["stop"])
                if stopped:
                    break
            if stopped:
                break
    (out / "summary.md").write_text(summarize(records, meta), encoding="utf-8")
    print(f"results: {out}")
    return stopped


def rerun(path, guard, runs, reason):
    """Reruns once each (task, repeat) in `runs` of the eval in `path` (same seed), replaces it in
    runs.jsonl, records `reason` in the new record's `rerun_of` and in meta.json, and rewrites
    summary.md. The first attempts stay in runs-first-attempt.jsonl. Returns the reason it
    stopped early, or None."""
    path = Path(path)
    cfg = yaml.safe_load((path / "config.yaml").read_text(encoding="utf-8"))
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    suite = load_suite(REPO_ROOT / cfg["tasks"])
    if suite["sha256"] != meta["tasks_sha256"]:
        raise SystemExit("the task file changed since this eval ran; not rerunning")
    lines = (path / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    if missing := set(runs) - {(r["task"], r["repeat"]) for r in records}:
        raise SystemExit(f"runs not in this eval: {sorted(missing)}")
    first = path / "runs-first-attempt.jsonl"
    if not first.exists():
        first.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tasks = {t["id"]: t for t in suite["tasks"]}
    todo = [r for r in records if (r["task"], r["repeat"]) in runs]
    print(f"{len(todo)} runs of {len(records)} to rerun once ({reason})")
    stamp, reruns, stopped = time.strftime("%Y%m%d-%H%M%S"), [], None
    pacer = Pacer(cfg["max_rpm"]) if cfg.get("max_rpm") else None
    for old in todo:
        client = make_client(cfg, old["seed"], pacer)
        run_id = f"eval-{stamp}-{old['task']}-r{old['repeat']}-rerun"
        rerun_of = old | {"reason": reason}
        rec, stopped = run_one(cfg, tasks[old["task"]], old["repeat"], run_id, client, guard,
                               suite, rerun_of)  # fmt: skip
        reruns.append(rec)
        if stopped:
            break
    records = replace_reruns(records, reruns)
    events = meta.get("reruns", [])
    events = [events] if isinstance(events, dict) else events  # one dict before reasons existed
    events.append({"date": time.strftime("%Y-%m-%d %H:%M:%S %z"), "git": git_state(),
                   "reason": reason, "runs": [[r["task"], r["repeat"]] for r in reruns]})  # fmt: skip
    meta["reruns"] = events
    (path / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), "utf-8")
    (path / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    (path / "summary.md").write_text(summarize(records, meta), encoding="utf-8")
    print(f"rewrote {path / 'runs.jsonl'} and summary.md")
    return stopped


def rerun_errors(path, guard):
    """Reruns once each run of the eval in `path` that was an api error and not yet rerun."""
    records = [
        json.loads(line) for line in (Path(path) / "runs.jsonl").read_text("utf-8").splitlines()
    ]
    runs = {(r["task"], r["repeat"]) for r in records
            if r["category"] == "api error" and "rerun_of" not in r}  # fmt: skip
    return rerun(path, guard, runs, "api error")


def regrade_dir(path):
    """Grades every run of the eval in `path` again with the current grader, from the traces (no
    LLM calls), into runs-v<version>.jsonl and summary-v<version>.md next to runs.jsonl."""
    path = Path(path)
    cfg = yaml.safe_load((path / "config.yaml").read_text(encoding="utf-8"))
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    suite = load_suite(REPO_ROOT / cfg["tasks"])
    if suite["sha256"] != meta["tasks_sha256"]:
        raise SystemExit("the task file changed since this eval ran; not regrading")
    tasks = {t["id"]: t for t in suite["tasks"]}
    lines = (path / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    old = [json.loads(line) for line in lines]
    new = [regrade(tasks[r["task"]], r, suite, RUNS_DIR) for r in old]
    version = new[0]["grader"]
    out = path / f"runs-v{version}.jsonl"
    out.write_text("".join(json.dumps(r) + "\n" for r in new), encoding="utf-8")
    meta = meta | {"grader": version, "regraded": time.strftime("%Y-%m-%d %H:%M:%S %z"),
                   "regrade_git": git_state()}  # fmt: skip
    (path / f"summary-v{version}.md").write_text(summarize(new, meta), encoding="utf-8")
    changed = [(a, b) for a, b in zip(old, new, strict=True)
               if (a["correct"], a["category"]) != (b["correct"], b["category"])]  # fmt: skip
    print(f"{path}: {len(changed)} of {len(new)} runs graded differently")
    for a, b in changed:
        print(
            f"  {a['task']} r{a['repeat']}: {a['category'] or 'correct'} -> {b['category'] or 'correct'}"
        )
    return changed


def resummarize(path):
    path = Path(path)
    records = [json.loads(line) for line in (path / "runs.jsonl").read_text("utf-8").splitlines()]
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    (path / "summary.md").write_text(summarize(records, meta), encoding="utf-8")
    print(f"wrote {path / 'summary.md'}")


def run_self_test(tasks_path):
    suite = load_suite(tasks_path)
    lines = [
        "# Grader self-test",
        "",
        (
            f"`{suite['path'].relative_to(REPO_ROOT).as_posix()}` (sha256 {suite['sha256'][:12]}). "
            "Each task's reference answer, built with the real tools, must be graded correct; "
            "each corrupted answer must fail with the expected category. Regenerate with "
            "`.venv/Scripts/python scripts/run_eval.py --self-test`."
        ),
        "",
        "| task | type | variant | expected | got | ok |",
        "|---|---|---|---|---|---|",
    ]
    bad = 0
    counts = dict.fromkeys(TYPES, 0)
    for task in suite["tasks"]:
        for r in self_test(task, suite):
            counts[task["type"]] += 1
            bad += not r["ok"]
            got = "correct" if r["correct"] else r["category"]
            lines.append(f"| {task['id']} | {task['type']} | {r['variant']} "
                         f"| {r['expected'] or 'correct'} | {got} | {'yes' if r['ok'] else '**NO**'} |")  # fmt: skip
            if not r["ok"]:
                print(f"FAIL {task['id']} {r['variant']}: {r['reasons']}")
    total = sum(counts.values())
    lines[4:4] = [
        f"{total - bad}/{total} cases as expected ("
        + ", ".join(f"{t}: {n}" for t, n in counts.items())
        + ").",
        "",
    ]
    path = EVAL_DIR / "grader-selftest.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{total - bad}/{total} cases as expected; table in {path}")
    return bad == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config", nargs="?")
    parser.add_argument("--estimate", action="store_true", help="print the cost and stop")
    parser.add_argument("--rerun-errors", metavar="DIR", help="rerun DIR's api-error runs once")
    parser.add_argument("--rerun", metavar="DIR", help="rerun the --runs of DIR once")
    parser.add_argument("--runs", nargs="+", metavar="TASK:REPEAT", help="runs for --rerun")
    parser.add_argument("--reason", help="why the --runs are rerun (recorded)")
    parser.add_argument("--regrade", metavar="DIR", nargs="+", help="grade DIR's runs again")
    parser.add_argument("--summarize", metavar="DIR", help="rewrite DIR/summary.md")
    parser.add_argument("--self-test", action="store_true", help="test the grader, no API calls")
    args = parser.parse_args()
    if args.summarize:
        return resummarize(args.summarize)
    if args.regrade:
        for path in args.regrade:
            regrade_dir(path)
        return
    if args.rerun:
        if not (args.runs and args.reason):
            parser.error("--rerun needs --runs and --reason")
        runs = {(task, int(repeat)) for task, repeat in (r.rsplit(":", 1) for r in args.runs)}
        if stopped := rerun(args.rerun, Guard("eval"), runs, args.reason):
            print(f"STOPPED: {stopped}", file=sys.stderr)
            raise SystemExit(2)
        return
    if args.rerun_errors:
        if stopped := rerun_errors(args.rerun_errors, Guard("eval")):
            print(f"STOPPED: {stopped}", file=sys.stderr)
            raise SystemExit(2)
        return
    if args.self_test:
        raise SystemExit(0 if run_self_test(REPO_ROOT / "evals" / "tasks.yaml") else 1)
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    suite = load_suite(REPO_ROOT / cfg["tasks"])
    tasks = selected(cfg, suite)
    guard = Guard(cfg["phase"])
    expected = estimate(cfg, tasks, guard)
    if not args.estimate and (stopped := run_eval(args.config, cfg, tasks, suite, guard, expected)):
        print(f"STOPPED: {stopped}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
