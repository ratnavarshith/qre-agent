# First-order Trotter, 4-spin open Heisenberg chain H = sum (XX + YY + ZZ), dt = 0.1, 4 steps,
# from the Neel state |0101> (X on qubits 0 and 2; |0000> is an eigenstate).
# Each step: exp(-i dt XX), exp(-i dt YY), exp(-i dt ZZ) on pairs (0,1), (1,2), (2,3).
# Basis changes with H and S rather than rxx/ryy: those transpile to rx(±pi/2), which
# estimate_surface currently rejects.
from qiskit import QuantumCircuit

n, dt, steps = 4, 0.1, 4
circuit = QuantumCircuit(n)
circuit.x([0, 2])
for _ in range(steps):
    for i in range(n - 1):
        pair = [i, i + 1]
        circuit.h(pair)  # XX -> ZZ
        circuit.rzz(2 * dt, i, i + 1)  # RZZ(t) = exp(-i t/2 ZZ)
        circuit.h(pair)
        circuit.sdg(pair)  # YY -> ZZ: H Sdg maps Y to Z
        circuit.h(pair)
        circuit.rzz(2 * dt, i, i + 1)
        circuit.h(pair)
        circuit.s(pair)
        circuit.rzz(2 * dt, i, i + 1)
circuit.measure_all()
