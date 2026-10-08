"""The agent's tools. SCHEMAS describes them for the LLM (JSON Schema); Toolbox.call runs one by
name and always returns JSON. Circuits and results are passed between tools by id, so the LLM
never re-types a circuit or a result. See docs/agent-design.md."""

import ast
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from qiskit import qpy

from . import bicycle, surface
from .assumptions import load_assumptions
from .verify import verify

ALLOWED_IMPORTS = ("qiskit", "numpy", "math", "cmath", "fractions", "itertools", "functools")
BANNED_NAMES = {
    "eval", "exec", "compile", "open", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
}  # fmt: skip
ENV_KEEP = ("PATH", "SYSTEMROOT", "TEMP", "TMP")  # nothing else, so no API keys
TIMEOUT_S = 60
RUNNER = Path(__file__).with_name("sandbox_runner.py")
DROP = ("frontier",)  # every point on surface's Pareto frontier; long and not needed by the agent


class SandboxError(Exception):
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


def run_circuit_code(code, timeout=TIMEOUT_S, allowed=ALLOWED_IMPORTS):
    """Run code that assigns a QuantumCircuit to `circuit` in a fresh process with a minimal
    environment, sockets disabled and a timeout. Returns the circuit; raises SandboxError."""
    check_code(code, allowed)
    env = {k: os.environ[k] for k in ENV_KEEP if k in os.environ}
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "circuit.qpy"
        try:
            r = subprocess.run(
                [sys.executable, "-I", str(RUNNER), str(out)],
                input=code,
                capture_output=True,
                text=True,
                env=env,
                cwd=tmp,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise SandboxError(f"timed out after {timeout} s") from None
        if r.returncode:
            raise SandboxError(r.stderr.strip())
        with out.open("rb") as f:
            return qpy.load(f)[0]


_CIRCUIT_ID = {"type": "string", "description": "circuit_id returned by build_circuit"}
_P = {"type": "number", "description": "physical error rate (default from assumptions)"}
_BUDGET = {"type": "number", "description": "total logical error budget (default 1e-3)"}
SCHEMAS = [
    {
        "name": "build_circuit",
        "description": (
            "Run Python code that builds the problem's circuit with Qiskit and assigns it to a "
            f"variable named `circuit`. Allowed imports: {', '.join(ALLOWED_IMPORTS)}. "
            f"No files, no network, {TIMEOUT_S} s limit. Returns a circuit_id and a summary, "
            "or the error."
        ),
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Python source"}},
            "required": ["code"],
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
                "physical_error_rate": {**_P, "enum": list(bicycle.MODELS)},
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
    """One agent session: circuits and results by id, and every output the agent has seen."""

    def __init__(self, assumptions=None):
        self.assumptions = assumptions or load_assumptions()
        self.circuits, self.results, self.outputs = {}, {}, []

    def call(self, name, arguments):
        try:
            if name not in TOOLS:
                raise ValueError(f"unknown tool {name!r}")
            out = getattr(self, name)(**arguments)
        except Exception as e:  # noqa: BLE001 (any failure goes back to the agent)
            out = {"error": f"{type(e).__name__}: {e}"}
        self.outputs.append(out)
        return json.dumps(out)

    def build_circuit(self, code):
        circuit = run_circuit_code(code)
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
        r = surface.estimate_surface(self.circuits[circuit_id], a)
        return self._record("surface", circuit_id, {**r, "passes": r["error"] <= a.error_budget})

    def estimate_bicycle(self, circuit_id, code=None, physical_error_rate=None, error_budget=None):
        a = self._assumptions(
            bicycle_code=code, physical_error_rate=physical_error_rate, error_budget=error_budget
        )
        return self._record(
            "bicycle", circuit_id, bicycle.estimate_bicycle(self.circuits[circuit_id], a)
        )

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
        )
