"""Bicycle-architecture estimates from IBM's bicycle compiler and numerics (Tour de gross,
arXiv:2506.03094). See docs/bicycle-interface.md for the inputs, units and decisions."""

import csv
import io
import math
from importlib.metadata import version

from .assumptions import load_assumptions
from .compiler import compile_pbc, compiler_version, run, t_injections
from .pbc import to_pbc
from .surface import NS, PACKAGES, SYNTHESIS_SHARE

MODELS = {1e-3: "1e-3", 1e-4: "1e-4"}  # the only physical error rates bicycle_numerics models
MODULE = {"gross": (288, 90, 22), "two-gross": (576, 158, 34)}  # c, u, a: paper Table 1
FACTORY = {  # f, a' per (code, p): paper Tables 1 and 3
    ("gross", 1e-3): (454, 29),
    ("gross", 1e-4): (810, 13),
    ("two-gross", 1e-3): (463, 29),
    ("two-gross", 1e-4): (18_600, 49),
}
DISTANCE = {"gross": 12, "two-gross": 18}  # [[144,12,12]] and [[288,12,18]], paper Sec. 2.1
DATA_QUBITS_PER_MODULE = 11  # plus one pivot
LOGICAL_QUBITS_PER_MODULE = 12
COUNTS = ("t_injs", "measurements", "joint_measurements", "automorphisms", "idles")


def physical_qubits(code, p, modules):
    """Paper Eq. 25: q = M(c + u + a) - a + a' + f, a 1D chain of M modules and one factory."""
    c, u, a = MODULE[code]
    f, a_factory = FACTORY[(code, p)]
    return modules * (c + u + a) - a + a_factory + f


def _is_t(op):
    return math.isclose(abs(float(op["Rotation"]["angle"])), math.pi / 4, abs_tol=1e-12)


def estimate_bicycle(circuit, assumptions=None):
    a = assumptions or load_assumptions()
    code, p, budget = a.bicycle_code, a.physical_error_rate, a.error_budget
    if code not in MODULE:
        raise ValueError(f"unsupported bicycle code: {code}")
    if p not in MODELS:
        raise ValueError(f"bicycle supports physical error rates 1e-3 and 1e-4 only, got {p}")

    ops = to_pbc(circuit, a)
    if not ops:
        raise ValueError("circuit has no non-Clifford rotations or measurements")
    n = len(ops[0].get("Rotation", ops[0].get("Measurement"))["basis"])
    rotation_ops = [op for op in ops if "Rotation" in op]
    t_count = sum(map(_is_t, rotation_ops))
    rotations = len(rotation_ops) - t_count

    # Same synthesis rule as estimate_surface's gridsynth mode: diamond eps per rotation,
    # operator-norm eps/2 for gridsynth. Without rotations the accuracy only touches T angles.
    eps = SYNTHESIS_SHARE * budget / rotations if rotations else 0.0
    compiled = compile_pbc(a, code, ops, eps / 2 if rotations else 1e-9)
    lines = compiled.splitlines()
    if len(lines) != len(ops):
        raise RuntimeError(f"compiler returned {len(lines)} lines for {len(ops)} ops")
    synth_ts = [t_injections(s) for s, op in zip(lines, ops) if "Rotation" in op and not _is_t(op)]

    numerics = run(a, "bicycle_numerics", [str(n), f"{code}_{MODELS[p]}"], compiled)
    rows = list(csv.DictReader(io.StringIO(numerics)))
    if len(rows) != len(ops):
        raise RuntimeError(f"numerics returned {len(rows)} rows for {len(ops)} ops")
    counts = {k: sum(int(r[k]) for r in rows) for k in COUNTS}
    timesteps = int(rows[-1]["end_time"])
    instruction_error = float(rows[-1]["total_error"])  # additive, excludes synthesis
    synthesis_error = rotations * eps
    error = instruction_error + synthesis_error

    modules = math.ceil(n / DATA_QUBITS_PER_MODULE)
    qubits = physical_qubits(code, p, modules)
    runtime_ns = timesteps * a.timestep_ns
    point = {
        "physical_qubits": qubits,
        "runtime_ns": runtime_ns,
        "physical_qubit_seconds": qubits * runtime_ns * NS,
        "distance": DISTANCE[code],
        "logical_qubits": LOGICAL_QUBITS_PER_MODULE * modules,
        "ts_per_rotation": sum(synth_ts) / len(synth_ts) if synth_ts else None,
        "synthesis_error": synthesis_error,
        "error": error,
    }
    return {
        **point,
        "passes": error <= budget,  # union bound over instructions and rotations: conservative
        "error_breakdown": {
            "instructions": instruction_error,
            "synthesis": synthesis_error,
            "total": error,
            "budget": budget,
        },
        "t_count": t_count,
        "rotation_count": rotations,
        "measurements": len(ops) - len(rotation_ops),
        "synthesis_epsilon": eps if rotations else None,
        "code": code,
        "modules": modules,
        "timesteps": timesteps,
        "instruction_counts": counts,
        "min_qubits": point,
        "min_runtime": point,
        "frontier": [point],
        "assumptions": a.as_dict(),
        "versions": {
            **{pkg: version(pkg) for pkg in PACKAGES},
            "bicycle_compiler": compiler_version(a),
        },
    }
