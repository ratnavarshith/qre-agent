"""Surface-code resource estimates on QDK's v3 estimator (qdk.qre). See docs/qdk-interface.md."""

from importlib.metadata import version

from qiskit import qasm3, transpile

from qdk.qre import estimate, instruction_name, property_name, property_name_to_key
from qdk.qre.application import OpenQASMApplication
from qdk.qre.instruction_ids import LATTICE_SURGERY
from qdk.qre.models import GateBased, RoundBasedFactory, SurfaceCode

from .assumptions import load_assumptions

PACKAGES = ("qdk", "qiskit")
NS = 1e-9


def _logical_counts(trace):
    counts = {instruction_name(k): v for k, v in trace.gate_counts.items()}
    t = counts.pop("T", 0) + counts.pop("T_DAG", 0)
    rotations = sum(counts.pop(g, 0) for g in ("RX", "RY", "RZ"))
    measurements = sum(counts.pop(g) for g in list(counts) if g.startswith("MEAS"))
    if counts:  # e.g. CCX: not produced by the fixed basis, but never drop it silently
        raise ValueError(f"unexpected non-Clifford instructions in trace: {counts}")
    return t, rotations, measurements


def _point(entry):
    props = {property_name(k): v for k, v in entry.properties.items()}
    ls = entry.source.get(LATTICE_SURGERY).instruction
    return {
        "physical_qubits": entry.qubits,
        "runtime_ns": entry.runtime,
        "physical_qubit_seconds": entry.qubits * entry.runtime * NS,
        "distance": ls.get_property(property_name_to_key("DISTANCE")),
        "logical_qubits": props["LOGICAL_COMPUTE_QUBITS"],
        "ts_per_rotation": props["NUM_TS_PER_ROTATION"],
        "error": entry.error,
    }


def estimate_surface(circuit, assumptions=None):
    a = assumptions or load_assumptions()
    if a.qec != "surface_code":
        raise ValueError(f"unsupported qec scheme: {a.qec}")

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
    sc = SurfaceCode.q(code_cycle_override=a.code_cycle_ns)
    table = estimate(app, arch, sc * RoundBasedFactory.q(code_query=sc), max_error=a.error_budget)
    points = [_point(e) for e in table]
    if not points:
        raise RuntimeError("estimator returned no results within the error budget")

    best = min(points, key=lambda p: (p["physical_qubit_seconds"], p["physical_qubits"]))
    return {
        **best,
        "t_count": t_count,
        "rotation_count": rotations,
        "measurements": measurements,
        "min_qubits": min(points, key=lambda p: (p["physical_qubits"], p["runtime_ns"])),
        "min_runtime": min(points, key=lambda p: (p["runtime_ns"], p["physical_qubits"])),
        "frontier": points,
        "assumptions": a.as_dict(),
        "versions": {p: version(p) for p in PACKAGES},
    }
