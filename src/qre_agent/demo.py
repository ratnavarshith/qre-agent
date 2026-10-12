"""One demo run for server.py: the agent under routing policy f with the Claude fallback off,
the full injection guardrail, a per-run dollar cap and estimates one at a time. A run reports
progress through `Hooks` and stops at the next LLM call or tool call after `Hooks.cancel` is set.
See docs/demo-api.md."""

import shutil
import tempfile
import threading
import time
from pathlib import Path

from .agent import MAX_STEPS, MAX_TOKENS, environment
from .limits import RUN_CAP_USD
from .reliability import Reliable
from .routing import run_routed
from .run_control import Cancelled, Hooks, new_run_id  # noqa: F401
from .spend import BUDGETS_PATH, Guard
from .tools import Toolbox

PRIMARY, STRONG = "google/gemini-2.5-flash", "anthropic/claude-sonnet-5"  # as in the routed eval
ESTIMATE_S = 90  # one estimate; benchmarks up to n = 64 take about 1 s
ESTIMATES = ("estimate_surface", "estimate_bicycle")
ESTIMATE_LOCK = (
    threading.Lock()
)  # an estimate needs up to ~0.9 GB (child + compiler): one at a time


class Checked:
    """A client that stops the run, instead of calling, once it is cancelled. Other attributes
    (tools, seed) come from the wrapped client."""

    def __init__(self, client, hooks):
        self.client, self.hooks = client, hooks

    def __getattr__(self, name):
        return getattr(self.client, name)

    def complete(self, model, messages, max_tokens):
        self.hooks.check()
        return self.client.complete(model, messages, max_tokens)


class Session:
    """Mixin for a Toolbox: reports each tool call, stops when cancelled, and lets one estimate
    run at a time across all sessions. `hooks` is set after construction."""

    hooks = None

    def call(self, name, arguments):
        self.hooks.check()
        self.hooks.steps += 1
        self.hooks.emit({"event": "step", "n": self.hooks.steps, "tool": name})
        if name not in ESTIMATES:
            return super().call(name, arguments)
        while not ESTIMATE_LOCK.acquire(timeout=0.5):
            self.hooks.check()
        try:
            self.hooks.check()
            return super().call(name, arguments)
        finally:
            ESTIMATE_LOCK.release()


class DemoToolbox(Session, Toolbox):
    pass


def versions_of(a):
    """The package and compiler versions and the measurement tables' hashes to report."""
    env = environment(a)
    return {"versions": env["versions"], "measurement_tables": env["measurement_tables"]}


def refusal_code(refused):
    """The error code for the reason `routing.Routed.refused` gives."""
    if refused.startswith("budget exhausted"):
        return "budget_exhausted"
    if refused.startswith("in-flight budget full"):
        return "provider_busy"
    return "run_cap"  # spend.Guard's BudgetError


class Demo:
    """`client` and `strong_client` are llm.OpenRouter clients (shared by all runs); `cache` is an
    EstimateCache or None; `info` is versions_of(...), reported with every result."""

    def __init__(
        self,
        client,
        strong_client,
        info,
        cache=None,
        toolbox_cls=DemoToolbox,
        run_cap=RUN_CAP_USD,
        estimate_s=ESTIMATE_S,
        budgets_path=BUDGETS_PATH,
    ):
        self.clients, self.info, self.cache = (client, strong_client), info, cache
        self.toolbox_cls, self.run_cap, self.estimate_s = toolbox_cls, run_cap, estimate_s
        self.budgets_path = budgets_path

    def run(self, problem, run_id, hooks):
        """The terminal event: {"event": "result", ...} or {"event": "error", "code", "message"}.
        Raises Cancelled when stopped from outside."""
        work = Path(tempfile.mkdtemp(prefix="qre-demo-"))  # traces and the spend log, removed after
        start, legs = time.perf_counter(), []
        try:
            guard = Guard("demo", self.budgets_path, work / "spend.jsonl", run_cap=self.run_cap)
            reliable = Reliable(guard, None, sleep=hooks.sleep)  # no Anthropic key: no fallback

            def make_toolbox():
                hooks.check()  # a leg that was cancelled is not followed by an escalation
                legs.append(1)
                if len(legs) == 2:
                    hooks.emit({"event": "escalated"})
                t = self.toolbox_cls(cache=self.cache, timeout=self.estimate_s)
                t.hooks = hooks
                return t

            (p, s), (pm, sm) = self.clients, (PRIMARY, STRONG)
            r = run_routed(
                problem,
                (Checked(p, hooks), pm),
                (Checked(s, hooks), sm),
                reliable,
                run_id,
                make_toolbox,
                MAX_STEPS,
                MAX_TOKENS,
                work / "runs",
                guardrail=True,
                otel=False,
            )
            hooks.check()
            return self.event(r, run_id, time.perf_counter() - start)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def event(self, r, run_id, latency_s):
        if r.refused:
            return {"event": "error", "code": refusal_code(r.refused), "message": r.refused}
        run = r.run
        if run.stop_reason.startswith("error"):  # both legs failed: an api error after retries
            return {"event": "error", "code": "provider_error", "message": run.stop_reason}
        cited = [e["result_id"] for e in (run.answer or {}).get("estimates", [])]
        results = r.toolbox.results
        latest = {r["architecture"]: i for i, r in results.items()}  # the last of each
        ids = [i for i in cited if i in results] or sorted(
            latest.values(), key=lambda i: int(i[1:])
        )
        routing = {
            k: r.routing.get(k)
            for k in ("policy", "escalated", "reason", "primary_model", "strong_model", "answered_by",
                      "cost_before_switch_usd", "cost_after_switch_usd", "cache_hits",
                      "cache_misses")
        }  # fmt: skip
        return {
            "event": "result",
            "run_id": run_id,
            "answer": run.answer,  # None when the agent gave none that parsed
            "table": [results[i] for i in ids],
            "verification": run.verification,  # None without an answer; not passed is shown as such
            "verify_passed_first_try": run.verify_passed_first_try,
            "stop_reason": run.stop_reason,
            "routing": routing,
            "assumptions": r.toolbox.assumptions.as_dict(),
            "steps": run.steps,
            "cost_usd": run.totals["cost_usd"],
            "latency_s": round(latency_s, 1),
            **self.info,
        }
