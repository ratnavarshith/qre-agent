"""Deterministic checks on a surface vs bicycle comparison and the agent's final answer.
No LLM. Each check returns None when it passes, else the reason. See docs/agent-design.md.

The final answer is JSON: {"summary": prose, "estimates": [{"result_id", "architecture",
"physical_qubits", "runtime_ns", "physical_qubit_seconds"}, ...]}."""

import json
import math
import re
from decimal import Decimal
from itertools import pairwise

NS = 1e-9
MAX_PHYSICAL_QUBITS = 10**9
MAX_RUNTIME_NS = 10 * 365.25 * 24 * 3600 / NS  # ten years
REPORTED = ("physical_qubits", "runtime_ns", "physical_qubit_seconds")
# 1,346.7  19.5  4.64e-04  1.2 × 10^-3  10^-3; not inside words (r1, c2) or after a decimal point.
# A time unit right after the number (1.93 ms, 1.93ms) is captured as `u`.
NUMBER = re.compile(
    r"(?<![\w.])(?P<m>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:[eE](?P<e>[+-]?\d+)|\s*[×x]\s*10\^(?P<x>[+-]?\d+)|\^(?P<p>[+-]?\d+))?"
    r"(?:\s?(?P<u>ms|[µμ]s|us|s)(?!\w))?"
)
UNIT_SHIFT = {"ms": -6, "µs": -3, "μs": -3, "us": -3, "s": -9}  # power of ten from ns to the unit


def same_circuit(surface, bicycle):
    """Both sides estimated the same circuit and counted the same T gates and rotations.
    Not the estimators' logical_qubits: those include each layout's overhead and differ."""
    for key in ("circuit", "t_count", "rotation_count"):
        if surface[key] != bicycle[key]:
            return f"{key} differs: surface {surface[key]} vs bicycle {bicycle[key]}"
    return None


def physical_bounds(result):
    qs, qubits, runtime = (
        result["physical_qubit_seconds"],
        result["physical_qubits"],
        result["runtime_ns"],
    )
    name = result.get("architecture", "result")
    if not (math.isfinite(qs) and qs > 0):
        return f"{name}: physical-qubit-seconds {qs} is not positive and finite"
    if not math.isclose(qs, qubits * runtime * NS, rel_tol=1e-9):
        return f"{name}: physical-qubit-seconds {qs} != qubits {qubits} x runtime {runtime} ns"
    if not result["circuit"]["num_qubits"] <= result["logical_qubits"] < qubits:
        return (
            f"{name}: expected circuit qubits {result['circuit']['num_qubits']} <= logical "
            f"qubits {result['logical_qubits']} < physical qubits {qubits}"
        )
    if qubits > MAX_PHYSICAL_QUBITS or runtime > MAX_RUNTIME_NS:
        return f"{name}: {qubits} qubits for {runtime} ns is beyond the sanity bounds"
    return None


def _written(match):
    m = match["m"].replace(",", "")
    if match["e"] or match["x"]:
        return Decimal(f"{m}E{match['e'] or match['x']}")
    if match["p"]:
        return Decimal(m) ** int(match["p"])
    return Decimal(m)


def _close(written, value):
    """value equals written at the precision written: 19.5 matches 19.509, 1346 not 1346.72."""
    return abs(value - written) <= Decimal(1).scaleb(written.as_tuple().exponent) / 2


def _leaves(value):
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple)):
        for v in value:
            yield from _leaves(v)
    elif isinstance(value, Decimal):
        yield value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield Decimal(repr(value))


def parse_answer(final_answer):
    """Returns (answer, None) or (None, reason). Numbers parse as Decimal to keep the
    precision written."""
    try:
        answer = json.loads(final_answer, parse_float=Decimal, parse_int=Decimal)
    except json.JSONDecodeError as e:
        return None, f"final_answer is not JSON: {e}"
    if not (
        isinstance(answer, dict)
        and isinstance(answer.get("summary"), str)
        and isinstance(answer.get("estimates"), list)
        and all(isinstance(e, dict) for e in answer["estimates"])
    ):
        return None, "final_answer must be an object with a summary string and an estimates list"
    return answer, None


