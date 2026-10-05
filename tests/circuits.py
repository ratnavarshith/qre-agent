from qiskit import QuantumCircuit
from qiskit.circuit.library import PauliEvolutionGate, QFTGate
from qiskit.quantum_info import SparsePauliOp


def qft(n):
    c = QuantumCircuit(n)
    c.append(QFTGate(n), range(n))
    c.measure_all()
    return c


def pauli_evolution():
    op = SparsePauliOp(["ZZI", "IZZ", "XXI", "IXX"], coeffs=[0.5, 0.5, 0.3, 0.3])
    c = QuantumCircuit(3)
    c.append(PauliEvolutionGate(op, time=0.7), range(3))
    c.measure_all()
    return c
