"""The agent's tools. SCHEMAS describes them for the LLM (JSON Schema); Toolbox.call runs one by
name and always returns JSON. Circuits and results are passed between tools by id, so the LLM
never re-types a circuit or a result. See docs/agent-design.md."""

import ast
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from qiskit import qpy
from qiskit.circuit.library import get_standard_gate_name_mapping

from . import bicycle, circuits, surface
from .assumptions import load_assumptions
from .cache import cache_key
from .verify import verify

ALLOWED_IMPORTS = ("qiskit", "numpy", "math", "cmath", "fractions", "itertools", "functools")
BANNED_NAMES = {
    "eval", "exec", "compile", "open", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
}  # fmt: skip
ENV_KEEP = ("PATH", "SYSTEMROOT", "TEMP", "TMP")  # nothing else, so no API keys
TIMEOUT_S = 60
TOOL_TIMEOUT_S = 180  # an estimate; the slowest seen in the evals took under 1 s in-process
RUNNER = Path(__file__).with_name("sandbox_runner.py")
STANDARD_GATES = set(get_standard_gate_name_mapping())
BICYCLE_P = {"1e-3": 1e-3, "1e-4": 1e-4}  # strings: Gemini allows enum on STRING only
DROP = (  # not needed by the agent: long, or provenance for the traces and the estimate cache
    "frontier",  # every point on surface's Pareto frontier
    "measurement_table_sha256",
)


class SandboxError(Exception):
    pass


class ToolTimeout(Exception):
    pass


