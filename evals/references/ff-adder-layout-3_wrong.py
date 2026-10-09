# Corrupted: a and b swapped with the carry, i.e. the adder sits on the wrong qubits.
from qiskit import QuantumCircuit
from qiskit.synthesis import adder_ripple_v95

circuit = QuantumCircuit(9)
circuit.compose(adder_ripple_v95(3, kind="half"), [0, 1, 2, 6, 3, 4, 5, 7, 8], inplace=True)
circuit.measure_all()
