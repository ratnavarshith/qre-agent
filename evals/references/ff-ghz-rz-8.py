# 8-qubit GHZ state, then Rz(pi/5) on every qubit.
import math

from qiskit import QuantumCircuit

n, theta = 8, math.pi / 5
circuit = QuantumCircuit(n)
circuit.h(0)
for i in range(n - 1):
    circuit.cx(i, i + 1)
for i in range(n):
    circuit.rz(theta, i)
circuit.measure_all()
