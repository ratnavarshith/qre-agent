# Corrupted: marks 10, not 5. Grover on 4 qubits, one iteration, marking |0101> (5; qubit 0 least significant).
from qiskit import QuantumCircuit
from qiskit.circuit.library import ZGate

n, marked = 4, 10
zeros = [i for i in range(n) if not marked >> i & 1]
mcz = ZGate().control(n - 1)

oracle = QuantumCircuit(n, name="oracle")
oracle.x(zeros)
oracle.append(mcz, range(n))
oracle.x(zeros)

circuit = QuantumCircuit(n)
circuit.h(range(n))
circuit.append(oracle.to_gate(), range(n))
circuit.h(range(n))
circuit.x(range(n))
circuit.append(mcz, range(n))
circuit.x(range(n))
circuit.h(range(n))
circuit.measure_all()
