"""The agent loop: an LLM calls the tools in tools.py for up to `max_steps` completions, then
gives a structured final answer, which the verifier checks. Every message, tool call, token
count, cost and latency goes to runs/<run_id>.jsonl, and an OpenTelemetry trace of the run (a span
per LLM call and per tool call) to runs/otel/<run_id>.jsonl. See docs/agent-design.md."""

import json
import platform
import re
import subprocess
import time
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from opentelemetry import context, trace

from . import tracing
from .compiler import compiler_versions
from .spend import REPO_ROOT, cost
from .surface import PACKAGES
from .tools import SCHEMAS, Toolbox
from .verify import parse_answer

RUNS_DIR = REPO_ROOT / "runs"
MAX_STEPS = 12
MAX_TOKENS = 4096
VERIFY_RETRIES = 1  # times a failed automatic verify goes back to the agent
FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)

SYSTEM = """\
You estimate the fault-tolerant resources a quantum computation needs on two architectures: the \
surface code (Microsoft's QDK resource estimator) and IBM's bicycle architecture (gross or \
two-gross code, IBM's bicycle compiler).

Tools:
- build_benchmark: build one of our standard benchmark circuits (qft, qpe, tfim, adder, grover) \
of size n; its description says exactly what each one is (QFT with final swaps, phase \
estimation, transverse-field Ising model, ripple-carry adder, Grover search). Use it only when \
the task's circuit is exactly that benchmark (same algorithm, variant and qubit layout); \
otherwise write it with build_circuit, e.g. for another layout or qubit order, no final \
swaps, a given oracle or marked state, or another construction.
- build_circuit: for anything else, run Qiskit code that builds the problem's circuit (with \
measurements) and assigns it to `circuit`. No reset or initialize: qubits start in |0>. Returns a circuit_id. If it fails, read the error, fix the code, retry.
- estimate_surface / estimate_bicycle: estimate a circuit_id. Returns a result_id and the \
estimate: physical_qubits, runtime_ns, physical_qubit_seconds, error, passes (error within \
budget) and more.
- verify: checks a draft final answer the same way your final answer will be checked.

Always estimate the same circuit on both architectures with the same physical error rate and \
error budget, even when the user asks about only one: the answer is checked against both.

Defaults, used when the user does not give a value:
- physical error rate p = {p} (bicycle supports only 1e-3 and 1e-4)
- total logical error budget = {budget}
- bicycle: {code} code, logical timestep {timestep} ns
- surface: {gate} ns gates, {measurement} ns measurements
In the summary, state every assumption the user did not give you, e.g. "assuming a physical \
error rate of 1e-3". Say so if a result's error exceeds the budget (passes is false).

Final answer: reply with one JSON object and nothing else:
{{"summary": "<prose>", "estimates": [
  {{"result_id": "r1", "architecture": "surface", "physical_qubits": 140015, \
"runtime_ns": 1.93e6, "physical_qubit_seconds": 271}},
  {{"result_id": "r2", "architecture": "bicycle", "physical_qubits": 1226, \
"runtime_ns": 7.53e7, "physical_qubit_seconds": 92.3}}]}}
It is checked automatically:
- exactly one surface and one bicycle estimate, each citing the result_id it reports, with that \
result's field names, units and values. Rounding is fine; values must agree at the precision \
written.
- every number in the summary must equal a number in some tool output, at the precision written. \
Don't compute ratios, percentages or differences.
"""


def system_prompt(a):
    return SYSTEM.format(
        p=f"{a.physical_error_rate:g}",
        budget=f"{a.error_budget:g}",
        code=a.bicycle_code,
        timestep=a.timestep_ns,
        gate=a.gate_time_ns,
        measurement=a.measurement_time_ns,
    )


def llm_attributes(model, usage, latency, reliability):
    """Span attributes of an LLM call that returned. `reliability`: the model that answered,
    retries, whether it was the fallback, and the kinds of failures retried."""
    attributes = {
        "gen_ai.request.model": model,
        "gen_ai.response.model": reliability["model"],
        "gen_ai.usage.input_tokens": usage["input_tokens"],
        "gen_ai.usage.cached_tokens": usage["cached_tokens"],
        "gen_ai.usage.output_tokens": usage["output_tokens"],
        "cost_usd": usage["cost_usd"],
        "latency_s": latency,
        "retries": reliability["retries"],
        "fallback": reliability["fallback"],
    }
    if reliability["faults"]:
        attributes["faults"] = reliability["faults"]
    return attributes


def llm_failure(model, error, latency):
    """Span attributes of an LLM call that raised: error.type is a ProviderError's kind (timeout,
    rate_limit, ...) or the exception's name."""
    attributes = {
        "gen_ai.request.model": model,
        "error.type": getattr(error, "kind", None) or type(error).__name__,
        "latency_s": latency,
        "retries": getattr(error, "retries", 0),
        "fallback": getattr(error, "fallback", False),
    }
    if faults := getattr(error, "faults", ()):
        attributes["faults"] = list(faults)
    return attributes


