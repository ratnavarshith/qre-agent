import hashlib
import math
import re
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from circuits import pauli_evolution
from qiskit import QuantumCircuit
from qiskit.circuit.library import PauliEvolutionGate
from qiskit.quantum_info import SparsePauliOp
from qiskit.transpiler.passes import LitinskiTransformation

from qre_agent import estimate_bicycle, estimate_surface, load_assumptions
from qre_agent.bicycle import CODE_ERRORS, instruction_error, physical_qubits
from qre_agent.circuits import qft
from qre_agent.pbc import iter_pbc

A = load_assumptions()
UPSTREAM_SCRIPTS = Path(A.compiler_dir).parent.parent / "scripts"


@pytest.fixture(scope="module")
def upstream_parse():
    if not (UPSTREAM_SCRIPTS / "qiskit_parser.py").is_file():
        pytest.skip("upstream qiskit_parser.py not found")
    sys.path.insert(0, str(UPSTREAM_SCRIPTS))
    from qiskit_parser import iter_qiskit_pbc_circuit

    return lambda pbc: list(iter_qiskit_pbc_circuit(pbc))


def litinski(circuit, use_ppr=False):
    return LitinskiTransformation(fix_clifford=False, use_ppr=use_ppr)(circuit)


def gates(*ops, n=2, measure=True):
    c = QuantumCircuit(n, n)
    for name, *args in ops:
        getattr(c, name)(*args)
    if measure:
        c.measure(range(n), range(n))
    return litinski(c)


def evolution(label, coeff, time, qubits, n):
    c = QuantumCircuit(n)
    c.append(PauliEvolutionGate(SparsePauliOp(label, coeffs=[coeff]), time=time), qubits)
    return c


# PBC circuits where the upstream parser is right: measurements (incl. signs and Y), and rotations
# on the circuit's leading qubits in order, where only the angle convention differs.
UPSTREAM_CORRECT = [
    gates(("x", 0)),
    gates(("sdg", 0), ("h", 0)),
    gates(("s", 0), ("h", 0), ("x", 1)),
    gates(("s", 0), ("cx", 0, 1), ("h", 0), ("cx", 0, 1)),
    gates(("t", 0), n=1),
    gates(("x", 0), ("t", 0), n=1, measure=False),
    gates(("h", 0), ("cx", 0, 1), ("rz", 0.3, 1), ("tdg", 0)),
    evolution("XY", -0.5, 0.2, [0, 1], 2),
    evolution("ZZ", 1.0, 0.3, [0, 1], 3),
]


@pytest.mark.parametrize("pbc", UPSTREAM_CORRECT)
def test_converter_matches_upstream_where_upstream_is_correct(pbc, upstream_parse):
    ours, theirs = list(iter_pbc(pbc)), upstream_parse(pbc)
    assert len(ours) == len(theirs)
    for o, t in zip(ours, theirs):
        if "Measurement" in t:
            assert o == t
        else:
            assert o["Rotation"]["basis"] == t["Rotation"]["basis"]
            assert float(o["Rotation"]["angle"]) == pytest.approx(
                -2 * float(t["Rotation"]["angle"])
            )


def test_angle_bug(upstream_parse):
    c = QuantumCircuit(1)
    c.t(0)
    pbc = litinski(c)
    assert float(upstream_parse(pbc)[0]["Rotation"]["angle"]) == pytest.approx(math.pi / 8)
    assert float(next(iter_pbc(pbc))["Rotation"]["angle"]) == -math.pi / 4  # T = exp(-i·π/8·Z)


def test_qubit_index_bug(upstream_parse):
    c = QuantumCircuit(3)
    c.t(2)
    pbc = litinski(c)
    assert upstream_parse(pbc)[0]["Rotation"]["basis"] == ["Z", "I", "I"]
    assert next(iter_pbc(pbc))["Rotation"]["basis"] == ["I", "I", "Z"]


def test_pauli_product_rotations_match_evolutions():
    c = QuantumCircuit(3)
    c.h(1)
    c.cx(0, 2)
    c.rz(0.3, 2)
    c.t(1)
    c.x(0)
    c.tdg(0)
    assert list(iter_pbc(litinski(c, use_ppr=True))) == list(iter_pbc(litinski(c)))


def test_eq25_matches_paper():
    # Tour de gross Sec. 4, TFIM on 100 qubits: 10 two-gross modules + 1e-3 two-gross factory.
    assert physical_qubits("two-gross", 1e-3, 10) == 8138
    # The paper's gross figure (4817) is 16 higher: it uses a' = 29, Table 3 gives 13 at 1e-4.
    assert physical_qubits("gross", 1e-4, 10) == 4801


def test_rejects_unsupported_error_rate():
    with pytest.raises(ValueError, match="1e-3 and 1e-4"):
        estimate_bicycle(qft(4), replace(A, physical_error_rate=5e-4))


def test_missing_binary_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError, match="cargo build"):
        estimate_bicycle(qft(4), replace(A, compiler_dir=str(tmp_path)))


@pytest.mark.compiler
@pytest.mark.parametrize("code,passes", [("gross", False), ("two-gross", True)])
def test_pass_rule(code, passes):
    r = estimate_bicycle(qft(4), replace(A, bicycle_code=code))
    b = r["error_breakdown"]
    assert b["synthesis"] == pytest.approx(A.error_budget / 3)  # 9 rotations × eps
    assert b["total"] == r["error"] == b["instructions"] + b["synthesis"]
    assert r["passes"] is passes is (b["total"] <= A.error_budget)


@pytest.mark.compiler
def test_deterministic():
    assert estimate_bicycle(qft(8), A) == estimate_bicycle(qft(8), A)


