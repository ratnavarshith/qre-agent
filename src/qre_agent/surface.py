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
from .compiler import compiler_version, gridsynth_t_counts

PACKAGES = ("qdk", "qiskit")
NS = 1e-9
ROTATIONS = ("rx", "ry", "rz")
SYNTHESIS_SHARE = 1 / 3  # legacy convention: rotation synthesis gets a third of the budget


def _logical_counts(trace):
    counts = {instruction_name(k): v for k, v in trace.gate_counts.items()}
    t = counts.pop("T", 0) + counts.pop("T_DAG", 0)
    rotations = sum(counts.pop(g, 0) for g in ("RX", "RY", "RZ"))
    measurements = sum(counts.pop(g) for g in list(counts) if g.startswith("MEAS"))
    if counts:  # e.g. CCX: not produced by the fixed basis, but never drop it silently
        raise ValueError(f"unexpected non-Clifford instructions in trace: {counts}")
    return t, rotations, measurements


def _rotation_angles(prepared):
    """Angles phi of the non-T rotations in the exp(i·phi/2·P) convention (rz(t) = exp(-i·t/2·Z))."""
    angles = [-float(i.operation.params[0]) for i in prepared.data if i.operation.name in ROTATIONS]
    return [phi for phi in angles if not math.isclose(abs(phi), math.pi / 4, abs_tol=1e-12)]


def _native_synthesis_error(rotations, ts):
    return rotations * 2 ** ((4.86 - ts) / 0.53)  # v3's mixed-fallback fit, qre psspc.rs


def _point(entry, rotations, synthesis_error):
    props = {property_name(k): v for k, v in entry.properties.items()}
    ls = entry.source.get(LATTICE_SURGERY).instruction
    ts = props["NUM_TS_PER_ROTATION"]
    if synthesis_error is None:  # native: v3's own synthesis error is already inside entry.error
        synthesis_error, extra = _native_synthesis_error(rotations, ts), 0.0
    else:  # gridsynth: v3 ran on the remaining budget, so add our synthesis share back
        extra = synthesis_error
    return {
        "physical_qubits": entry.qubits,
        "runtime_ns": entry.runtime,
        "physical_qubit_seconds": entry.qubits * entry.runtime * NS,
        "distance": ls.get_property(property_name_to_key("DISTANCE")),
        "logical_qubits": props["LOGICAL_COMPUTE_QUBITS"],
        "ts_per_rotation": ts,
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

    prepared = transpile(
        circuit,
        basis_gates=list(a.basis_gates),
        optimization_level=a.optimization_level,
        seed_transpiler=a.seed,
    )
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
        max_error=a.error_budget - (synthesis_error or 0.0),
    )
    points = [_point(e, rotations, synthesis_error) for e in table]
    if not points:
        raise RuntimeError("estimator returned no results within the error budget")

    versions = {p: version(p) for p in PACKAGES}
    if eps is not None:
        versions["bicycle_compiler"] = compiler_version(a)
    best = min(points, key=lambda p: (p["physical_qubit_seconds"], p["physical_qubits"]))
    return {
        **best,
        "t_count": t_count,
        "rotation_count": rotations,
        "measurements": measurements,
        "synthesis_epsilon": eps,
        "min_qubits": min(points, key=lambda p: (p["physical_qubits"], p["runtime_ns"])),
        "min_runtime": min(points, key=lambda p: (p["runtime_ns"], p["physical_qubits"])),
        "frontier": points,
        "assumptions": a.as_dict(),
        "versions": versions,
    }
