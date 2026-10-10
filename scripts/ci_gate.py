"""CI eval gate: 10 of the eval's tasks on one model, failed if the agent regressed. Pass 1 runs
each task once (seed + 0); 3 or more failures fail the gate at once, otherwise every failed task
is rerun with seeds + 1 and + 2 and the gate fails if any rerun fails. Writes the config, meta.json,
runs.jsonl and summary.md to the config's results_dir (and the summary to $GITHUB_STEP_SUMMARY).
Spend phase: the config's, with its run_cap on top. See docs/agent-design.md (CI eval gate).

  .venv/Scripts/python scripts/ci_gate.py evals/ci-gate.yaml

Exit codes: 0 passed, 1 accuracy regression, 2 the run cap stopped it, 3 the gate could not run
(key missing, rejected or out of credit, or records missing). Never prints the key or the key
check's response, only dollar amounts.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import run_eval

from qre_agent.agent import environment
from qre_agent.eval import load_suite
from qre_agent.llm import Pacer
from qre_agent.spend import REPO_ROOT, Guard, api_key
from qre_agent.tools import Toolbox

KEY_URL = "https://openrouter.ai/api/v1/key"  # https://openrouter.ai/docs/api-reference/limits
WARN_FACTOR = 3  # warn when the key has less than this many run caps left
# A key out of credit is a 402 or a 403 with one of these `limit_source`s (the docs list the key's
# own limit under both; the in-flight budget, also a 402, is transient and not one of them), or a
# 401 for a key OpenRouter doesn't accept.
OUT_OF_CREDIT = re.compile(
    r"HTTP (?:402|403)\b.*(?:openrouter_key_limit|openrouter_credits)", re.DOTALL
)
REJECTED = re.compile(r"HTTP 401\b")


class GateExit(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def key_remaining(data):
    """Dollars left on the key, from GET /api/v1/key's `data`: the least of the key's own
    `limit_remaining` and the `effective_budget`'s `remaining_usd`. None if it has neither."""
    left = [data.get("limit_remaining")]
    if budget := data.get("effective_budget"):
        left.append(budget.get("remaining_usd"))
    left = [x for x in left if isinstance(x, (int, float))]
    return min(left) if left else None


def fetch_key_info(key, url=KEY_URL, timeout=20):
    """`data` of the key check. Errors name the status or the exception type, never the reply."""
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return json.load(r)["data"]
    except urllib.error.HTTPError as e:
        raise GateExit(3, f"the key check failed: HTTP {e.code}") from None
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise GateExit(3, f"the key check failed: {type(e).__name__}") from None


def preflight(cfg, fetch=None):
    """Checks the key before any run; returns the dollars left on it."""
    try:
        key = api_key("OPENROUTER_API_KEY")
    except RuntimeError:
        raise GateExit(3, "OPENROUTER_API_KEY is missing or malformed in this run") from None
    data = (fetch or fetch_key_info)(key)
    left = key_remaining(data) if isinstance(data, dict) else None
    if left is None:
        raise GateExit(3, "the key has no credit limit, so nothing caps it but the run cap")
    if left < cfg["run_cap"]:
        raise GateExit(
            3,
            f"the key has ${left:.2f} left, below the ${cfg['run_cap']:.2f} run cap: "
            "raise its limit or replace OPENROUTER_API_KEY",
        )
    if left < WARN_FACTOR * cfg["run_cap"]:
        print(f"::warning::the key has ${left:.2f} left, under {WARN_FACTOR} gate runs")
    print(f"key: ${left:.2f} left")
    return left


def key_error(record):
    """Why the run says the key can't be used, or None."""
    reason = record["stop_reason"]
    if REJECTED.search(reason):
        return "the key was rejected (HTTP 401)"
    if OUT_OF_CREDIT.search(reason):
        return "the key is out of credit (OpenRouter refused the call)"
    return None


def decide(records, tasks, cfg):
    """(passed, why) from the records. A pass needs a first-pass record for every task and all the
    reruns of the tasks that failed it; GateExit(3) if any are missing."""
    first = {r["task"]: r for r in records if r["repeat"] == 0}
    failed = [t["id"] for t in tasks if t["id"] in first and not first[t["id"]]["correct"]]
    if len(failed) >= cfg["fail_fast"]:
        return False, f"{len(failed)} of {len(tasks)} tasks failed on the first pass"
    bad = sorted({r["task"] for r in records if r["repeat"] > 0 and not r["correct"]})
    if bad:
        return False, f"failed again on a rerun: {', '.join(bad)}"
    have = {(r["task"], r["repeat"]) for r in records}
    want = [(t["id"], 0) for t in tasks] + [(t, n) for t in failed for n in cfg["rerun_repeats"]]
    if missing := [f"{t} r{n}" for t, n in want if (t, n) not in have]:
        raise GateExit(3, f"records missing for: {', '.join(missing)}")
    return True, f"{len(failed)} first-pass failures, all passed on the reruns"


