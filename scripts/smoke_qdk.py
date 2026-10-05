"""Smoke test: QDK resource estimator on Qiskit circuits, v3 (qdk.qre) vs legacy (qdk.qiskit.estimate).

Both paths get the same transpiled circuit. Legacy runs with skip_transpilation=True,
otherwise its backend re-transpiles and the logical counts drift (see docs/qdk-interface.md).

Run: .venv/Scripts/python scripts/smoke_qdk.py
"""

import platform
import re
import warnings
from importlib.metadata import version

from qiskit import QuantumCircuit, qasm3, transpile
from qiskit.circuit.library import PauliEvolutionGate, QFTGate
from qiskit.quantum_info import SparsePauliOp

from qdk.estimator import EstimatorError
from qdk.qiskit import estimate as legacy_estimate
from qdk.qre import estimate, instruction_name, property_name
from qdk.qre.application import OpenQASMApplication
from qdk.qre.models import GateBased, RoundBasedFactory, SurfaceCode

BASIS = ["h", "x", "y", "z", "s", "sdg", "t", "tdg", "rx", "ry", "rz", "cx", "cz"]
ERROR_BUDGET = 1e-3
OWN_TRANSPILE_RUNS = 5

# Legacy: qubit_gate_ns_e3 + surface_code (these are also the legacy defaults).
LEGACY_PARAMS = {
    "qubitParams": {"name": "qubit_gate_ns_e3"},
    "qecScheme": {"name": "surface_code"},
    "errorBudget": ERROR_BUDGET,
}

# v3: closest match to qubit_gate_ns_e3 (50 ns gates, 100 ns measurement, all error rates 1e-3).
# code_cycle_override=400 matches legacy's (4*twoQubitGateTime + 2*oneQubitMeasurementTime);
# v3's own default would be 1*H + 4*CNOT + MEAS = 350 ns. The factory builds its own surface code
# patches, so it needs the same override.
V3_ARCH = GateBased(error_rate=1e-3, gate_time=50, measurement_time=100)
V3_SC = SurfaceCode.q(code_cycle_override=400)
V3_ISA = V3_SC * RoundBasedFactory.q(code_query=V3_SC)


def qft(n):
    c = QuantumCircuit(n)
    c.append(QFTGate(n), range(n))
    c.measure_all()
    return c


def pauli_evolution():
    op = SparsePauliOp(["ZZI", "IZZ", "XXI", "IXX"], coeffs=[0.5, 0.5, 0.3, 0.3])
    c = QuantumCircuit(3)
    c.append(PauliEvolutionGate(op, time=0.7), range(3))
    c.measure_all()
    return c


def prepare(circuit):
    # Decomposes PauliEvolutionGate / QFTGate into BASIS; fixed seed and no optimization.
    return transpile(circuit, basis_gates=BASIS, optimization_level=0, seed_transpiler=0)


def run_legacy(circuit, skip_transpilation):
    # The backend's own transpile is unseeded by default; seed it so that row is reproducible.
    r = legacy_estimate(circuit, LEGACY_PARAMS, skip_transpilation=skip_transpilation, seed_transpiler=0)
    lc, pc = r["logicalCounts"], r["physicalCounts"]
    return {
        "logical_qubits": pc["breakdown"]["algorithmicLogicalQubits"],
        "t": lc["tCount"],
        "rotations": lc["rotationCount"],
        "measurements": lc["measurementCount"],
        "physical_qubits": pc["physicalQubits"],
        "runtime_ns": pc["runtime"],
        "distance": r["logicalQubit"]["codeDistance"],
        "factories": pc["breakdown"]["numTfactories"],
        "ts_per_rotation": pc["breakdown"]["numTsPerRotation"],
    }


def gate_counts(trace):
    counts = {instruction_name(k): v for k, v in trace.gate_counts.items()}
    t = sum(counts.pop(g, 0) for g in ("T", "T_DAG"))
    rot = sum(counts.pop(g, 0) for g in ("RX", "RY", "RZ"))
    meas = sum(counts.pop(g) for g in list(counts) if g.startswith("MEAS"))
    return t, rot, meas, counts


