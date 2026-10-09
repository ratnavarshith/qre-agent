# 4-bit Cuccaro ripple-carry adder on an interleaved layout: carry-in ancilla on qubit 0,
# a_i on qubit 2i + 1, b_i on qubit 2i + 2, carry-out on qubit 9; a + b ends in 2, 4, 6, 8, 9.
from qiskit import QuantumCircuit
from qiskit.synthesis import adder_ripple_c04

adder = adder_ripple_c04(4, kind="half")  # qubits: a0-a3, b0-b3, cout, helper (carry-in)
circuit = QuantumCircuit(10)
circuit.compose(adder, [1, 3, 5, 7, 2, 4, 6, 8, 9, 0], inplace=True)
circuit.measure_all()
