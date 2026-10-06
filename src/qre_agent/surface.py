"""Surface-code resource estimates on QDK's v3 estimator (qdk.qre). See docs/qdk-interface.md."""

import math
from importlib.metadata import version

from qiskit import qasm3, transpile

from qdk.qre import (
    PSSPC,
    LatticeSurgery,
    estimate,
    instruction_name,
    property_name,
    property_name_to_key,
)
from qdk.qre.application import OpenQASMApplication
from qdk.qre.instruction_ids import LATTICE_SURGERY
from qdk.qre.models import GateBased, RoundBasedFactory, SurfaceCode

from .assumptions import load_assumptions
from .compiler import compiler_versions, gridsynth_t_counts

PACKAGES = ("qdk", "qiskit")
NS = 1e-9
ROTATIONS = ("rx", "ry", "rz")
# qasm3.dumps writes |angle| < 1e-9 as 0 (qiskit pi_check eps) and v3 then drops the gate. We drop
# such rotations explicitly for both architectures and charge |θ|/2 each to the error total.
ZERO_ANGLE = 1e-9
# LitinskiTransformation (bicycle path) treats rotations within ~2.4495e-6 of a multiple of π/2 as
# Clifford (measured on qiskit 2.5.2) while v3 counts them, so such circuits are rejected.
CLIFFORD_TOL = 2.45e-6
SYNTHESIS_SHARE = 1 / 3  # legacy convention: rotation synthesis gets a third of the budget


def _logical_counts(trace):
    counts = {instruction_name(k): v for k, v in trace.gate_counts.items()}
    t = counts.pop("T", 0) + counts.pop("T_DAG", 0)
    rotations = sum(counts.pop(g, 0) for g in ("RX", "RY", "RZ"))
    measurements = sum(counts.pop(g) for g in list(counts) if g.startswith("MEAS"))
    if counts:  # e.g. CCX: not produced by the fixed basis, but never drop it silently
        raise ValueError(f"unexpected non-Clifford instructions in trace: {counts}")
    return t, rotations, measurements


def prepare(circuit, a):
    """Transpile to the fixed basis, reduce rotation angles mod 2π (a global phase only) and drop
    rotations too small to survive the QASM export. Both architectures estimate this circuit.
    Returns it with the error bound of the dropped rotations: sum of |θ|/2 (operator norm)."""
    transpiled = transpile(
        circuit,
        basis_gates=list(a.basis_gates),
        optimization_level=a.optimization_level,
        seed_transpiler=a.seed,
    )
    prepared = transpiled.copy_empty_like()
    dropped = 0.0
    for inst in transpiled.data:
        op = inst.operation
        if op.name in ROTATIONS:
            theta = math.remainder(float(op.params[0]), 2 * math.pi)
            if abs(theta) < ZERO_ANGLE:
                dropped += abs(theta) / 2
                continue
            if ZERO_ANGLE <= abs(math.remainder(theta, math.pi / 2)) < CLIFFORD_TOL:
                raise ValueError(
                    f"{op.name}({theta}) is within {CLIFFORD_TOL} of a Clifford angle; "
                    "LitinskiTransformation would drop it on the bicycle path"
                )
            op = type(op)(theta)
        prepared.append(op, inst.qubits, inst.clbits)
    return prepared, dropped


def _rotation_angles(prepared):
    """Angles phi of the non-T rotations in the exp(i·phi/2·P) convention (rz(t) = exp(-i·t/2·Z))."""
    angles = [-float(i.operation.params[0]) for i in prepared.data if i.operation.name in ROTATIONS]
    return [phi for phi in angles if not math.isclose(abs(phi), math.pi / 4, abs_tol=1e-12)]


def _native_synthesis_error(rotations, ts):
    return rotations * 2 ** ((4.86 - ts) / 0.53)  # v3's mixed-fallback fit, qre psspc.rs


def _point(entry, rotations, synthesis_error, dropped):
    props = {property_name(k): v for k, v in entry.properties.items()}
    ls = entry.source.get(LATTICE_SURGERY).instruction
    ts = props["NUM_TS_PER_ROTATION"]
    if synthesis_error is None:  # native: v3's own synthesis error is already inside entry.error
        synthesis_error, extra = _native_synthesis_error(rotations, ts), 0.0
    else:  # gridsynth: v3 ran on the remaining budget, so add our synthesis share back
        extra = synthesis_error
    extra += dropped
    return {
        "physical_qubits": entry.qubits,
        "runtime_ns": entry.runtime,
        "physical_qubit_seconds": entry.qubits * entry.runtime * NS,
        "distance": ls.get_property(property_name_to_key("DISTANCE")),
        "logical_qubits": props["LOGICAL_COMPUTE_QUBITS"],
        "ts_per_rotation": ts if rotations else None,  # arbitrary tie without rotations
        "synthesis_error": synthesis_error,
        "error": entry.error + extra,
    }


def _gridsynth_query(a, prepared, rotations):
    """Pin v3 to gridsynth's mean T count at the same epsilon the bicycle compiler uses."""
    eps = SYNTHESIS_SHARE * a.error_budget / rotations
    angles = _rotation_angles(prepared)
    if len(angles) != rotations:
        raise ValueError(f"found {len(angles)} rotation angles, v3 counted {rotations}")
    counts = gridsynth_t_counts(a, angles, eps / 2)  # operator norm eps/2 bounds diamond eps
    ts = math.ceil(sum(counts) / len(counts))
    return PSSPC.q(num_ts_per_rotation=[ts]) * LatticeSurgery.q(), eps


def estimate_surface(circuit, assumptions=None):
    a = assumptions or load_assumptions()
    if a.qec != "surface_code":
        raise ValueError(f"unsupported qec scheme: {a.qec}")
    if a.synthesis not in ("gridsynth", "native"):
        raise ValueError(f"unsupported synthesis mode: {a.synthesis}")

    prepared, dropped = prepare(circuit, a)
    app = OpenQASMApplication(qasm3.dumps(prepared))
    t_count, rotations, measurements = _logical_counts(app.get_trace())

    arch = GateBased(
        error_rate=a.physical_error_rate,
        gate_time=a.gate_time_ns,
        measurement_time=a.measurement_time_ns,
        two_qubit_gate_time=a.two_qubit_gate_time_ns,
    )
    trace_query, eps, synthesis_error = None, None, None
    if a.synthesis == "gridsynth" and rotations:
        trace_query, eps = _gridsynth_query(a, prepared, rotations)
        synthesis_error = rotations * eps
    sc = SurfaceCode.q(code_cycle_override=a.code_cycle_ns)
    table = estimate(
        app,
        arch,
        sc * RoundBasedFactory.q(code_query=sc),
        trace_query,
        max_error=a.error_budget - (synthesis_error or 0.0) - dropped,
    )
    points = [_point(e, rotations, synthesis_error, dropped) for e in table]
    if not points:
        raise RuntimeError("estimator returned no results within the error budget")

    versions = {p: version(p) for p in PACKAGES}
    if eps is not None:
        versions.update(compiler_versions(a))
    best = min(points, key=lambda p: (p["physical_qubit_seconds"], p["physical_qubits"]))
    return {
        **best,
        "t_count": t_count,
        "rotation_count": rotations,
        "measurements": measurements,
        "synthesis_epsilon": eps,
        "dropped_error": dropped,
        "min_qubits": min(points, key=lambda p: (p["physical_qubits"], p["runtime_ns"])),
        "min_runtime": min(points, key=lambda p: (p["runtime_ns"], p["physical_qubits"])),
        "frontier": points,
        "assumptions": a.as_dict(),
        "versions": versions,
    }
