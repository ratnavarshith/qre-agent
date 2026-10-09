# 4-bit Draper (QFT) adder with carry-out: a = qubits 0-3, b = 4-7, carry = 8; a + b in 4-8.
from qiskit.synthesis import adder_qft_d00

circuit = adder_qft_d00(4, kind="half")
circuit.measure_all()
