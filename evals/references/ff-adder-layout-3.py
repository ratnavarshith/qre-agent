# 3-bit Vedral-Barenco-Ekert ripple-carry adder: a = qubits 0-2, b = 3-5, carry-out = 6,
# two ancillas 7-8; a + b ends in 3, 4, 5, 6.
from qiskit.synthesis import adder_ripple_v95

circuit = adder_ripple_v95(3, kind="half")
circuit.measure_all()
