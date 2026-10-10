"""How many estimator calls the stored evals would have saved with the estimate cache. Replays
each run's tool calls in the order the runs ran: circuits are rebuilt from the logged build calls,
each estimate's cache key is computed as Toolbox computes it, and the logged result stands in for
the estimate. A call is saved when an earlier successful call had its key; failed estimates are
not cached. Counted with the cache shared within a run, within one model's eval, and across all
of them. Writes <results_dir>/config.yaml, meta.json and summary.md.

  .venv/Scripts/python scripts/cache_savings.py evals/phase3-cache.yaml
"""

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

import yaml

from qre_agent.agent import environment
from qre_agent.cache import cache_key
from qre_agent.eval import is_tool_error
from qre_agent.spend import REPO_ROOT
from qre_agent.tools import Toolbox

RUNS_DIR = REPO_ROOT / "runs"
SCOPES = ("run", "model", "all")


class Replay(Toolbox):
    """A Toolbox whose estimates return the logged result and record their cache key."""

    def __init__(self, prompt):
        super().__init__(prompt=prompt, timeout=None)
        self.logged, self.calls = None, []

    def _estimate(self, kind, fn, circuit, a):
        out = self.logged
        try:
            key = None if is_tool_error(out) else cache_key(kind, circuit, a)
        except ValueError:  # prepare() raised, as the estimate did
            key = None
        self.calls.append({"kind": kind, "key": key, "latency_s": self.latency})
        if key is None:
            raise RuntimeError(out.get("error", "prepare failed"))
        return {k: v for k, v in out.items() if k not in ("result_id", "architecture", "circuit")}


def replay(path):
    """Estimator calls of one stored run, and the build calls whose rebuilt circuit differs."""
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    tb = Replay(records[0]["task"])
    mismatches = 0
    for rec in records:
        if rec["type"] != "tool_call" or not isinstance(rec["arguments"], dict):
            continue
        name, out = rec["name"], rec["result"]
        if name.startswith("build_"):
            if is_tool_error(out):
                continue
            rebuilt = json.loads(tb.call(name, rec["arguments"]))
            mismatches += rebuilt != out
        elif name.startswith("estimate_"):
            tb.logged, tb.latency = out, rec["latency_s"]
            tb.call(name, rec["arguments"])
    return records[0]["time"], tb.calls, mismatches


def saved(runs, scope, model=None):
    """(calls saved, their logged latency) of `model`'s runs (None: all), with the cache emptied
    at each new run, model or never (`scope`)."""
    n, seconds, seen, current = 0, 0.0, set(), None
    for run in runs:
        group = {"run": run["run_id"], "model": run["model"], "all": None}[scope]
        if group != current:
            seen, current = set(), group
        for call in run["calls"]:
            if call["key"] in seen:
                if model in (None, run["model"]):
                    n, seconds = n + 1, seconds + call["latency_s"]
            elif call["key"] is not None:
                seen.add(call["key"])
    return n, seconds


def table(runs):
    rows = [
        (
            "| model (runs) | estimator calls | failed | distinct keys | saved: within a run "
            "| within the model's eval | across all evals | time saved across all |"
        ),
        "|---|---|---|---|---|---|---|---|",
    ]
    for model in [*dict.fromkeys(r["model"] for r in runs), None]:
        group = [r for r in runs if model in (None, r["model"])]
        calls = [c for r in group for c in r["calls"]]
        keys = {c["key"] for c in calls} - {None}
        cells = [saved(runs, scope, model) for scope in SCOPES]
        pct = " | ".join(f"{n} ({n / len(calls):.0%})" for n, _ in cells)
        rows.append(
            f"| {model or 'all'} ({len(group)}) | {len(calls)} | "
            f"{sum(c['key'] is None for c in calls)} | {len(keys)} | {pct} "
            f"| {cells[2][1] / 60:.1f} min |"
        )
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    args = parser.parse_args()
    env = environment(Toolbox().assumptions)  # before writing: the results are tracked
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    runs, mismatches = [], 0
    for directory in cfg["evals"]:
        for line in (REPO_ROOT / directory / "runs.jsonl").read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            start, calls, bad = replay(RUNS_DIR / f"{rec['run_id']}.jsonl")
            mismatches += bad
            runs.append({"run_id": rec["run_id"], "model": rec["model"], "start": start,
                         "task": rec["task"], "calls": calls})  # fmt: skip
            print(f"{rec['run_id']}: {len(calls)} estimator calls", file=sys.stderr)
    runs.sort(key=lambda r: r["start"])

    out = REPO_ROOT / cfg["results_dir"]
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(args.config, out / "config.yaml")
    (out / "meta.json").write_text(json.dumps(env | {"config": cfg}, indent=1) + "\n")

    calls = [c for r in runs for c in r["calls"]]
    repeats = Counter(c["key"] for c in calls if c["key"] is not None)
    lines = [
        "# Estimate cache: calls the four-model eval would have saved",
        "",
        (
            "Generated by `scripts/cache_savings.py` from the stored traces of the "
            f"{len(runs)} runs in the configs' `runs.jsonl` (reruns replace the runs they "
            "reran), in the order they ran. Circuits are rebuilt from the logged build calls and "
            "each estimate's key is computed as `Toolbox` computes it (circuit after "
            "`prepare()`, assumptions, tool versions, the measurement table for bicycle); "
            "nothing is estimated. A call is saved when an earlier successful call had its key. "
            "Failed estimates are not cached. Deterministic: no seeds. Versions, tables and "
            "hardware in `meta.json`."
        ),
        "",
        (
            "Saved calls, with the cache shared within a run, within one model's eval, or "
            "across all four (each model's row counts its own calls saved, in the order the "
            "evals ran). Time saved is the logged wall time of the saved calls. The eval ran "
            "estimates in-process; since 72832f4 each one starts a child process (about 1.7 s), "
            "which a hit also skips."
        ),
        "",
        *table(runs),
        "",
        (
            f"Most-repeated keys: {', '.join(str(n) for _, n in repeats.most_common(5))} calls. "
            f"Rebuilt circuits that differed from the logged summary: {mismatches}."
        ),
        "",
    ]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
