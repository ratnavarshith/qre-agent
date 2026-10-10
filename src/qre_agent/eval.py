"""The eval: tasks from evals/tasks.yaml, each run several times through the agent and graded
deterministically. `grade` scores one run and puts each failure in a category; `self_test` feeds
the grader reference answers (must pass) and corrupted ones (must fail with the right category);
`summarize` writes the report. See docs/agent-design.md (Evals)."""

import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from functools import cache
from pathlib import Path

import yaml

from . import semantics
from .agent import Run, system_prompt
from .spend import REPO_ROOT, cost
from .tools import SCHEMAS, Toolbox, run_circuit_code
from .verify import REPORTED

TASKS_PATH = REPO_ROOT / "evals" / "tasks.yaml"
TYPES = ("standard", "free-form", "ambiguous")
DIFFICULTIES = ("easy", "medium", "hard", "ambiguous")
# A failed run's category is the first of its failures in this order.
CATEGORIES = (
    "api error",  # the provider failed: not the agent's fault, reported separately
    "budget/step limit",
    "gave up",
    "wrong circuit",
    "tool misuse",
    "wrong assumptions",
    "missed budget fail",  # an estimate is over the error budget and the summary doesn't say so
    "made-up numbers",
    "unit/format",
    "reference mismatch",  # right circuit and settings, numbers differ from results.json: the
    # toolchain changed since the comparison ran, so the grader's references are stale
)
FIELDS = ("physical_qubits", "runtime_ns", "physical_qubit_seconds", "t_count")
RERUN_KEYS = ("run_id", "stop_reason", "category", "reason")  # kept from a replaced run
P_STRINGS = {1e-3: "1e-3", 1e-4: "1e-4"}  # how estimate_bicycle takes them

# Physical error rates as written: 1e-3, 1.0e-03, 1 × 10^-3, 10^{-3}, 10⁻³, 0.001, 0.1%.
P_WRITTEN = {
    1e-3: r"1(?:\.0+)?\s*[eE]\s*-\s*0*3\b|(?:1\s*[×x]\s*)?10\^\{?-3\}?|10⁻³|(?<![\d.])0\.001(?!\d)|0\.1\s*%",
    1e-4: r"1(?:\.0+)?\s*[eE]\s*-\s*0*4\b|(?:1\s*[×x]\s*)?10\^\{?-4\}?|10⁻⁴|(?<![\d.])0\.0001(?!\d)|0\.01\s*%",
}  # fmt: skip
# The value counts as the stated error rate only right after "error rate" or "p" (a few words
# allowed, no digits or clause breaks) or right before "error rate", so "an error budget of
# 0.001" doesn't state p.
P_BEFORE = re.compile(r"(?:error rate|\bp\b)[^.,;\d]{0,15}$", re.IGNORECASE)
P_AFTER = re.compile(r"^\s*(?:physical\s+)?(?:error rate|p\b)", re.IGNORECASE)
CODE_WRITTEN = {
    "two-gross": r"two[- ]gross|\[\[288,\s*12,\s*18\]\]",
    "gross": r"(?<!two-)(?<!two )\bgross\b|\[\[144,\s*12,\s*12\]\]",
}
ASSUMED = re.compile(r"assum|default", re.IGNORECASE)
FAILS = re.compile(
    r"exceed|\bfail|(?:over|above|outside|beyond) (?:the |its )?(?:error |logical error )?budget"
    r"|(?:larger|greater|higher|more) than (?:the |its )?(?:error |logical error )?budget"
    r"|not (?:within|meet|pass|fit|stay)|(?:doesn't|does not|do not|don't|cannot|can't) "
    r"(?:meet|pass|fit|stay|satisfy)|passes\W+(?:is\s+)?false|too (?:high|large)",
    re.IGNORECASE,
)
VERIFY_CATEGORY = {
    "answer_format": "unit/format",
    "fields_match": "made-up numbers",  # or unit/format when the values are off by 10^3k
    "numbers_match": "made-up numbers",
    "same_circuit": "tool misuse",
    "physical_bounds": "tool misuse",
    "grows_with_size": "tool misuse",
}