def fields_match(estimates, results, required):
    """Each reported estimate equals the result it cites, field by field, at the precision
    written; the results in `required` are each reported."""
    for e in estimates:
        r = results.get(e.get("result_id"))
        if r is None:
            return f"estimate cites unknown result {e.get('result_id')!r}"
        if e.get("architecture") != r["architecture"]:
            return f"{r['result_id']} is {r['architecture']}, reported as {e.get('architecture')!r}"
        for field in REPORTED:
            if not isinstance(e.get(field), Decimal):
                return f"{r['result_id']}: {field} missing or not a number"
            if not _close(e[field], Decimal(repr(r[field]))):
                return f"{r['result_id']}: {field} reported {e[field]}, result has {r[field]}"
    cited = {e["result_id"] for e in estimates}
    missing = [r for r in required if r not in cited]
    return f"answer does not report {missing}" if missing else None


def numbers_in(text):
    return [(m[0], _written(m)) for m in NUMBER.finditer(text.replace("−", "-"))]


def numbers_match(text, outputs):
    """Every number written in the prose equals some tool output at the precision written. A
    number with a time unit right after it (ms, µs, s) must equal a tool value, which is in ns,
    converted to that unit, and never the raw value: "1.93 ms" matches 1,934,400 ns and
    "1,934,400 ms" doesn't. Without a unit the raw value is compared."""
    known = set(_leaves(outputs))

    def matches(m):
        shift = UNIT_SHIFT.get(m["u"], 0)
        return any(_close(_written(m), t.scaleb(shift)) for t in known)

    unmatched = [m[0] for m in NUMBER.finditer(text.replace("−", "-")) if not matches(m)]
    return f"numbers not in any tool output: {unmatched}" if unmatched else None


def grows_with_size(sweep):
    """Physical-qubit-seconds grows strictly with problem size on each architecture. Raw qubit
    counts are not checked: they can dip when the estimator picks a different factory."""
    sizes = [p["size"] for p in sweep]
    if len(set(sizes)) != len(sizes):
        return f"repeated sizes in sweep: {sizes}"
    points = sorted(sweep, key=lambda p: p["size"])
    for arch in ("surface", "bicycle"):
        for p, q in pairwise(points):
            x, y = p[arch]["physical_qubit_seconds"], q[arch]["physical_qubit_seconds"]
            if not x < y:
                return f"{arch}: physical-qubit-seconds {x} at size {p['size']} >= {y} at size {q['size']}"
    return None


def verify(surface, bicycle, final_answer, outputs=(), sweep=(), prompt=""):
    """`final_answer` is the JSON string described above, `outputs` the tool outputs the agent
    saw, `sweep` [{size, surface, bicycle}] and `prompt` the task: numbers written in it
    ("8-bit") count as known."""
    failures = []

    def add(check, reason):
        if reason and {"check": check, "reason": reason} not in failures:
            failures.append({"check": check, "reason": reason})

    pairs = [(surface, bicycle)] + [(p["surface"], p["bicycle"]) for p in sweep]
    for s, b in pairs:
        add("same_circuit", same_circuit(s, b))
        add("physical_bounds", physical_bounds(s))
        add("physical_bounds", physical_bounds(b))
    answer, reason = parse_answer(final_answer)
    add("answer_format", reason)
    if answer:
        results = {r["result_id"]: r for pair in pairs for r in pair}
        required = [surface["result_id"], bicycle["result_id"]]
        add("fields_match", fields_match(answer["estimates"], results, required))
        sources = [surface, bicycle, list(outputs), [p["size"] for p in sweep]]
        sources.append([n for _, n in numbers_in(prompt)])
        add("numbers_match", numbers_match(answer["summary"], sources))
    if sweep:
        add("grows_with_size", grows_with_size(sweep))
    return {"passed": not failures, "failures": failures}
