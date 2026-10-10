"""Semantic checks of a circuit the agent wrote, for small sizes (n <= MAX_N). Used by the eval
grader, not by verify: they need the task's family and size. Each check returns None when it
passes, else the reason. See docs/agent-design.md.

- qft: the unitary equals the QFT, with or without the final swaps (up to global phase).
- adder: classical simulation on a few basis inputs gives a + b.
- grover: the instruction named "oracle" flips the phase of exactly one data state.
- qpe, tfim: T and rotation counts within a tolerance of the benchmark circuit's (no semantics).

The eval's free-form tasks also use same_state and counts_close_to against a reference circuit.
"""

import random

from qiskit import QuantumCircuit, qasm3, transpile
from qiskit.circuit.library import QFTGate
from qiskit.quantum_info import Operator, Statevector

from qdk.qre.application import OpenQASMApplication

from .assumptions import load_assumptions
from .circuits import FAMILIES
from .surface import _logical_counts, prepare

MAX_N = 8
COUNT_TOL = 0.1  # relative tolerance on T and rotation counts for qpe and tfim
ADDER_INPUTS = 6
CLASSICAL = ("x", "cx", "ccx", "swap")
SKIP = ("measure", "barrier")
ATOL = 1e-8


def _unitary(circuit):
    return circuit.remove_final_measurements(inplace=False)