def run_gate(cfg, tasks, run_task):
    """The records. `run_task(task, repeat) -> (record, refused)`; raises GateExit when the cap
    stops a run or the key is unusable, before anything else is run."""
    records = []

    def go(task, repeat):
        rec, refused = run_task(task, repeat)
        records.append(rec)
        if why := key_error(rec):
            raise GateExit(3, why)
        if refused:
            raise GateExit(2, refused)
        return rec

    for task in tasks:
        go(task, 0)
    failed = [t for t in tasks if not records[tasks.index(t)]["correct"]]
    if len(failed) >= cfg["fail_fast"]:
        return records  # broad: reruns would only cost money
    for task in failed:
        for repeat in cfg["rerun_repeats"]:
            if not go(task, repeat)["correct"]:
                return records  # the gate has failed; the rest would only cost money
    return records


def check_written(path, records):
    """The records read back from runs.jsonl must be the ones the gate decided on."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        written = [(r["task"], r["repeat"]) for r in map(json.loads, text.splitlines())]
    except (OSError, ValueError, KeyError):
        written = None
    if written != [(r["task"], r["repeat"]) for r in records]:
        raise GateExit(3, f"{Path(path).name} does not hold the {len(records)} runs the gate made")


def summary(records, cfg, passed, why, left):
    cost = sum(r["cost_usd"] for r in records)
    rows = []
    for task in dict.fromkeys(r["task"] for r in records):
        runs = [r for r in records if r["task"] == task]
        marks = ", ".join(f"r{r['repeat']} {'ok' if r['correct'] else r['category']}" for r in runs)
        reasons = "; ".join(
            f["reason"] for r in runs if not r["correct"] for f in r["failures"][:1]
        )
        rows.append(f"| {task} | {marks} | {reasons} |")
    return "\n".join(
        [
            f"# CI eval gate: {'passed' if passed else 'FAILED'}",
            "",
            (
                f"{why}. {cfg['model']}, {len(records)} runs, seed {cfg['seed']} + repeat, cost "
                f"${cost:.4f} of the ${cfg['run_cap']:.2f} run cap, ${left:.2f} on the key before."
            ),
            "",
            "| task | runs | why it failed |",
            "|---|---|---|",
            *rows,
            "",
        ]
    )


def gate(config_path, cfg):
    """Runs the gate; returns the exit code for a pass (0) or an accuracy failure (1)."""
    suite = load_suite(REPO_ROOT / cfg["tasks"])
    tasks = run_eval.selected(cfg, suite)
    left = preflight(cfg)
    guard = Guard(cfg["phase"], run_cap=cfg["run_cap"])
    out = run_eval.out_dir(cfg)
    shutil.copy(config_path, out / "config.yaml")
    meta = {"model": cfg["model"], "date": time.strftime("%Y-%m-%d %H:%M:%S %z"), "config": cfg,
            "tasks_sha256": suite["sha256"], "key_left_usd": left,
            **environment(Toolbox().assumptions)}  # fmt: skip
    (out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    pacer = Pacer(cfg["max_rpm"]) if cfg.get("max_rpm") else None
    clients = {}

    def run_task(task, repeat):
        if repeat not in clients:
            clients[repeat] = run_eval.make_client(cfg, cfg["seed"] + repeat, pacer)
        run_id = f"ci-{stamp}-{task['id']}-r{repeat}"
        rec, refused = run_eval.run_one(cfg, task, repeat, run_id, clients[repeat], guard, suite)
        with (out / "runs.jsonl").open("a", encoding="utf-8") as f:  # kept if a later run stops
            f.write(json.dumps(rec) + "\n")
        return rec, refused

    records = run_gate(cfg, tasks, run_task)
    check_written(out / "runs.jsonl", records)
    passed, why = decide(records, tasks, cfg)
    text = summary(records, cfg, passed, why, left)
    (out / "summary.md").write_text(text, encoding="utf-8")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    print(text)
    if not passed:
        print(f"::error::eval gate failed: {why}")
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    args = parser.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    try:
        sys.exit(gate(args.config, cfg))
    except GateExit as e:
        print(f"::error::eval gate: {e}")
        sys.exit(e.code)
    except Exception as e:  # noqa: BLE001 (a crash is not an accuracy failure: exit 3, no details)
        print(f"::error::eval gate crashed: {type(e).__name__}")
        sys.exit(3)


if __name__ == "__main__":
    main()
