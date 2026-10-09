"""The agent loop: an LLM calls the tools in tools.py for up to `max_steps` completions, then
gives a structured final answer, which the verifier checks. Every message, tool call, token
count, cost and latency goes to runs/<run_id>.jsonl. See docs/agent-design.md."""

import json
import platform
import re
import time
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from .compiler import compiler_versions
from .spend import REPO_ROOT, cost
from .surface import PACKAGES
from .tools import SCHEMAS, Toolbox
from .verify import parse_answer

RUNS_DIR = REPO_ROOT / "runs"
MAX_STEPS = 12
MAX_TOKENS = 4096
FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)

SYSTEM = """\
You estimate the fault-tolerant resources a quantum computation needs on two architectures: the \
surface code (Microsoft's QDK resource estimator) and IBM's bicycle architecture (gross or \
two-gross code, IBM's bicycle compiler).

Tools:
- build_circuit: run Qiskit code that builds the problem's circuit (with measurements) and \
assigns it to `circuit`. Returns a circuit_id. If it fails, read the error, fix the code, retry.
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


@dataclass
class Run:
    run_id: str
    stop_reason: str  # "answered" or "step_limit"
    answer: dict | None  # the parsed final answer
    verification: dict | None  # verify's {"passed", "failures"}, when there is an answer
    steps: int  # LLM calls
    totals: dict  # tokens, cost and latency over the run
    trace_path: Path


def environment(a):
    """Versions and hardware recorded with every run."""
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
):
    toolbox = toolbox or Toolbox()
    trace_path = Path(runs_dir) / f"{run_id}.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    totals = dict.fromkeys(
        ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens"), 0
    ) | {"cost_usd": 0.0, "reported_cost_usd": 0.0, "llm_s": 0.0, "tool_s": 0.0}

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
        stop_reason, steps = "step_limit", 0
        try:
            while steps < max_steps:
                steps += 1
                start = time.perf_counter()
                reply = guard.complete(client, model, messages, max_tokens)
                latency = time.perf_counter() - start
                usd = cost(
                    guard.prices[model],
                    reply.input_tokens,
                    reply.output_tokens,
                    reply.cached_tokens,
                )
                usage = {
                    "input_tokens": reply.input_tokens,
                    "cached_tokens": reply.cached_tokens,
                    "output_tokens": reply.output_tokens,
                    "reasoning_tokens": getattr(reply, "reasoning_tokens", 0),
                    "cost_usd": usd,
                    "reported_cost_usd": reply.reported_cost_usd,
                }
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
                )

                calls = message.get("tool_calls") or []
                for call in calls:
                    start = time.perf_counter()
                    name = call["function"]["name"]
                    try:
                        arguments = json.loads(call["function"]["arguments"] or "{}")
                        result = toolbox.call(name, arguments)
                    except json.JSONDecodeError as e:
                        arguments = call["function"]["arguments"]
                        result = json.dumps({"error": f"arguments are not valid JSON: {e}"})
                    tool_s = time.perf_counter() - start
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
                    stop_reason = "answered"
                    break
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
            log(
                "final",
                stop_reason=stop_reason,
                steps=steps,
                answer=answer,
                verification=verification,
                totals=totals,
            )
    return Run(run_id, stop_reason, answer, verification, steps, totals, trace_path)