def check_code(code, allowed=ALLOWED_IMPORTS):
    """Static check before running: allowlisted imports only, no dunder access, no eval/exec/open.
    A guardrail against mistakes, not a security boundary."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise SandboxError(f"SyntaxError: {e}") from None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = ["." * node.level + (node.module or "")]
        else:
            modules = []
        for m in modules:
            if m.split(".")[0] not in allowed:
                raise SandboxError(
                    f"import of {m!r} is not allowed (allowed: {', '.join(allowed)})"
                )
        if isinstance(node, ast.Name) and (node.id in BANNED_NAMES or node.id.startswith("__")):
            raise SandboxError(f"use of {node.id!r} is not allowed")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise SandboxError(f"access to {node.attr!r} is not allowed")


def _child(conn, fn, args):
    try:
        conn.send((True, fn(*args)))
    except Exception as e:  # noqa: BLE001 (re-raised in the parent)
        try:
            conn.send((False, e))
        except Exception:  # noqa: BLE001 (an exception that doesn't pickle)
            conn.send((False, RuntimeError(f"{type(e).__name__}: {e}")))


def run_in_child(fn, *args, timeout):
    """fn(*args) in a fresh process, killed after `timeout` seconds (ToolTimeout). Not a thread:
    a thread can't be killed, and QDK's interpreter fails when used from a second thread."""
    ctx = multiprocessing.get_context("spawn")
    receive, send = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_child, args=(send, fn, args), daemon=True)
    process.start()
    send.close()
    try:
        if not receive.poll(timeout):
            process.kill()
            raise ToolTimeout(f"{fn.__name__} timed out after {timeout} s")
        try:
            ok, value = receive.recv()
        except EOFError:  # the child died without answering
            process.join()
            raise RuntimeError(f"{fn.__name__} exited with code {process.exitcode}") from None
    finally:
        process.join()
        receive.close()
    if not ok:
        raise value
    return value


def find_reset(circuit):
    """Name of the first reset found, looking inside composite instructions; `initialize` adds
    resets. Standard gates are not opened."""
    for inst in circuit.data:
        op = inst.operation
        if op.name in ("reset", "initialize"):
            return op.name
        definition = None if op.name in STANDARD_GATES else getattr(op, "definition", None)
        if definition is not None and (found := find_reset(definition)):
            return found
    return None


def run_circuit_code(code, timeout=TIMEOUT_S, allowed=ALLOWED_IMPORTS):
    """Run code that assigns a QuantumCircuit to `circuit` in a fresh process with a minimal
    environment, sockets disabled and a timeout. Returns the circuit; raises SandboxError."""
    check_code(code, allowed)
    env = {k: os.environ[k] for k in ENV_KEEP if k in os.environ}
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "circuit.qpy"
        try:
            r = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-X",
                    "utf8",
                    str(RUNNER),
                    str(out),
                ],  # UTF-8 stdio, not cp1252
                input=code.encode("utf-8"),  # bytes: text mode would use the console code page
                capture_output=True,
                env=env,
                cwd=tmp,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise SandboxError(f"timed out after {timeout} s") from None
        if r.returncode:
            raise SandboxError(r.stderr.decode("utf-8", errors="replace").strip())
        with out.open("rb") as f:
            return qpy.load(f)[0]


BENCHMARKS = {  # what build_benchmark builds, from circuits.py
    "qft": "n-qubit quantum Fourier transform with the final swaps",
    "qpe": f"phase estimation of a phase gate with phase 2*pi*{circuits.QPE_PHASE:.4g} on its |1> "
    "eigenstate, n counting qubits (n + 1 qubits)",
    "tfim": f"1D transverse-field Ising chain of n spins, {circuits.TFIM_STEPS} Trotter steps of "
    f"dt {circuits.TFIM_DT} (J = h = 1)",
    "adder": "n-bit Cuccaro (CDKM) ripple-carry adder with carry-out (2n + 2 qubits)",
    "grover": f"Grover search on n >= 3 qubits for the all-ones state, {circuits.GROVER_ITERATIONS} "
    "iterations, multi-controlled Z as a Toffoli ladder on n - 2 ancillas (2n - 2 qubits)",
}
_CIRCUIT_ID = {"type": "string", "description": "circuit_id returned by build_circuit"}
_P = {"type": "number", "description": "physical error rate (default from assumptions)"}
_BUDGET = {"type": "number", "description": "total logical error budget (default 1e-3)"}
SCHEMAS = [
    {
        "name": "build_circuit",
        "description": (
            "Run Python code that builds the problem's circuit with Qiskit and assigns it to a "
            f"variable named `circuit`. Allowed imports: {', '.join(ALLOWED_IMPORTS)}. "
            f"No files, no network, {TIMEOUT_S} s limit. Qubits start in |0>: no reset or "
            "initialize (the bicycle compiler can't compile resets); prepare basis states with X. "
            "Returns a circuit_id and a summary, or the error."
        ),
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Python source"}},
            "required": ["code"],
        },
    },
    {
        "name": "build_benchmark",
        "description": (
            "Build a standard benchmark circuit, with measurements, and return its circuit_id and "
            "a summary. Use it only when the task's circuit is exactly one of these, including the "
            "layout; otherwise write the circuit with build_circuit: "
            + "; ".join(f"{k}: {v}" for k, v in BENCHMARKS.items())
            + ". QFT and QPE above n = 20 hit the bicycle path's angle tolerance."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "family": {"type": "string", "enum": list(BENCHMARKS)},
                "n": {"type": "integer", "description": "problem size (qubits, bits or spins)"},
            },
            "required": ["family", "n"],
        },
    },
    {
        "name": "estimate_surface",
        "description": "Surface-code resource estimate (Microsoft QDK estimator) for a circuit.",
        "parameters": {
            "type": "object",
            "properties": {
                "circuit_id": _CIRCUIT_ID,
                "physical_error_rate": _P,
                "error_budget": _BUDGET,
            },
            "required": ["circuit_id"],
        },
    },
    {
        "name": "estimate_bicycle",
        "description": (
            "Bicycle-code resource estimate (IBM bicycle compiler) for a circuit. "
            "Physical error rate must be 1e-3 or 1e-4."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "circuit_id": _CIRCUIT_ID,
                "code": {"type": "string", "enum": list(bicycle.MODULE)},
                "physical_error_rate": {
                    "type": "string",
                    "enum": list(BICYCLE_P),
                    "description": "physical error rate (default from assumptions)",
                },
                "error_budget": _BUDGET,
            },
            "required": ["circuit_id"],
        },
    },
    {
        "name": "verify",
        "description": (
            "Deterministic checks before answering: both estimates saw the same circuit, the "
            "numbers are physically sane, every number in final_answer comes from a tool "
            "output, and for a size sweep qubit-seconds grow with size. Returns passed and "
            "the failed checks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "surface_result": {
                    "type": "string",
                    "description": "result_id from estimate_surface",
                },
                "bicycle_result": {
                    "type": "string",
                    "description": "result_id from estimate_bicycle",
                },
                "final_answer": {
                    "type": "string",
                    "description": (
                        'JSON: {"summary": "<prose>", "estimates": [{"result_id": "r1", '
                        '"architecture": "surface", "physical_qubits": 140015, "runtime_ns": '
                        '1.93e6, "physical_qubit_seconds": 271}, ...]}, one estimate per '
                        "result reported, with the units and field names of the result. "
                        "Numbers may be rounded; they must agree at the precision written."
                    ),
                },
                "sweep": {
                    "type": "array",
                    "description": "for a size sweep: one entry per size",
                    "items": {
                        "type": "object",
                        "properties": {
                            "size": {"type": "number"},
                            "surface": {"type": "string", "description": "surface result_id"},
                            "bicycle": {"type": "string", "description": "bicycle result_id"},
                        },
                        "required": ["size", "surface", "bicycle"],
                    },
                },
            },
            "required": ["surface_result", "bicycle_result", "final_answer"],
        },
    },
]
TOOLS = [s["name"] for s in SCHEMAS]


class Toolbox:
    """One agent session: circuits and results by id, and every output the agent has seen.

    Tools are safe to repeat: the same call returns the same output under a new id and leaves
    earlier ids and results alone (tests/test_tools.py)."""

    def __init__(self, assumptions=None, prompt="", timeout=TOOL_TIMEOUT_S, cache=None):
        """`prompt` is the task; numbers in it count as known to verify. `timeout`: seconds an
        estimate may take; it runs in a child process that is killed then. None runs estimates
        in this process, with no limit. `cache`: an EstimateCache, or None for no caching."""
        self.assumptions = assumptions or load_assumptions()
        self.prompt = prompt
        self.timeout = timeout
        self.cache = cache
        self.cache_hit = None  # whether the last call's estimate was a cache hit (None: no cache)
        self.cache_hits = self.cache_misses = 0  # this session's estimates
        self.circuits, self.results, self.outputs = {}, {}, []

    def call(self, name, arguments):
        self.cache_hit = None
        try:
            if name not in TOOLS:
                raise ValueError(f"unknown tool {name!r}")
            out = getattr(self, name)(**arguments)
        except Exception as e:  # noqa: BLE001 (any failure goes back to the agent)
            out = {"error": f"{type(e).__name__}: {e}"}
        self.outputs.append(out)
        return json.dumps(out)

    def _estimate(self, kind, fn, circuit, a):
        if self.cache is not None:
            key = cache_key(kind, circuit, a)
            if (hit := self.cache.get(key)) is not None:
                self.cache_hit, self.cache_hits = True, self.cache_hits + 1
                return hit
            self.cache_hit, self.cache_misses = False, self.cache_misses + 1
        if self.timeout is None:
            r = fn(circuit, a)
        else:
            r = run_in_child(fn, circuit, a, timeout=self.timeout)
        return r if self.cache is None else self.cache.put(key, r)

    def build_circuit(self, code):
        circuit = run_circuit_code(code)
        if reset := find_reset(circuit):
            raise ValueError(
                f"circuit contains {reset!r}, which resets qubits; the bicycle compiler can't "
                "compile resets. Qubits start in |0>: prepare basis states with X gates instead."
            )
        return self._store(circuit)

    def build_benchmark(self, family, n):
        if family not in BENCHMARKS:
            raise ValueError(f"unknown family {family!r}, expected one of {list(BENCHMARKS)}")
        if family == "grover" and n < 3:
            raise ValueError("grover needs n >= 3")
        if n < 1:
            raise ValueError("n must be >= 1")
        return {"family": family, "n": n} | self._store(circuits.FAMILIES[family](n))

    def _store(self, circuit):
        circuit_id = f"c{len(self.circuits) + 1}"
        self.circuits[circuit_id] = circuit
        return {
            "circuit_id": circuit_id,
            "num_qubits": circuit.num_qubits,
            "num_clbits": circuit.num_clbits,
            "depth": circuit.depth(),
            "ops": dict(circuit.count_ops()),
        }

    def _assumptions(self, **overrides):
        return replace(self.assumptions, **{k: v for k, v in overrides.items() if v is not None})

    def _record(self, architecture, circuit_id, result):
        result_id = f"r{len(self.results) + 1}"
        circuit = {"circuit_id": circuit_id, "num_qubits": self.circuits[circuit_id].num_qubits}
        out = {"result_id": result_id, "architecture": architecture, "circuit": circuit}
        out |= {k: v for k, v in result.items() if k not in DROP}
        self.results[result_id] = out
        return out

    def estimate_surface(self, circuit_id, physical_error_rate=None, error_budget=None):
        a = self._assumptions(physical_error_rate=physical_error_rate, error_budget=error_budget)
        r = self._estimate("surface", surface.estimate_surface, self.circuits[circuit_id], a)
        return self._record("surface", circuit_id, {**r, "passes": r["error"] <= a.error_budget})

    def estimate_bicycle(self, circuit_id, code=None, physical_error_rate=None, error_budget=None):
        if physical_error_rate is not None:
            p = BICYCLE_P.get(physical_error_rate, physical_error_rate)
            if p not in BICYCLE_P.values():
                raise ValueError(
                    f"physical_error_rate must be '1e-3' or '1e-4', got {physical_error_rate!r}"
                )
            physical_error_rate = p
        a = self._assumptions(
            bicycle_code=code, physical_error_rate=physical_error_rate, error_budget=error_budget
        )
        r = self._estimate("bicycle", bicycle.estimate_bicycle, self.circuits[circuit_id], a)
        return self._record("bicycle", circuit_id, r)

    def _result(self, result_id, architecture):
        r = self.results[result_id]
        if r["architecture"] != architecture:
            raise ValueError(f"{result_id} is a {r['architecture']} result, not {architecture}")
        return r

    def verify(self, surface_result, bicycle_result, final_answer, sweep=()):
        points = [
            {
                "size": p["size"],
                "surface": self._result(p["surface"], "surface"),
                "bicycle": self._result(p["bicycle"], "bicycle"),
            }
            for p in sweep
        ]
        return verify(
            self._result(surface_result, "surface"),
            self._result(bicycle_result, "bicycle"),
            final_answer,
            self.outputs,
            points,
            self.prompt,
        )
