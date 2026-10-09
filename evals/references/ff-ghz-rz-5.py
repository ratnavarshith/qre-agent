# 5-qubit GHZ state, then Rz(0.3) on every qubit.
from qiskit import QuantumCircuit

n, theta = 5, 0.3
circuit = QuantumCircuit(n)
circuit.h(0)
for i in range(n - 1):
    circuit.cx(i, i + 1)
for i in range(n):
    circuit.rz(theta, i)
circuit.measure_all()