def tool_attributes(result, latency):
    """Span attributes of a tool call. A failed call's output is {"error": "<Exception>: ..."}
    (estimates have a numeric `error`, the logical error); error.type is the exception name."""
    out = json.loads(result)
    attributes = {"latency_s": latency}
    if list(out) == ["error"]:
        bad_json = out["error"].startswith("arguments are not valid JSON")
        attributes["error.type"] = "JSONDecodeError" if bad_json else out["error"].split(":")[0]
    return attributes


@dataclass
class Run:
    run_id: str
    stop_reason: str  # "answered" or "step_limit"
    answer: dict | None  # the parsed final answer
    verification: dict | None  # verify's {"passed", "failures"}, when there is an answer
    steps: int  # LLM calls
    totals: dict  # tokens, cost and latency over the run
    trace_path: Path
    # Scored separately: strict = first try; lenient = first try or after the retry.
    verify_passed_first_try: bool = False
    verify_passed_after_retry: bool = False  # the first verify failed and the revision passed


def git_state(root=REPO_ROOT):
    """The checked-out commit and whether tracked files differ from it. Untracked files don't
    count: results are written to untracked directories. Both are None outside a repository."""

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        return {
            "commit": git("rev-parse", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
        }
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"commit": None, "dirty": None}


def environment(a):
    """Versions, hardware and git state recorded with every run."""
    try:
        compiler = compiler_versions(a)
    except FileNotFoundError:
        compiler = {}
    return {
        "versions": {pkg: version(pkg) for pkg in PACKAGES} | compiler,
        "hardware": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python": platform.python_version(),
        },
        "git": git_state(),
    }


def final_answer(text, toolbox):
    """(answer JSON, surface result_id, bicycle result_id, None), or (..., reason) when the reply
    is not an answer that can be verified. A ```json fence around the answer is dropped."""
    m = FENCE.match(text.strip())
    text = m[1] if m else text
    answer, reason = parse_answer(text)
    if reason:
        return text, None, None, reason
    ids = {}  # by the cited result's architecture; a mislabeled one fails verify's fields_match
    for e in answer["estimates"]:
        if e.get("result_id") not in toolbox.results:
            return text, None, None, f"estimate cites unknown result {e.get('result_id')!r}"
        ids[toolbox.results[e["result_id"]]["architecture"]] = e["result_id"]
    if len(ids) != 2 or len(answer["estimates"]) != 2:
        return text, None, None, "need exactly one surface and one bicycle estimate"
    return text, ids["surface"], ids["bicycle"], None


