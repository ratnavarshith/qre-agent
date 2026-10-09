# Corrupted: one controlled phase has the wrong sign.
import math

from qiskit import QuantumCircuit

n = 6
circuit = QuantumCircuit(n)
for j in reversed(range(n)):
    circuit.h(j)
    for k in reversed(range(j)):
        sign = -1 if (j, k) == (3, 1) else 1
        circuit.cp(sign * math.pi / 2 ** (j - k), k, j)
circuit.measure_all()
