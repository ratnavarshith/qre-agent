"""Benchmark circuit families. Each builder takes a size n and returns a measured QuantumCircuit."""

import math

from qiskit import QuantumCircuit
from qiskit.circuit.library import PauliEvolutionGate, QFTGate
from qiskit.quantum_info import SparsePauliOp
from qiskit.synthesis import adder_ripple_c04

TFIM_STEPS, TFIM_DT, TFIM_J, TFIM_H = 4, 0.1, 1.0, 1.0
GROVER_ITERATIONS = 2
QPE_PHASE = 1 / 3  # not a dyadic fraction, so every controlled power is a non-Clifford rotation


def qft(n):
    c = QuantumCircuit(n)
    c.append(QFTGate(n), range(n))
    c.measure_all()
    return c


def cdkm_adder(n):
    """n-bit Cuccaro-Draper-Kutin-Moulton ripple-carry adder with carry-out: 2n Toffolis."""
    c = adder_ripple_c04(n, kind="half")
    c.measure_all()
    return c


def tfim(n):
    """Trotterized 1D transverse-field Ising chain, H = -J·sum Z_i Z_i+1 - h·sum X_i."""
    terms = [("ZZ", [i, i + 1], -TFIM_J) for i in range(n - 1)] + [
        ("X", [i], -TFIM_H) for i in range(n)
    ]
    step = PauliEvolutionGate(SparsePauliOp.from_sparse_list(terms, num_qubits=n), time=TFIM_DT)
    c = QuantumCircuit(n)
    for _ in range(TFIM_STEPS):
        c.append(step, range(n))
    c.measure_all()
    return c


def _mcz(c, qubits, ancillas):
    """Phase-flip |1...1> on qubits with a Toffoli AND-ladder into clean ancillas: 2(len-2) CCX."""
    ladder = [(qubits[0], qubits[1], ancillas[0])]
    ladder += [(ancillas[k - 1], qubits[k + 1], ancillas[k]) for k in range(1, len(qubits) - 2)]
    for gate in ladder:
        c.ccx(*gate)
    c.cz(ancillas[len(qubits) - 3], qubits[-1])
    for gate in reversed(ladder):
        c.ccx(*gate)


def grover(n, marked=None):
    """Grover search on n >= 3 qubits for one marked state, GROVER_ITERATIONS iterations."""
    marked = (1 << n) - 1 if marked is None else marked
    zeros = [i for i in range(n) if not marked >> i & 1]
    data, anc = list(range(n)), list(range(n, 2 * n - 2))
    c = QuantumCircuit(2 * n - 2, n)
    c.h(data)
    for _ in range(GROVER_ITERATIONS):
        if zeros:
            c.x(zeros)
        _mcz(c, data, anc)  # oracle
        if zeros:
            c.x(zeros)
        c.h(data)
        c.x(data)
        _mcz(c, data, anc)  # diffusion about |s>, up to global phase
        c.x(data)
        c.h(data)
    c.measure(data, range(n))
    return c


def qpe(n):
    """Phase estimation of P(2π·QPE_PHASE) on its |1> eigenstate with n counting qubits."""
    c = QuantumCircuit(n + 1, n)
    c.x(n)
    c.h(range(n))
    for k in range(n):
        c.cp(2 * math.pi * QPE_PHASE * 2**k, k, n)
    c.append(QFTGate(n).inverse(), range(n))
    c.measure(range(n), range(n))
    return c


FAMILIES = {"qft": qft, "adder": cdkm_adder, "tfim": tfim, "grover": grover, "qpe": qpe}