def qft(circuit, n, swaps=None):
    """`swaps`: True requires the final swaps, False requires none, None accepts either. Either
    qubit order is accepted: the circuit written with qubit 0 as the most significant bit is
    the same QFT with its qubits relabelled (grader v2; v1 took Qiskit's order only)."""
    if circuit.num_qubits != n:
        return f"expected {n} qubits, got {circuit.num_qubits}"
    reference = QuantumCircuit(n)
    reference.append(QFTGate(n), range(n))
    no_swaps = reference.copy()  # the QFT is the H/CP network followed by reversing the qubits
    for i in range(n // 2):
        no_swaps.swap(i, n - 1 - i)
    u = Operator(_unitary(circuit))
    accepted = ([reference] if swaps is not False else []) + (
        [no_swaps] if swaps is not True else []
    )
    if any(u.equiv(Operator(c)) or u.equiv(Operator(c.reverse_bits())) for c in accepted):
        return None
    which = {None: "with or without", True: "with", False: "without"}[swaps]
    return f"unitary is not the QFT {which} the final swaps"


def _adder_layout(circuit, n):
    """(a, b, sum) qubit indices: registers named a, b and cout when the circuit has them (as
    qiskit's adders do), else a = qubits 0..n-1, b = n..2n-1, sum = b then qubit 2n."""
    regs = {r.name: [circuit.find_bit(q).index for q in r] for r in circuit.qregs}
    if {"a", "b", "cout"} <= set(regs):
        return regs["a"], regs["b"], regs["b"] + regs["cout"]
    return list(range(n)), list(range(n, 2 * n)), list(range(n, 2 * n + 1))


def _run_classical(circuit, bits):
    """Basis state in, basis state out, for a circuit of X/CX/CCX/SWAP; None if it has others."""
    bits = list(bits)
    for inst in circuit.data:
        name, q = inst.operation.name, [circuit.find_bit(b).index for b in inst.qubits]
        if name in SKIP:
            continue
        if name == "x":
            bits[q[0]] ^= 1
        elif name in ("cx", "ccx"):
            bits[q[-1]] ^= all(bits[i] for i in q[:-1])
        elif name == "swap":
            bits[q[0]], bits[q[1]] = bits[q[1]], bits[q[0]]
        else:
            return None
    return bits


def _run_statevector(circuit, bits):
    index = sum(b << i for i, b in enumerate(bits))
    probs = Statevector.from_int(index, 2**circuit.num_qubits).evolve(_unitary(circuit))
    out = int(probs.probabilities().argmax())
    if abs(probs.probabilities()[out] - 1) > ATOL:
        return None
    return [out >> i & 1 for i in range(circuit.num_qubits)]


def adder(circuit, n, layout=None, inputs=ADDER_INPUTS, seed=0):
    """`layout` is (a, b, sum) qubit indices, little-endian; the default is _adder_layout."""
    a_q, b_q, s_q = layout or _adder_layout(circuit, n)
    top = 2**n - 1
    rng = random.Random(seed)
    cases = [(0, 0), (1, 1), (top, 1), (top, top)]
    cases += [(rng.randint(0, top), rng.randint(0, top)) for _ in range(inputs - len(cases))]
    try:
        classical = transpile(_unitary(circuit), basis_gates=list(CLASSICAL), optimization_level=0)
    except Exception:  # noqa: BLE001 (not decomposable into classical gates)
        classical = None
    for a, b in cases:
        bits = [0] * circuit.num_qubits
        for i, q in enumerate(a_q):
            bits[q] = a >> i & 1
        for i, q in enumerate(b_q):
            bits[q] = b >> i & 1
        out = _run_classical(classical, bits) if classical is not None else None
        if out is None:
            if circuit.num_qubits > 20:
                return "not a classical reversible circuit and too large to simulate"
            out = _run_statevector(circuit, bits)
            if out is None:
                return f"input a={a}, b={b} does not give a basis state"
        got = sum(out[q] << i for i, q in enumerate(s_q))
        if got != a + b:
            return f"a={a}, b={b}: got {got}, expected {a + b}"
    return None


def grover(circuit, n, marked=None):
    """The oracle is the first instruction named "oracle"; its first n qubits are the data and
    the rest are ancillas starting in |0>. It must map |x>|0> to ±|x>|0> with exactly one minus
    sign (relative to the others), on `marked` if given."""
    oracle = next((i.operation for i in circuit.data if i.operation.name.lower() == "oracle"), None)
    if oracle is None or oracle.definition is None:
        return "no instruction named 'oracle' to check"
    body = oracle.definition
    phases = []
    for x in range(2**n):
        amplitude = Statevector.from_int(x, 2**body.num_qubits).evolve(body).data[x]
        if abs(abs(amplitude) - 1) > ATOL:
            return f"oracle does not map |{x}> to a phase times itself (ancillas must return to 0)"
        phases.append(amplitude)
    reference = max(phases, key=lambda p: sum(abs(q - p) < ATOL for q in phases))
    flipped = [x for x, p in enumerate(phases) if abs(p + reference) < ATOL]
    if len(flipped) + sum(abs(p - reference) < ATOL for p in phases) != 2**n:
        return "oracle applies phases other than ±1"
    if len(flipped) != 1:
        return f"oracle flips {len(flipped)} states, expected exactly one"
    if marked is not None and flipped[0] != marked:
        return f"oracle marks {flipped[0]}, expected {marked}"
    return None


def logical_counts(circuit, a=None):
    """(T count, rotation count) as the surface estimate counts them."""
    prepared, _ = prepare(circuit, a or load_assumptions())
    t, rotations, _ = _logical_counts(OpenQASMApplication(qasm3.dumps(prepared)).get_trace())
    return t, rotations


def same_state(circuit, reference):
    """Both circuits, run on |0...0> without their final measurements, give the same state up to
    global phase."""
    if circuit.num_qubits != reference.num_qubits:
        return f"expected {reference.num_qubits} qubits, got {circuit.num_qubits}"
    if circuit.num_qubits > 20:
        return "too large to simulate"
    if Statevector(_unitary(circuit)).equiv(Statevector(_unitary(reference))):
        return None
    return "output state differs from the reference circuit's"


def counts_close(circuit, family, n, rel_tol=COUNT_TOL):
    """T and rotation counts each within rel_tol of the benchmark circuit's."""
    return counts_close_to(circuit, FAMILIES[family](n), rel_tol)


def counts_close_to(circuit, reference, rel_tol=COUNT_TOL):
    """T and rotation counts each within rel_tol of the reference circuit's."""
    got, expected = logical_counts(circuit), logical_counts(reference)
    for name, g, e in zip(("T count", "rotation count"), got, expected, strict=True):
        if abs(g - e) > rel_tol * e:
            return f"{name} {g} is not within {rel_tol:.0%} of the reference {e}"
    return None


def check(family, circuit, n, **kw):
    if n > MAX_N:
        return f"semantic checks run for n <= {MAX_N} only, got n = {n}"
    if family in ("qpe", "tfim"):
        return counts_close(circuit, family, n, **kw)
    return {"qft": qft, "adder": adder, "grover": grover}[family](circuit, n, **kw)
