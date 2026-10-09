# Corrupted: the oracle flips two states (22 and 23), not one.
from qiskit import QuantumCircuit
from qiskit.circuit.library import ZGate

n, marked = 5, 22
zeros = [i for i in range(n) if not marked >> i & 1]

oracle = QuantumCircuit(n, name="oracle")
oracle.x(zeros)
oracle.append(ZGate().control(n - 2), range(1, n))  # ignores qubit 0
oracle.x(zeros)

mcz = ZGate().control(n - 1)
circuit = QuantumCircuit(n)
circuit.h(range(n))
for _ in range(2):
    circuit.append(oracle.to_gate(), range(n))
    circuit.h(range(n))
    circuit.x(range(n))
    circuit.append(mcz, range(n))
    circuit.x(range(n))
    circuit.h(range(n))
circuit.measure_all()
