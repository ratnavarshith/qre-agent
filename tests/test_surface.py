import math
from dataclasses import replace
from pathlib import Path

import pytest
from circuits import pauli_evolution
from qiskit import QuantumCircuit, transpile

from qdk.qiskit import estimate as legacy_estimate
from qre_agent import estimate_surface, load_assumptions
from qre_agent.circuits import qft

GRIDSYNTH = load_assumptions()
A = replace(GRIDSYNTH, synthesis="native")  # the doc tables below are v3's own synthesis
needs_compiler = pytest.mark.skipif(
    not Path(GRIDSYNTH.compiler_dir).is_dir(), reason="bicycle compiler not built"
)

# Values from the comparison table in docs/qdk-interface.md (QFT8, default assumptions).
DOC_QFT8_MIN_QUBITS = (156_545, 4_140_000, 25)
DOC_QFT8_MIN_RUNTIME = (295_225, 1_821_600, 11)


def key(p):
    return p["physical_qubits"], p["runtime_ns"], p["distance"]


@pytest.fixture(scope="module")
def qft8():
    return estimate_surface(qft(8), A)


def test_qft8_matches_doc_table(qft8):
    assert key(qft8["min_qubits"]) == DOC_QFT8_MIN_QUBITS
    assert key(qft8["min_runtime"]) == DOC_QFT8_MIN_RUNTIME
    assert (
        qft8["logical_qubits"],
        qft8["t_count"],
        qft8["rotation_count"],
        qft8["measurements"],
    ) == (25, 21, 63, 8)


def test_chosen_point_is_min_qubit_seconds(qft8):
    frontier = qft8["frontier"]
    assert qft8["physical_qubit_seconds"] == min(p["physical_qubit_seconds"] for p in frontier)
    assert qft8["physical_qubit_seconds"] == pytest.approx(
        qft8["physical_qubits"] * qft8["runtime_ns"] * 1e-9
    )
    assert qft8["min_qubits"]["physical_qubits"] == min(p["physical_qubits"] for p in frontier)
    assert qft8["min_runtime"]["runtime_ns"] == min(p["runtime_ns"] for p in frontier)


def test_cost_grows_with_size():
    costs = [estimate_surface(qft(n), A)["physical_qubit_seconds"] for n in (4, 8, 16)]
    assert costs == sorted(costs)


def test_deterministic():
    assert estimate_surface(qft(8), A) == estimate_surface(qft(8), A)


def test_reports_assumptions_and_versions(qft8):
    assert qft8["assumptions"] == A.as_dict()
    assert set(qft8["versions"]) == {"qdk", "qiskit"}


@pytest.mark.parametrize(
    "name,circuit", [("qft4", qft(4)), ("qft8", qft(8)), ("pauli_evo3", pauli_evolution())]
)
def test_logical_counts_match_legacy(name, circuit):
    prepared = transpile(
        circuit,
        basis_gates=list(A.basis_gates),
        optimization_level=A.optimization_level,
        seed_transpiler=A.seed,
    )
    legacy = legacy_estimate(
        prepared,
        {
            "qubitParams": {"name": "qubit_gate_ns_e3"},
            "qecScheme": {"name": "surface_code"},
            "errorBudget": A.error_budget,
        },
        skip_transpilation=True,
    )
    lc = legacy["logicalCounts"]
    ours = estimate_surface(circuit, A)
    assert (ours["t_count"], ours["rotation_count"], ours["measurements"]) == (
        lc["tCount"],
        lc["rotationCount"],
        lc["measurementCount"],
    )
    assert (
        ours["logical_qubits"] == legacy["physicalCounts"]["breakdown"]["algorithmicLogicalQubits"]
    )


@needs_compiler
def test_gridsynth_mode_pins_ts_and_splits_budget():
    r = estimate_surface(qft(8), GRIDSYNTH)
    budget, rotations = GRIDSYNTH.error_budget, r["rotation_count"]
    eps = budget / 3 / rotations
    assert r["synthesis_epsilon"] == pytest.approx(eps)
    # Ross-Selinger mean T count at operator-norm eps/2 (arXiv:2203.10064 Table 1): ~57.7 here
    ts = r["ts_per_rotation"]
    assert abs(ts - (3.02 * math.log2(2 / eps) + 1.77)) < 3
    for p in r["frontier"]:
        assert p["ts_per_rotation"] == ts
        assert p["synthesis_error"] == pytest.approx(rotations * eps)
        assert p["error"] - p["synthesis_error"] <= budget * 2 / 3
        assert p["error"] <= budget
    assert "bicycle_compiler" in r["versions"]


@pytest.mark.parametrize("assumptions", [A, GRIDSYNTH], ids=["native", "gridsynth"])
def test_ts_per_rotation_is_none_without_rotations(assumptions):
    # With no rotations v3's Ts-per-rotation is an arbitrary tie that varies between processes.
    c = QuantumCircuit(2, 2)
    c.t(0)
    c.cx(0, 1)
    c.measure([0, 1], [0, 1])
    r = estimate_surface(c, assumptions)
    assert r["rotation_count"] == 0
    assert r["ts_per_rotation"] is None
    assert all(p["ts_per_rotation"] is None for p in r["frontier"])
