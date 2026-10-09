# Corrupted: an extra qubit 8 that never receives the carry (fixed-width adder, sum mod 16).
from qiskit import QuantumCircuit
from qiskit.synthesis import adder_qft_d00

circuit = QuantumCircuit(9)
circuit.compose(adder_qft_d00(4, kind="fixed"), range(8), inplace=True)
circuit.measure_all()
