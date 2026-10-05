"""Qiskit circuit -> PBC ops for IBM's bicycle compiler.

Replaces the compiler's scripts/qiskit_parser.py, which writes +t·c instead of -2·t·c for
PauliEvolutionGate and puts rotations on the wrong qubits. The compiler's convention is
exp(i·phi/2·P). See docs/bicycle-interface.md.
"""

from qiskit.transpiler.passes import LitinskiTransformation, RemoveBarriers

from .surface import prepare

PAULIS = "IXZY"  # indexed by 2·z + x


def to_pbc(circuit, a):
    """Prepare like estimate_surface, then Litinski-transform to Pauli rotations and measurements."""
    prepared, dropped = prepare(circuit, a)
    pbc = LitinskiTransformation(fix_clifford=False)(RemoveBarriers()(prepared))
    return list(iter_pbc(pbc)), dropped


def _basis(pbc, inst, pauli):
    """Full-width basis of a qiskit Pauli acting on inst.qubits, and its sign."""
    if pauli.phase not in (0, 2):
        raise ValueError(f"non-Hermitian Pauli {pauli}")
    index = {q: i for i, q in enumerate(pbc.qubits)}
    basis = ["I"] * pbc.num_qubits
    for q, z, x in zip(inst.qubits, pauli.z, pauli.x):
        basis[index[q]] = PAULIS[2 * int(z) + int(x)]
    return basis, -1 if pauli.phase == 2 else 1


def _evolution(pbc, inst):
    """PauliEvolutionGate(c·P, t) = exp(-i·t·c·P), so phi = -2·t·c."""
    op = inst.operation
    terms = [] if isinstance(op.operator, list) else op.operator.to_sparse_list()
    if len(terms) != 1:
        raise ValueError("PauliEvolution is not a single Pauli rotation")
    paulis, local, coeff = terms[0]
    if complex(coeff).imag or set(paulis) - set("XYZ"):
        raise ValueError(f"unsupported PauliEvolution term {terms[0]}")
    index = {q: i for i, q in enumerate(pbc.qubits)}
    basis = ["I"] * pbc.num_qubits
    for p, k in zip(paulis, local):
        basis[index[inst.qubits[k]]] = p
    return basis, -2 * float(op.params[0]) * complex(coeff).real


def iter_pbc(pbc):
    """Yield compiler ops for a circuit of Pauli rotations and Pauli product measurements."""
    for inst in pbc.data:
        op = inst.operation
        if op.name == "PauliEvolution":
            basis, phi = _evolution(pbc, inst)
            yield {"Rotation": {"basis": basis, "angle": repr(phi)}}
        elif op.name == "pauli_product_rotation":  # exp(-i·theta/2·P), so phi = -theta
            basis, sign = _basis(pbc, inst, op.pauli())
            yield {"Rotation": {"basis": basis, "angle": repr(-sign * float(op.params[0]))}}
        elif op.name == "pauli_product_measurement":
            basis, sign = _basis(pbc, inst, op.pauli())
            yield {"Measurement": {"basis": basis, "flip_result": sign == -1}}
        else:
            raise ValueError(f"unsupported instruction in PBC circuit: {op.name}")