def run(
    task,
    client,
    guard,
    model,
    run_id,
    toolbox=None,
    max_steps=MAX_STEPS,
    max_tokens=MAX_TOKENS,
    runs_dir=RUNS_DIR,
    verify_retries=VERIFY_RETRIES,
    otel=True,
):
    """`otel`: also write the OpenTelemetry trace to <runs_dir>/otel/<run_id>.jsonl."""
    toolbox = toolbox or Toolbox()
    toolbox.prompt = task
    trace_path = Path(runs_dir) / f"{run_id}.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    totals = dict.fromkeys(
        (
            "input_tokens",
            "cached_tokens",
            "cache_write_tokens",
            "output_tokens",
            "reasoning_tokens",
        ),
        0,
    ) | {"cost_usd": 0.0, "reported_cost_usd": 0.0, "llm_s": 0.0, "tool_s": 0.0}

    spans = tracing.provider(Path(runs_dir) / "otel" / f"{run_id}.jsonl") if otel else None
    tracer = spans.get_tracer(__name__) if spans else tracing.NO_OP
    root = tracer.start_span(
        "agent_run",
        attributes={"run_id": run_id, "gen_ai.request.model": model, "phase": guard.phase or ""},
    )
    attached = context.attach(trace.set_span_in_context(root))

    with trace_path.open("w", encoding="utf-8") as f:

        def log(kind, **record):
            f.write(json.dumps({"type": kind, "time": time.time(), **record}, default=str) + "\n")
            f.flush()

        log(
            "meta",
            run_id=run_id,
            task=task,
            model=model,
            phase=guard.phase,
            max_steps=max_steps,
            max_tokens=max_tokens,
            seed=getattr(client, "seed", None),
            tools=[s["name"] for s in SCHEMAS],
            assumptions=toolbox.assumptions.as_dict(),
            **environment(toolbox.assumptions),
        )
        messages = [
            {"role": "system", "content": system_prompt(toolbox.assumptions)},
            {"role": "user", "content": task},
        ]
        for m in messages:
            log("message", message=m)

        answer = verification = None
        stop_reason, steps, verifications = "step_limit", 0, []
        try:
            while steps < max_steps:
                steps += 1
                with tracer.start_as_current_span("llm_call", attributes={"step": steps}) as span:
                    start = time.perf_counter()
                    try:
                        reply = guard.complete(client, model, messages, max_tokens)
                    except Exception as e:
                        span.set_attributes(llm_failure(model, e, time.perf_counter() - start))
                        raise
                    latency = time.perf_counter() - start
                    served = getattr(reply, "model", None) or model  # the fallback's, after one
                    usd = cost(
                        guard.prices[served],
                        reply.input_tokens,
                        reply.output_tokens,
                        reply.cached_tokens,
                        reply.cache_write_tokens,
                    )
                    usage = {
                        "input_tokens": reply.input_tokens,
                        "cached_tokens": reply.cached_tokens,
                        "cache_write_tokens": reply.cache_write_tokens,
                        "output_tokens": reply.output_tokens,
                        "reasoning_tokens": getattr(reply, "reasoning_tokens", 0),
                        "cost_usd": usd,
                        "reported_cost_usd": reply.reported_cost_usd,
                    }
                    reliability = {
                        "model": served,
                        "retries": getattr(reply, "retries", 0),
                        "fallback": getattr(reply, "fallback", False),
                        "faults": list(getattr(reply, "faults", ())),
                    }
                    span.set_attributes(llm_attributes(model, usage, latency, reliability))
                for k, v in usage.items():
                    totals[k] += v or 0
                totals["llm_s"] += latency
                message = getattr(reply, "message", None) or {
                    "role": "assistant",
                    "content": reply.text,
                }
                messages.append(message)
                log(
                    "llm_call",
                    step=steps,
                    message=message,
                    latency_s=latency,
                    finish_reason=getattr(reply, "finish_reason", None),
                    **usage,
                    **reliability,
                )

                calls = message.get("tool_calls") or []
                for call in calls:
                    start = time.perf_counter()
                    name = call["function"]["name"]
                    attributes = {"step": steps, "tool.name": name, "tool.call_id": call["id"]}
                    with tracer.start_as_current_span("tool_call", attributes=attributes) as span:
                        try:
                            arguments = json.loads(call["function"]["arguments"] or "{}")
                            result = toolbox.call(name, arguments)
                        except json.JSONDecodeError as e:
                            arguments = call["function"]["arguments"]
                            result = json.dumps({"error": f"arguments are not valid JSON: {e}"})
                        tool_s = time.perf_counter() - start
                        span.set_attributes(tool_attributes(result, tool_s))
                    totals["tool_s"] += tool_s
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                    log(
                        "tool_call",
                        step=steps,
                        id=call["id"],
                        name=name,
                        arguments=arguments,
                        result=json.loads(result),
                        latency_s=tool_s,
                    )
                if calls:
                    continue

                text, surface_id, bicycle_id, reason = final_answer(reply.text, toolbox)
                if reason is None:
                    answer = json.loads(text)
                    verification = toolbox.verify(surface_id, bicycle_id, text)
                    verifications.append(verification["passed"])
                    log(
                        "auto_verify",
                        step=steps,
                        attempt=len(verifications),
                        verification=verification,
                    )
                    if verification["passed"] or len(verifications) > verify_retries:
                        stop_reason = "answered"
                        break
                    if steps == max_steps:  # no step left to revise in
                        stop_reason = "answered"
                        break
                    feedback = {
                        "role": "user",
                        "content": "Your final answer failed verification: "
                        f"{json.dumps(verification['failures'])}. Fix it, calling tools if you "
                        "need to, and reply with only the corrected JSON object.",
                    }
                    messages.append(feedback)
                    log("message", step=steps, message=feedback)
                    continue
                feedback = {
                    "role": "user",
                    "content": f"Your final answer could not be checked: {reason}. Reply with only "
                    "the JSON object described in the instructions.",
                }
                messages.append(feedback)
                log("message", step=steps, message=feedback)
        except Exception as e:
            stop_reason = f"error: {type(e).__name__}: {e}"
            raise
        finally:
            first_try = verifications[:1] == [True]
            after_retry = len(verifications) > 1 and not verifications[0] and verifications[-1]
            log(
                "final",
                stop_reason=stop_reason,
                steps=steps,
                answer=answer,
                verification=verification,
                verify_passed_first_try=first_try,
                verify_passed_after_retry=after_retry,
                totals=totals,
            )
            root.set_attributes(
                {
                    "stop_reason": stop_reason,
                    "steps": steps,
                    "verify_passed": bool(verification and verification["passed"]),
                    "cost_usd": totals["cost_usd"],
                }
            )
            root.end()
            context.detach(attached)
            if spans:
                spans.shutdown()
    return Run(
        run_id, stop_reason, answer, verification, steps, totals, trace_path, first_try, after_retry
    )
