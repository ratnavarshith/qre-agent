# Corrupted: dt = 0.2. First-order Trotter, 4-spin open Heisenberg chain H = sum (XX + YY + ZZ), dt = 0.1, 4 steps,
# from the Neel state |0101> (X on qubits 0 and 2; |0000> is an eigenstate).
# Each step: exp(-i dt XX), exp(-i dt YY), exp(-i dt ZZ) on pairs (0,1), (1,2), (2,3).
from qiskit import QuantumCircuit

n, dt, steps = 4, 0.2, 4
circuit = QuantumCircuit(n)
circuit.x([0, 2])
for _ in range(steps):
    for i in range(n - 1):
        circuit.rxx(2 * dt, i, i + 1)  # RXX(t) = exp(-i t/2 XX)
        circuit.ryy(2 * dt, i, i + 1)
        circuit.rzz(2 * dt, i, i + 1)
circuit.measure_all()