@pytest.mark.compiler
@pytest.mark.parametrize("circuit", [qft(4), qft(8), pauli_evolution()])
def test_logical_counts_match_surface(circuit):
    bicycle, surface = estimate_bicycle(circuit, A), estimate_surface(circuit, A)
    keys = ("t_count", "rotation_count", "measurements", "synthesis_epsilon")
    assert [bicycle[k] for k in keys] == [surface[k] for k in keys]
    assert surface["ts_per_rotation"] == math.ceil(bicycle["ts_per_rotation"])
    assert bicycle["instruction_counts"]["t_injs"] == pytest.approx(
        bicycle["t_count"] + bicycle["rotation_count"] * bicycle["ts_per_rotation"]
    )


@pytest.mark.compiler
def test_runtime_linear_in_timestep():
    runs = [estimate_bicycle(qft(4), replace(A, timestep_ns=t)) for t in (50, 66.7, 100)]
    assert len({r["timesteps"] for r in runs}) == 1
    for r, t in zip(runs, (50, 66.7, 100)):
        assert r["runtime_ns"] == pytest.approx(r["timesteps"] * t)


@pytest.mark.compiler
def test_rescoring_with_code_constants_reproduces_numerics():
    # QFT16 spans two modules, so idles and joint measurements are exercised too.
    r = estimate_bicycle(qft(16), replace(A, bicycle_code="gross", physical_error_rate=1e-4))
    assert r["instruction_counts"]["idles"] and r["instruction_counts"]["joint_measurements"]
    rescored = instruction_error(r["instruction_counts"], CODE_ERRORS[("gross", 1e-4)])
    assert rescored == pytest.approx(r["error_breakdown"]["instructions"], rel=1e-9)


@pytest.mark.compiler
def test_sub_export_precision_rotation_counted_the_same_on_both_sides():
    # qasm3.dumps writes |angle| < 1e-9 as 0 and v3 drops it; the bicycle path never exports.
    c = QuantumCircuit(1, 1)
    c.rz(1e-10, 0)
    c.rz(0.3, 0)
    c.measure(0, 0)
    assert estimate_surface(c, A)["rotation_count"] == estimate_bicycle(c, A)["rotation_count"] == 1


@pytest.mark.compiler
def test_large_angles_are_reduced_mod_2pi():
    # QPE's controlled powers reach 2π/3·2^31, past the compiler's I32F96 angle range (±2^31).
    c = QuantumCircuit(1, 1)
    c.rz(2 * math.pi / 3 * 2**31, 0)
    c.rz(2 * math.pi + math.pi / 4, 0)  # a T gate in disguise
    c.measure(0, 0)
    s, b = estimate_surface(c, A), estimate_bicycle(c, A)
    assert (s["t_count"], s["rotation_count"]) == (b["t_count"], b["rotation_count"]) == (1, 1)


@pytest.mark.parametrize("estimator", [estimate_surface, estimate_bicycle])
def test_near_clifford_rotation_is_rejected(estimator):
    # LitinskiTransformation silently treats angles within ~2.45e-6 of k·π/2 as Clifford.
    c = QuantumCircuit(1, 1)
    c.rz(math.pi / 2 + 1e-7, 0)
    c.measure(0, 0)
    with pytest.raises(ValueError, match="Clifford"):
        estimator(c, A)


@pytest.mark.compiler
def test_dropped_rotations_are_accounted_in_the_error():
    # Dropping rz(θ) for |θ| < 1e-9 costs at most |θ|/2 each (operator norm), added to the total.
    c = QuantumCircuit(1, 1)
    c.rz(1e-10, 0)
    c.rz(-4e-10, 0)
    c.rz(0.3, 0)
    c.measure(0, 0)
    bound = (1e-10 + 4e-10) / 2
    s, b = estimate_surface(c, A), estimate_bicycle(c, A)
    assert s["dropped_error"] == pytest.approx(bound)
    assert b["dropped_error"] == b["error_breakdown"]["dropped"] == pytest.approx(bound)
    assert b["error"] == pytest.approx(
        b["error_breakdown"]["instructions"] + b["synthesis_error"] + bound
    )
    for p in s["frontier"]:
        assert p["error"] <= A.error_budget
        assert p["error"] >= p["synthesis_error"] + bound
    clean = QuantumCircuit(1, 1)
    clean.rz(0.3, 0)
    clean.measure(0, 0)
    assert estimate_surface(clean, A)["dropped_error"] == 0.0


@pytest.mark.compiler
def test_versions_record_the_rsgridsynth_the_compiler_was_built_with():
    # target/release/../../Cargo.lock is the lock the binaries were built from.
    lock = tomllib.loads((Path(A.compiler_dir).parent.parent / "Cargo.lock").read_text("utf-8"))
    expected = next(p["version"] for p in lock["package"] if p["name"] == "rsgridsynth")
    for r in (estimate_bicycle(qft(4), A), estimate_surface(qft(4), A)):
        assert r["versions"]["rsgridsynth"] == expected


@pytest.mark.compiler
def test_results_record_compiler_identity_not_machine_paths():
    root = Path(A.compiler_dir).parent.parent
    if not (root / ".git").exists():
        pytest.skip("compiler directory is not a git checkout")
    lock_sha = hashlib.sha256((root / "Cargo.lock").read_bytes()).hexdigest()
    for r in (estimate_bicycle(qft(4), A), estimate_surface(qft(4), A)):
        assert re.fullmatch(r"[0-9a-f]{40}(-dirty)?", r["versions"]["bicycle_compiler_commit"])
        assert r["versions"]["cargo_lock_sha256"] == lock_sha
        assert "compiler_dir" not in r["assumptions"]