# ---- tasks ----


def load_suite(path=TASKS_PATH):
    """The task file plus the comparison rows its standard and ambiguous tasks are graded on."""
    path = Path(path)
    suite = yaml.safe_load(path.read_text(encoding="utf-8"))
    suite["path"] = path
    suite["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    results = json.loads((REPO_ROOT / suite["results"]).read_text(encoding="utf-8"))
    suite["rows"] = results["rows"]
    suite["results_meta"] = results["meta"]
    return suite


def expected_rows(suite, family, n, p, code):
    """{"surface": row, "bicycle": row} from results.json, or None if it has no such rows."""
    found = {}
    for row in suite["rows"]:
        if (row["family"], row["n"], row["p"]) == (family, n, p):
            if row["arch"] == "surface":
                found["surface"] = row
            elif row["arch"] == code:
                found["bicycle"] = row
    return found if len(found) == 2 else None


@cache
def reference_circuit(path):
    return run_circuit_code(Path(path).read_text(encoding="utf-8"))


def reference_path(suite, task_id, wrong=False):
    return REPO_ROOT / suite["references"] / f"{task_id}{'_wrong' if wrong else ''}.py"


# ---- grading ----


def stated_p(summary):
    """Physical error rates the summary states as such."""
    found = set()
    for p, pattern in P_WRITTEN.items():
        for m in re.finditer(pattern, summary):
            if P_BEFORE.search(summary[: m.start()]) or P_AFTER.match(summary[m.end() :]):
                found.add(p)
    return found


def states(key, value, summary):
    """The summary states this value of an assumption (p, code or n)."""
    if key == "p":
        return value in stated_p(summary)
    if key == "code":
        return bool(re.search(CODE_WRITTEN[value], summary, re.IGNORECASE))
    sizes = rf"(?<![\d.,]){value}(?:\s*|-)(?:qubits?|bits?|spins?|sites?|counting)"
    return bool(re.search(rf"{sizes}|\bn\s*=\s*{value}\b", summary, re.IGNORECASE))


def used_settings(toolbox, cited):
    """What the cited results were computed with."""
    s, b = cited["surface"], cited["bicycle"]
    circuit_id = s["circuit"]["circuit_id"]
    built = next((o for o in toolbox.outputs if o.get("circuit_id") == circuit_id), {})
    return {
        "built": built,
        "family": built.get("family"),
        "n": built.get("n"),
        "p": b["assumptions"]["physical_error_rate"],
        "p_surface": s["assumptions"]["physical_error_rate"],
        "code": b["assumptions"]["bicycle_code"],
        "budgets": (s["assumptions"]["error_budget"], b["assumptions"]["error_budget"]),
    }


def circuit_failures(task, toolbox, cited, used, suite):
    """(category, reason) for a circuit that isn't the one the task asks for."""
    if task["type"] == "free-form":
        circuit = toolbox.circuits[cited["surface"]["circuit"]["circuit_id"]]
        reference = reference_circuit(reference_path(suite, task["id"]))
        checks = []
        if spec := task.get("semantic"):
            args = {k: v for k, v in spec.items() if k not in ("family", "n")}
            checks.append(lambda: getattr(semantics, spec["family"])(circuit, spec["n"], **args))
        if task.get("state"):
            checks.append(lambda: semantics.same_state(circuit, reference))
        if tol := task.get("counts"):
            checks.append(lambda: semantics.counts_close_to(circuit, reference, tol))
        out = []
        for check in checks:
            try:
                reason = check()
            except Exception as e:  # noqa: BLE001 (a malformed circuit fails the check)
                reason = f"{type(e).__name__}: {e}"
            if reason:
                out.append(("wrong circuit", reason))
        return out
    if used["family"] is None:
        return [("tool misuse", "the circuit came from build_circuit, not build_benchmark")]
    want = task["params"]
    out = []
    if used["family"] != want["family"]:
        out.append(("wrong circuit", f"family {used['family']}, task asks for {want['family']}"))
    if "n" in want and used["n"] != want["n"]:
        out.append(("wrong circuit", f"size {used['n']}, task asks for {want['n']}"))
    return out


def _unit_slip(answer, results):
    """Some reported field is off from its result by a power of 1000 (ms written as ns, ...)."""
    for e in answer["estimates"]:
        r = results.get(e.get("result_id"), {})
        for field in REPORTED:
            got, want = e.get(field), r.get(field)
            if isinstance(got, (int, float)) and got > 0 and want:
                k = math.log10(want / got) / 3
                if round(k) != 0 and abs(k - round(k)) < 0.01:
                    return True
    return False


def grade(task, toolbox, run, suite, last_message=None):
    """{"correct", "category", "failures": [{"category", "reason"}]} for one run. `last_message`
    is the last assistant message, used to tell giving up from running out of steps."""
    failures = []

    def fail(category, reason):
        failures.append({"category": category, "reason": reason})

    if run.stop_reason.startswith("error"):
        budget = "BudgetError" in run.stop_reason
        fail("budget/step limit" if budget else "api error", run.stop_reason)
    elif run.answer is None:
        last = last_message or {}
        if last.get("tool_calls"):
            fail("budget/step limit", f"no answer within {run.steps} steps")
        elif "estimates" in (last.get("content") or ""):
            fail("unit/format", "the final answer never parsed as the required JSON")
        else:
            fail("gave up", f"stopped without an answer: {(last.get('content') or '')[:200]!r}")
    else:
        summary = run.answer["summary"]
        cited = {}
        for e in run.answer["estimates"]:
            r = toolbox.results[e["result_id"]]
            cited[r["architecture"]] = r
        used = used_settings(toolbox, cited)
        for category, reason in circuit_failures(task, toolbox, cited, used, suite):
            fail(category, reason)

        want = task["params"]
        for key, got in (("p", used["p"]), ("code", used["code"])):
            if key in want and got != want[key]:
                fail("wrong assumptions", f"bicycle {key} {got}, the task gives {want[key]}")
        if used["p_surface"] != used["p"]:
            fail("wrong assumptions", f"surface p {used['p_surface']}, bicycle p {used['p']}")
        if any(b != suite["error_budget"] for b in used["budgets"]):
            fail("wrong assumptions", f"error budgets {used['budgets']}, expected the default")
        assumed = {key: used[key] for key in task.get("assume", ())}
        if assumed and not ASSUMED.search(summary):
            fail("wrong assumptions", "the summary doesn't say it assumed anything")
        for key, value in assumed.items():
            if not states(key, value, summary):
                fail("wrong assumptions", f"the summary doesn't state the {key} used ({value})")

        if task["type"] != "free-form" and not failures:
            settings = {k: want.get(k, assumed.get(k)) for k in ("family", "n", "p", "code")}
            rows = expected_rows(suite, **settings)
            for arch, row in (rows or {}).items():
                for field in FIELDS:
                    got = cited[arch][field]
                    if not math.isclose(got, row[field], rel_tol=suite["rel_tol"]):
                        fail(
                            "reference mismatch", f"{arch} {field} {got}, results.json {row[field]}"
                        )

        for arch, r in cited.items():
            if not r["passes"] and not FAILS.search(summary):
                fail("missed budget fail", f"{arch} error {r['error']:.3g} is over the budget")

        verification = run.verification or {"passed": False, "failures": []}
        for f in verification["failures"]:
            category = VERIFY_CATEGORY.get(f["check"], "tool misuse")
            if f["check"] == "fields_match" and _unit_slip(run.answer, toolbox.results):
                category = "unit/format"
            fail(category, f"verify {f['check']}: {f['reason']}")

    category = min((f["category"] for f in failures), key=CATEGORIES.index, default=None)
    return {"correct": not failures, "category": category, "failures": failures}


# ---- one run ----


def is_tool_error(output):
    """Toolbox.call's {"error": "..."}; results have a numeric "error" (their logical error)."""
    return isinstance(output.get("error"), str)


def read_trace(path):
    records = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
    final = next((r for r in records if r["type"] == "final"), None)
    calls = [r for r in records if r["type"] == "llm_call"]
    return final, (calls[-1]["message"] if calls else None)


def run_from_trace(run_id, path):
    """A Run rebuilt from a trace, for a run that raised (the trace's final record is written
    even then)."""
    final, _ = read_trace(path)
    return Run(
        run_id,
        final["stop_reason"],
        final["answer"],
        final["verification"],
        final["steps"],
        final["totals"],
        Path(path),
        final["verify_passed_first_try"],
        final["verify_passed_after_retry"],
    )


def record(task, toolbox, run, grading, model, seed, repeat, wall_s, rerun_of=None, paced=0.0):
    """One line of runs.jsonl. `rerun_of` is the earlier record this run replaces (with a
    `reason` when it wasn't rerun for an api error);
    `paced` is the time the request pacer made it wait, which is left out of the latencies."""
    t = run.totals
    return {
        "task": task["id"],
        "type": task["type"],
        "difficulty": task["difficulty"],
        "model": model,
        "repeat": repeat,
        "seed": seed,
        "run_id": run.run_id,
        "stop_reason": run.stop_reason,
        "correct": grading["correct"],
        "category": grading["category"],
        "failures": grading["failures"],
        "verify_first_try": run.verify_passed_first_try,
        "verify_after_retry": run.verify_passed_after_retry,
        "tool_errors": sum(map(is_tool_error, toolbox.outputs)),
        "steps": run.steps,
        "input_tokens": t.get("input_tokens", 0),
        "cached_tokens": t.get("cached_tokens", 0),
        "cache_write_tokens": t.get("cache_write_tokens", 0),
        "output_tokens": t.get("output_tokens", 0),
        "reasoning_tokens": t.get("reasoning_tokens", 0),
        "cost_usd": t.get("cost_usd", 0.0),
        "reported_cost_usd": t.get("reported_cost_usd", 0.0),
        "latency_s": wall_s - paced,
        "paced_wait_s": paced,
        "llm_s": t.get("llm_s", 0.0) - paced,
        "tool_s": t.get("tool_s", 0.0),
    } | ({"rerun_of": {k: rerun_of[k] for k in RERUN_KEYS if k in rerun_of}} if rerun_of else {})


def _api_error(stop_reason):
    return stop_reason.startswith("error") and "BudgetError" not in stop_reason


def api_error_counts(records):
    """(runs that were api errors on the first attempt, runs that still are): a rerun record
    carries `rerun_of`, the first attempt it replaced, which may have been rerun for another
    reason."""
    first = sum(
        _api_error(r["rerun_of"]["stop_reason"])
        if "rerun_of" in r
        else r["category"] == "api error"
        for r in records
    )
    return first, sum(r["category"] == "api error" for r in records)


def replace_reruns(records, reruns):
    """The records, with each run replaced by its rerun (same task and repeat), in order."""
    by_run = {(r["task"], r["repeat"]): r for r in reruns}
    return [by_run.get((r["task"], r["repeat"]), r) for r in records]


# ---- cost estimates ----

# Worst-case growth of the prompt per step, as in scripts/run_trial.py: a reply that fills
# max_tokens at OUT_CHARS chars per token, plus TOOL_CALLS tool results of TOOL_CHARS chars each.
OUT_CHARS, TOOL_CALLS, TOOL_CHARS = 6, 3, 4000


def worst_case(prompt, price, max_steps, max_tokens):
    """Dollars for one run if it uses every step and each call hits the guard's worst case
    (every prompt character a token, every reply max_tokens)."""
    tools = len(json.dumps([{"type": "function", "function": s} for s in SCHEMAS]))
    system = system_prompt(Toolbox().assumptions)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    base = len(json.dumps(messages)) + tools
    grow = OUT_CHARS * max_tokens + TOOL_CALLS * TOOL_CHARS
    return sum(
        cost(price, base + k * grow, max_tokens, written_tokens=base + k * grow)
        for k in range(max_steps)
    )


def past_runs(model, runs_dir=REPO_ROOT / "runs"):
    """Steps and token totals of each earlier run of this model, from its traces' final records."""
    runs = []
    for path in Path(runs_dir).glob("*.jsonl"):
        if path.name == "spend.jsonl":
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            continue
        meta, last = json.loads(lines[0]), json.loads(lines[-1])
        if meta.get("model") == model and last.get("type") == "final":
            runs.append({"steps": last["steps"], **last["totals"]})
    return runs


def expected_cost(price, past, ratio=1.0, prefix=0):
    """Mean cost of a run, from the token totals of earlier runs `past` of a profile model.
    For another model, input tokens are scaled by `ratio` (its prompt tokens over the profile
    model's: tokenizers differ) and output tokens are kept. `prefix` is the number of prompt
    tokens (tools and system prompt) the model reads from its cache on every step; it is
    capped at the whole prompt. With ratio 1 and no prefix the profile's own cached tokens
    are used."""
    costs = []
    for t in past:
        tokens = t["input_tokens"] * ratio
        if ratio == 1 and not prefix:
            cached = t["cached_tokens"]
        else:
            cached = min(prefix * t["steps"], tokens)
        costs.append(cost(price, tokens, t["output_tokens"], cached))
    return statistics.mean(costs)


def stop_reason(records, expected_total, stop):
    """Why the eval should stop and ask, or None: the cost so far is over `cost_factor` times
    the expected cost of the whole eval, or, once `min_runs` runs are done, more than
    `api_error_rate` of the runs are api errors."""
    spent = sum(r["cost_usd"] for r in records)
    if spent > stop["cost_factor"] * expected_total:
        return (
            f"cost ${spent:.4f} is over {stop['cost_factor']:g}x the expected "
            f"${expected_total:.4f} after {len(records)} runs"
        )
    errors = sum(r["category"] == "api error" for r in records)
    if len(records) >= stop["min_runs"] and errors / len(records) > stop["api_error_rate"]:
        return (
            f"{errors} of {len(records)} runs are api errors (limit {stop['api_error_rate']:.0%})"
        )
    return None


# ---- grader self-test ----


def _desc(task, n):
    if task["type"] == "free-form":
        return "The circuit"
    return f"The {n}-qubit {task['params']['family']} benchmark" if n else "The benchmark"


def _estimate(task, suite, settings, wrong_circuit=False):
    """A Toolbox holding one circuit estimated on both architectures with these settings, as
    the agent would have built it."""
    toolbox = Toolbox(prompt=task["prompt"])

    def call(name, **arguments):  # through Toolbox.call, so outputs are kept as in a real run
        out = json.loads(toolbox.call(name, arguments))
        if is_tool_error(out):
            raise RuntimeError(f"{task['id']}: {name} failed: {out['error']}")
        return out

    if task["type"] == "free-form":
        code = reference_path(suite, task["id"], wrong_circuit).read_text(encoding="utf-8")
        circuit_id = call("build_circuit", code=code)["circuit_id"]
    else:
        circuit_id = call("build_benchmark", family=settings["family"], n=settings["n"])
        circuit_id = circuit_id["circuit_id"]
    p = settings["p"]
    s = call("estimate_surface", circuit_id=circuit_id, physical_error_rate=p)
    b = call(
        "estimate_bicycle",
        circuit_id=circuit_id,
        code=settings["code"],
        physical_error_rate=P_STRINGS[p],
    )
    return toolbox, toolbox.results[s["result_id"]], toolbox.results[b["result_id"]]


def _answer(task, s, b, said, fail_note=True, extra="", runtime_scale=1):
    """A final answer reporting results s and b. `said` holds the settings the summary states
    ({} states none); `fail_note` says when a result is over budget."""
    parts = []
    if "p" in said:
        parts.append(f"a physical error rate of {P_STRINGS[said['p']]}")
    if said:
        parts.append(f"an error budget of {s['assumptions']['error_budget']:g}")
    if "code" in said:
        parts.append(f"the {said['code']} code")
    summary = _desc(task, said.get("n")) + (f", assuming {', '.join(parts)}" if said else "")
    summary += (
        f": the surface code needs {s['physical_qubits']} physical qubits for "
        f"{s['runtime_ns']} ns ({s['physical_qubit_seconds']} physical qubit-seconds) and the "
        f"bicycle code needs {b['physical_qubits']} physical qubits for {b['runtime_ns']} ns "
        f"({b['physical_qubit_seconds']} physical qubit-seconds)."
    )
    if fail_note:
        for name, r in (("surface", s), ("bicycle", b)):
            if not r["passes"]:
                summary += f" The {name} estimate exceeds the error budget (error {r['error']})."
    estimates = [
        {
            "result_id": r["result_id"],
            "architecture": r["architecture"],
            "physical_qubits": r["physical_qubits"],
            "runtime_ns": r["runtime_ns"] * runtime_scale,
            "physical_qubit_seconds": r["physical_qubit_seconds"],
        }
        for r in (s, b)
    ]
    return {"summary": summary + extra, "estimates": estimates}


def _grade_answer(task, suite, toolbox, s, b, answer):
    verification = toolbox.verify(s["result_id"], b["result_id"], json.dumps(answer))
    run = Run("self-test", "answered", answer, verification, 0, {}, None, verification["passed"])
    return grade(task, toolbox, run, suite)


def _other(value, choices):
    return next(c for c in choices if c != value)


def self_test(task, suite):
    """Grade a reference answer (must be correct) and corrupted answers (each must fail, with
    the category given). Returns [{"variant", "expected", "correct", "category", "reasons"}]."""
    params = task["params"]
    settings = {**params, **task.get("reference", {})}
    said = {k: settings[k] for k in ("p", "code", "n") if k in settings}
    out = []

    def check(variant, expected, toolbox, s, b, answer):
        g = _grade_answer(task, suite, toolbox, s, b, answer)
        reasons = [f"{f['category']}: {f['reason']}" for f in g["failures"]]
        out.append(
            {"variant": variant, "expected": expected, "correct": g["correct"],
             "category": g["category"], "reasons": reasons}
        )  # fmt: skip

    reference = _estimate(task, suite, settings)
    _, s, b = reference
    check("reference", None, *reference, _answer(task, s, b, said))
    if not (s["passes"] and b["passes"]):
        check("budget fail unstated", "missed budget fail", *reference,
              _answer(task, s, b, said, fail_note=False))  # fmt: skip
    ratio = s["physical_qubits"] / b["physical_qubits"]
    made_up = f" That is {ratio:.4g} times fewer physical qubits on bicycle."
    check("made-up ratio", "made-up numbers", *reference, _answer(task, s, b, said, extra=made_up))
    check("runtime in ms", "unit/format", *reference,
          _answer(task, s, b, said, runtime_scale=1e-6))  # fmt: skip

    if task["type"] == "free-form":
        wrong = _estimate(task, suite, settings, wrong_circuit=True)
        check("wrong circuit", "wrong circuit", *wrong, _answer(task, *wrong[1:], said))
    elif "n" in params:
        sizes = sorted({r["n"] for r in suite["rows"] if r["family"] == params["family"]})
        wrong = _estimate(task, suite, {**settings, "n": _other(params["n"], sizes)})
        check("wrong size", "wrong circuit", *wrong, _answer(task, *wrong[1:], said))
    if "p" in params:
        wrong = _estimate(task, suite, {**settings, "p": _other(params["p"], P_STRINGS)})
        check("wrong p", "wrong assumptions", *wrong, _answer(task, *wrong[1:], said))
    if "code" in params:
        wrong = _estimate(task, suite, {**settings, "code": _other(params["code"], CODE_WRITTEN)})
        check("wrong code", "wrong assumptions", *wrong, _answer(task, *wrong[1:], said))

    if task["type"] == "ambiguous":
        alternative = {**params, **task["alternative"]}
        alt = _estimate(task, suite, alternative)
        alt_said = {k: alternative[k] for k in ("p", "code", "n") if k in alternative}
        check("alternative assumption", None, *alt, _answer(task, *alt[1:], alt_said))
        given = {k: v for k, v in said.items() if k not in task["assume"]}
        check("assumption unstated", "wrong assumptions", *reference,
              _answer(task, s, b, given))  # fmt: skip
        misstated = said | {k: task["alternative"][k] for k in task["assume"]}
        check("assumption misstated", "wrong assumptions", *reference,
              _answer(task, s, b, misstated))  # fmt: skip
    for row in out:
        row["ok"] = row["correct"] if row["expected"] is None else (
            not row["correct"] and row["category"] == row["expected"]
        )  # fmt: skip
    return out


# ---- report ----


def _spread(values):
    """mean ± sample std (min–max) over repeats, as percentages of the fraction."""
    if not values:
        return "-"
    if len(values) == 1:
        return f"{100 * values[0]:.0f}%"
    return (
        f"{100 * statistics.mean(values):.0f}% ± {100 * statistics.stdev(values):.0f} "
        f"({100 * min(values):.0f}–{100 * max(values):.0f})"
    )


def _rates(records, key):
    """The fraction of runs with `key` true, one value per repeat."""
    by_repeat = defaultdict(list)
    for r in records:
        by_repeat[r["repeat"]].append(bool(key(r)))
    return [sum(v) / len(v) for _, v in sorted(by_repeat.items())]


def _mean_sd(values, fmt="{:.2f}"):
    if not values:
        return "-"
    if len(values) == 1:
        return fmt.format(values[0])
    return f"{fmt.format(statistics.mean(values))} ± {fmt.format(statistics.stdev(values))}"


def summarize(records, meta):
    """summary.md: correct and verify rates with their spread across repeats, per type and
    difficulty; cost, tokens, steps and latency; failure counts by category; per-task results."""
    repeats = sorted({r["repeat"] for r in records})
    first, left = api_error_counts(records)
    graded = [r for r in records if r["category"] != "api error"]
    lines = [
        f"# Eval: {meta['model']}, {meta['date']}",
        "",
        f"{len({r['task'] for r in records})} tasks × {len(repeats)} repeats = {len(records)} "
        f"runs; seeds {sorted({r['seed'] for r in records})} (repeat i uses seed "
        f"{meta['config']['seed']} + i). Task file `{meta['tasks']}` (sha256 "
        f"{meta['tasks_sha256'][:12]}). Versions: "
        + ", ".join(f"{k} {v}" for k, v in meta["versions"].items())
        + f". Git commit {(meta.get('git') or {}).get('commit')}, dirty: "
        f"{(meta.get('git') or {}).get('dirty')}. Hardware: {meta['hardware']['processor']}, {meta['hardware']['platform']}, "
        f"Python {meta['hardware']['python']}.",
        "",
        (
            "Rates are mean ± sample standard deviation across repeats (min–max), each repeat "
            "being one pass over the tasks. Correct = the task's checks and verify both pass. "
            "Verify (any) = passed on the first try or after the one retry. Runs that hit an API "
            f"error are left out of the rates ({len(records) - len(graded)} here)."
        ),
        "",
        (
            f"API errors (no retries): {first} of {len(records)} runs on the first attempt, "
            f"{left} after rerunning them once."
        ),
        "",
        "| group | tasks | correct | verify first try | verify (any) | steps | tool errors |",
        "|---|---|---|---|---|---|---|",
    ]
    groups = [("all", graded)]
    groups += [(t, [r for r in graded if r["type"] == t]) for t in TYPES]
    # the "ambiguous" difficulty is the ambiguous type, already listed
    groups += [(d, [r for r in graded if r["difficulty"] == d]) for d in DIFFICULTIES[:-1]]
    for name, rs in groups:
        if not rs:
            continue
        lines.append(
            f"| {name} | {len({r['task'] for r in rs})} "
            f"| {_spread(_rates(rs, lambda r: r['correct']))} "
            f"| {_spread(_rates(rs, lambda r: r['verify_first_try']))} "
            f"| {_spread(_rates(rs, lambda r: r['verify_first_try'] or r['verify_after_retry']))} "
            f"| {_mean_sd([r['steps'] for r in rs], '{:.1f}')} "
            f"| {_mean_sd([r['tool_errors'] for r in rs], '{:.1f}')} |"
        )

    per_repeat = [[r for r in records if r["repeat"] == i] for i in repeats]
    lines += [
        "",
        "## Cost and time",
        "",
        "Per run: mean ± sd over all runs. Per repeat: one pass over the tasks.",
        "",
        "| | value |",
        "|---|---|",
        f"| total cost (ours) | ${sum(r['cost_usd'] for r in records):.4f} |",
        f"| total cost (OpenRouter) | ${sum(r['reported_cost_usd'] or 0 for r in records):.4f} |",
        f"| cost per repeat | ${_mean_sd([sum(r['cost_usd'] for r in rs) for rs in per_repeat], '{:.4f}')} |",
        f"| cost per run | ${_mean_sd([r['cost_usd'] for r in records], '{:.5f}')} |",
        f"| input tokens per run | {_mean_sd([r['input_tokens'] for r in records], '{:.0f}')} |",
        f"| cached tokens per run | {_mean_sd([r['cached_tokens'] for r in records], '{:.0f}')} |",
        f"| cache-written tokens per run | {_mean_sd([r.get('cache_write_tokens', 0) for r in records], '{:.0f}')} |",
        f"| output tokens per run | {_mean_sd([r['output_tokens'] for r in records], '{:.0f}')} |",
        f"| latency per run (s) | {_mean_sd([r['latency_s'] for r in records], '{:.1f}')} |",
        f"| of which LLM (s) | {_mean_sd([r['llm_s'] for r in records], '{:.1f}')} |",
        f"| of which tools (s) | {_mean_sd([r['tool_s'] for r in records], '{:.1f}')} |",
        "",
        "## Failures",
        "",
        "Each failed run counted once, under its first category in this order: "
        + ", ".join(CATEGORIES)
        + ".",
        "",
        "| category | " + " | ".join(TYPES) + " | total |",
        "|---|" + "---|" * (len(TYPES) + 1),
    ]
    counts = Counter((r["category"], r["type"]) for r in records if r["category"])
    for c in CATEGORIES:
        row = [counts[(c, t)] for t in TYPES]
        if sum(row):
            lines.append(f"| {c} | " + " | ".join(map(str, row)) + f" | {sum(row)} |")
    if not counts:
        lines.append("| (none) |" + " |" * (len(TYPES) + 1))

    lines += ["", "## Per task", "", "| task | type | difficulty | correct | failures |",
              "|---|---|---|---|---|"]  # fmt: skip
    by_task = defaultdict(list)
    for r in records:
        by_task[r["task"]].append(r)
    for task, rs in by_task.items():
        reasons = "; ".join(
            f"r{r['repeat']} {r['category']}: {r['failures'][0]['reason']}"
            for r in rs
            if r["category"]
        )
        reasons = reasons.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {task} | {rs[0]['type']} | {rs[0]['difficulty']} "
            f"| {sum(r['correct'] for r in rs)}/{len(rs)} | {reasons} |"
        )
    return "\n".join(lines) + "\n"
