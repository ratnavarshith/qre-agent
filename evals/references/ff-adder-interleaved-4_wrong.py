# Corrupted: carry-out and carry-in ancilla swapped, so the carry lands on qubit 0.
from qiskit import QuantumCircuit
from qiskit.synthesis import adder_ripple_c04

adder = adder_ripple_c04(4, kind="half")
circuit = QuantumCircuit(10)
circuit.compose(adder, [1, 3, 5, 7, 2, 4, 6, 8, 0, 9], inplace=True)
circuit.measure_all()
