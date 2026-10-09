import pytest
from qiskit import QuantumCircuit
from qiskit.circuit.library import QFT, CPhaseGate, QFTGate
from qiskit.quantum_info import Operator

from qre_agent import semantics
from qre_agent.circuits import FAMILIES, _mcz, cdkm_adder, qpe, tfim


def qft_no_swaps(n):
    """The textbook H/CP network without the final swaps, as the trial agent built it."""
    c = QuantumCircuit(n)
    for j in reversed(range(n)):
        c.h(j)
        for k in reversed(range(j)):
            c.cp(3.141592653589793 / 2 ** (j - k), k, j)
    c.measure_all()
    return c


def test_qft_accepts_with_and_without_swaps():
    trial = QFT(4, do_swaps=False).decompose()  # the trial agent's circuit
    assert Operator(trial).equiv(Operator(qft_no_swaps(4).remove_final_measurements(inplace=False)))
    assert semantics.check("qft", FAMILIES["qft"](4), 4) is None
    assert semantics.check("qft", qft_no_swaps(4), 4) is None


@pytest.mark.parametrize("change", ["inverse", "angle", "width"])
def test_qft_rejects_other_unitaries(change):
    c = QuantumCircuit(4)
    if change == "inverse":
        c.append(QFTGate(4).inverse(), range(4))
    elif change == "angle":
        c = qft_no_swaps(4)
        c.data[1] = c.data[1].replace(operation=CPhaseGate(1.0))
    else:
        c = QuantumCircuit(5)
        c.append(QFTGate(4), range(4))
    assert semantics.check("qft", c, 4) is not None


def flat_adder(n):
    """A ripple-carry adder without named registers: a = 0..n-1, b = n..2n-1 (gets the sum),
    carry-out 2n, then n carry ancillas that are uncomputed only partly (they don't matter)."""
    c = QuantumCircuit(3 * n + 1)
    carry = list(range(2 * n + 1, 3 * n + 1))
    for i in range(n):  # carries: c[i+1] = maj(a_i, b_i, c_i), written to the next ancilla
        cin = carry[i - 1] if i else None
        out = carry[i] if i < n - 1 else 2 * n
        c.ccx(i, n + i, out)
        if cin is not None:
            c.cx(i, n + i)
            c.ccx(cin, n + i, out)
            c.cx(i, n + i)
    for i in range(n):  # sums: b_i ^= a_i ^ c_i
        c.cx(i, n + i)
        if i:
            c.cx(carry[i - 1], n + i)
    return c


def test_adder_passes_on_the_benchmark_and_a_flat_layout():
    assert semantics.check("adder", cdkm_adder(4), 4) is None
    assert semantics.check("adder", flat_adder(3), 3) is None


def test_adder_fails_on_a_wrong_sum_and_honors_an_explicit_layout():
    broken = flat_adder(3)
    broken.data.pop()  # drop the last sum gate
    assert "expected" in semantics.check("adder", broken, 3)
    swapped = QuantumCircuit(10)
    swapped.append(flat_adder(3), [3, 4, 5, 0, 1, 2, 6, 7, 8, 9])  # a and b exchanged
    assert semantics.check("adder", swapped, 3) is not None  # default layout: wrong qubits
    assert semantics.check("adder", swapped, 3, layout=([3, 4, 5], [0, 1, 2], [0, 1, 2, 6])) is None


def named(oracle):
    inst = oracle.to_instruction()
    inst.name = "oracle"
    c = QuantumCircuit(oracle.num_qubits, 1)
    c.append(inst, range(oracle.num_qubits))
    return c


def test_grover_oracle_flips_exactly_the_marked_state():
    n = 4
    oracle = QuantumCircuit(2 * n - 2)
    oracle.x(0)  # marks 0b1110
    _mcz(oracle, list(range(n)), list(range(n, 2 * n - 2)))
    oracle.x(0)
    assert semantics.check("grover", named(oracle), n) is None
    assert semantics.check("grover", named(oracle), n, marked=0b1110) is None
    assert "marks 14, expected 15" in semantics.check("grover", named(oracle), n, marked=15)


def test_grover_rejects_the_trials_cz_chain_and_a_missing_oracle():
    chain = QuantumCircuit(8)  # what the trial agent used as its oracle: flips many states
    for i in range(7):
        chain.cz(i, i + 1)
    assert "expected exactly one" in semantics.check("grover", named(chain), 8)
    assert "no instruction named 'oracle'" in semantics.check("grover", FAMILIES["grover"](4), 4)


def test_grover_rejects_an_oracle_that_leaves_garbage_in_the_ancillas():
    oracle = QuantumCircuit(3)
    oracle.ccx(0, 1, 2)  # computes the AND but never uncomputes it
    assert "ancillas" in semantics.check("grover", named(oracle), 2)


def test_qpe_and_tfim_compare_counts_only():
    assert semantics.check("qpe", qpe(4), 4) is None
    one_step = QuantumCircuit(4)  # one Trotter step of the four
    one_step.append(tfim(4).data[0].operation, range(4))
    assert "rotation count" in semantics.check("tfim", one_step, 4)


def test_checks_are_for_small_n_only():
    assert "n <= 8" in semantics.check("qft", FAMILIES["qft"](9), 9)
