# Corrupted: controlled-phase instead of controlled-Rz. H on 4 qubits, controlled-Rz(pi/7) from qubit i to i + 1 for i = 0, 1, 2, then H on all.
import math

from qiskit import QuantumCircuit

n = 4
circuit = QuantumCircuit(n)
circuit.h(range(n))
for i in range(n - 1):
    circuit.cp(math.pi / 7, i, i + 1)
circuit.h(range(n))
circuit.measure_all()
