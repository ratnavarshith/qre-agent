# 6-qubit QFT without the final swaps: H then controlled phases, output bit-reversed.
import math

from qiskit import QuantumCircuit

n = 6
circuit = QuantumCircuit(n)
for j in reversed(range(n)):
    circuit.h(j)
    for k in reversed(range(j)):
        circuit.cp(math.pi / 2 ** (j - k), k, j)
circuit.measure_all()
