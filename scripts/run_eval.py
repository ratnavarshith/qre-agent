"""Run the eval: every task in the config, `runs` times each, graded by qre_agent.eval. Writes
results/eval/<model>/<date>[-name]/ with the config, runs.jsonl (one graded line per run) and
summary.md. Traces go to runs/<run_id>.jsonl. Spend phase: the config's (eval).

  .venv/Scripts/python scripts/run_eval.py evals/pilot.yaml --estimate   # cost, no API calls
  .venv/Scripts/python scripts/run_eval.py evals/pilot.yaml
  .venv/Scripts/python scripts/run_eval.py --summarize results/eval/<model>/<date>  # redo summary.md
  .venv/Scripts/python scripts/run_eval.py --self-test   # grader self-test, no API calls
"""

import argparse
import json
import shutil
import statistics
import time
from pathlib import Path

import yaml

from qre_agent.agent import environment, run
from qre_agent.eval import (
    TYPES,
    grade,
    load_suite,
    past_costs,
    read_trace,
    record,
    run_from_trace,
    self_test,
    summarize,
    worst_case,
)
from qre_agent.llm import OpenRouter
from qre_agent.spend import REPO_ROOT, BudgetError, Guard
from qre_agent.tools import SCHEMAS, Toolbox

EVAL_DIR = REPO_ROOT / "results" / "eval"


def selected(cfg, suite):
    only = cfg.get("only")
    if unknown := set(only or ()) - {t["id"] for t in suite["tasks"]}:
        raise SystemExit(f"unknown task ids in `only`: {sorted(unknown)}")
    return [t for t in suite["tasks"] if not only or t["id"] in only]


def estimate(cfg, tasks, guard):
    price = guard.prices[cfg["model"]]
    n_runs = len(tasks) * cfg["runs"]
    worst = cfg["runs"] * sum(
        worst_case(t["prompt"], price, cfg["max_steps"], cfg["max_tokens"]) for t in tasks
    )
    print(f"{len(tasks)} tasks x {cfg['runs']} runs = {n_runs} runs on {cfg['model']}")
    print(f"worst case: ${worst:.4f} (every run uses all {cfg['max_steps']} steps, each call at "
          "the guard's worst case)")  # fmt: skip
    costs = past_costs(cfg["model"])
    if costs:
        mean, high = statistics.mean(costs), max(costs)
        print(f"expected: ${mean * n_runs:.4f} (mean ${mean:.5f} per run over {len(costs)} "
              f"earlier runs of this model; ${high * n_runs:.4f} if every run cost as much as "
              f"the dearest, ${high:.5f})")  # fmt: skip
    else:
        print("expected: no earlier runs of this model to go by")
    print(f"phase {cfg['phase']} has ${guard.remaining():.4f} left of ${guard.cap}")


def out_dir(cfg):
    name = time.strftime("%Y-%m-%d") + (f"-{cfg['name']}" if cfg.get("name") else "")
    path = EVAL_DIR / cfg["model"].replace("/", "_") / name
    if path.exists():
        path = path.with_name(f"{path.name}-{time.strftime('%H%M%S')}")
    path.mkdir(parents=True)
    return path


def run_eval(config_path, cfg, tasks, suite, guard):
    out = out_dir(cfg)
    shutil.copy(config_path, out / "config.yaml")
    env = environment(Toolbox().assumptions)
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
    records = []
    with (out / "runs.jsonl").open("w", encoding="utf-8") as f:
        for repeat in range(cfg["runs"]):
            seed = cfg["seed"] + repeat
            client = OpenRouter(SCHEMAS, seed=seed)
            for task in tasks:
                run_id = f"eval-{stamp}-{task['id']}-r{repeat}"
                toolbox = Toolbox()
                start = time.perf_counter()
                stop = False
                try:
                    result = run(task["prompt"], client, guard, cfg["model"], run_id, toolbox,
                                 cfg["max_steps"], cfg["max_tokens"])  # fmt: skip
                except Exception as e:  # noqa: BLE001 (recorded; a BudgetError ends the eval)
                    result = run_from_trace(run_id, REPO_ROOT / "runs" / f"{run_id}.jsonl")
                    stop = isinstance(e, BudgetError)
                wall = time.perf_counter() - start
                _, last = read_trace(result.trace_path)
                grading = grade(task, toolbox, result, suite, last)
                rec = record(task, toolbox, result, grading, cfg["model"], seed, repeat, wall)
                records.append(rec)
                f.write(json.dumps(rec) + "\n")
                f.flush()
                print(f"r{repeat} {task['id']}: {'correct' if rec['correct'] else rec['category']}, "
                      f"steps {rec['steps']}, ${rec['cost_usd']:.5f}, {wall:.0f} s")  # fmt: skip
                if stop:
                    print("budget guard refused a call; stopping")
                    break
            if stop:
                break
    (out / "summary.md").write_text(summarize(records, meta), encoding="utf-8")
    print(f"results: {out}")


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
    parser.add_argument("--summarize", metavar="DIR", help="rewrite DIR/summary.md")
    parser.add_argument("--self-test", action="store_true", help="test the grader, no API calls")
    args = parser.parse_args()
    if args.summarize:
        return resummarize(args.summarize)
    if args.self_test:
        raise SystemExit(0 if run_self_test(REPO_ROOT / "evals" / "tasks.yaml") else 1)
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    suite = load_suite(REPO_ROOT / cfg["tasks"])
    tasks = selected(cfg, suite)
    guard = Guard(cfg["phase"])
    estimate(cfg, tasks, guard)
    if not args.estimate:
        run_eval(args.config, cfg, tasks, suite, guard)


if __name__ == "__main__":
    main()
