# Corrupted: Rz on qubits 0-6 only. (Moving a rotation to another qubit would not do: on a GHZ
# state Rz on any qubit gives the same state.)
import math

from qiskit import QuantumCircuit

n, theta = 8, math.pi / 5
circuit = QuantumCircuit(n)
circuit.h(0)
for i in range(n - 1):
    circuit.cx(i, i + 1)
for i in range(n - 1):
    circuit.rz(theta, i)
circuit.measure_all()