def v3_row(entry, logical):
    props = {property_name(k): v for k, v in entry.properties.items()}
    return {
        **logical,
        "logical_qubits": props["LOGICAL_COMPUTE_QUBITS"],
        "physical_qubits": entry.qubits,
        "runtime_ns": entry.runtime,
        "distance": int(re.search(r"distance=(\d+)", str(entry.source)).group(1)),
        "factories": sum(f.copies for f in entry.factories.values()),
        "ts_per_rotation": props["NUM_TS_PER_ROTATION"],
    }


def run_v3(circuit):
    app = OpenQASMApplication(qasm3.dumps(circuit))
    t, rot, meas, rest = gate_counts(app.get_trace())
    logical = {"t": t, "rotations": rot, "measurements": meas}
    # v3 returns a Pareto frontier (qubits vs runtime); report both ends.
    table = estimate(app, V3_ARCH, V3_ISA, max_error=ERROR_BUDGET)
    min_q = min(table, key=lambda e: (e.qubits, e.runtime))
    min_t = min(table, key=lambda e: (e.runtime, e.qubits))
    return v3_row(min_q, logical), v3_row(min_t, logical), len(table), rest


COLS = ["logical_qubits", "t", "rotations", "measurements", "physical_qubits", "runtime_ns",
        "distance", "factories", "ts_per_rotation"]


def main():
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    print(f"python {platform.python_version()} | qdk {version('qdk')} | qiskit {version('qiskit')} | {platform.platform()}")
    print(f"error budget {ERROR_BUDGET}; legacy {LEGACY_PARAMS['qubitParams']['name']}+surface_code; "
          f"v3 GateBased(1e-3, 50ns, 100ns) + SurfaceCode(cycle 400ns) * RoundBasedFactory\n")

    header = f"{'circuit':<11} {'path':<23}" + "".join(f"{c:>16}" for c in COLS)
    print(header)
    print("-" * len(header))
    for name, raw in [("qft4", qft(4)), ("qft8", qft(8)), ("pauli_evo3", pauli_evolution())]:
        c = prepare(raw)
        legacy = run_legacy(c, skip_transpilation=True)
        v3_min_q, v3_min_t, frontier, rest = run_v3(c)
        rows = [
            ("legacy (reference)", legacy),
            ("v3 (min qubits)", v3_min_q),
            ("v3 (min runtime)", v3_min_t),
        ]
        for path, r in rows:
            print(f"{name:<11} {path:<23}" + "".join(f"{r[k]:>16}" for k in COLS))
        same = all(legacy[k] == v3_min_q[k] for k in ("logical_qubits", "t", "rotations", "measurements"))
        print(f"{'':<11} logical counts reference vs v3: {'MATCH' if same else 'MISMATCH'}"
              f"; v3 frontier size {frontier}; v3 gates outside T/rot/meas {rest}")
        # Diagnostic: legacy with its own internal transpile is not reproducible, so report the spread.
        own = [run_legacy(c, skip_transpilation=False) for _ in range(OWN_TRANSPILE_RUNS)]
        spread = {k: (min(r[k] for r in own), max(r[k] for r in own)) for k in ("t", "rotations", "physical_qubits")}
        print(f"{'':<11} legacy own transpile, {OWN_TRANSPILE_RUNS} runs, (min, max): {spread}\n")

    print("Floquet:")
    q = prepare(qft(4))
    for qubit in ("qubit_maj_ns_e4", "qubit_gate_ns_e3"):
        try:
            r = legacy_estimate(q, {"qubitParams": {"name": qubit}, "qecScheme": {"name": "floquet_code"},
                                    "errorBudget": ERROR_BUDGET}, skip_transpilation=True)
            print(f"  legacy floquet_code + {qubit}: {r['physicalCounts']['physicalQubits']} qubits, "
                  f"{r['physicalCounts']['runtime']} ns")
        except EstimatorError as e:  # subclasses BaseException, not Exception
            print(f"  legacy floquet_code + {qubit}: ERROR {str(e).splitlines()[0][:120]}")
    import qdk.qre.models as m
    floquet = [n for n in m.__all__ if "floquet" in n.lower()]
    print(f"  v3 qdk.qre.models Floquet classes: {floquet or 'none'} (available: {', '.join(m.__all__)})")


if __name__ == "__main__":
    main()
