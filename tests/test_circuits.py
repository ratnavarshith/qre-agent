import math

import pytest

from qre_agent import estimate_bicycle, estimate_surface, load_assumptions
from qre_agent.circuits import FAMILIES, GROVER_ITERATIONS, TFIM_STEPS, cdkm_adder, grover
from qre_agent.surface import prepare

A = load_assumptions()

# Hand counts (T, non-T rotations) after the fixed-basis transpile.
EXPECTED = {
    # n(n-1)/2 controlled phases; the n-1 at π/2 give 3 rz(±π/4) = 3 T, the rest 3 rotations
    "qft": lambda n: (3 * (n - 1), 3 * math.comb(n - 1, 2)),
    # 2n Toffolis (n MAJ + n UMA), 7 T each
    "adder": lambda n: (14 * n, 0),
    # one rotation per Pauli term per step: n-1 ZZ + n X
    "tfim": lambda n: (0, TFIM_STEPS * (2 * n - 1)),
    # 2 multi-controlled Z per iteration, each 2(n-2) Toffolis
    "grover": lambda n: (GROVER_ITERATIONS * 2 * 2 * (n - 2) * 7, 0),
    # n controlled powers (3 rotations each, none Clifford for phase 1/3) + inverse QFT
    "qpe": lambda n: (3 * (n - 1), 3 * n + 3 * math.comb(n - 1, 2)),
}


@pytest.mark.compiler
@pytest.mark.parametrize("family", FAMILIES)
def test_logical_counts_match_hand_count_and_both_architectures(family):
    circuit = FAMILIES[family](4)
    surface, bicycle = estimate_surface(circuit, A), estimate_bicycle(circuit, A)
    keys = ("t_count", "rotation_count", "measurements")
    assert [surface[k] for k in keys] == [bicycle[k] for k in keys]
    assert (surface["t_count"], surface["rotation_count"]) == EXPECTED[family](4)


def test_adder_is_seven_t_per_toffoli():
    n = 5
    assert dict(cdkm_adder(n).decompose().count_ops())["ccx"] == 2 * n
    prepared = prepare(cdkm_adder(n), A)[0].count_ops()
    assert prepared["t"] + prepared["tdg"] == 7 * 2 * n


def test_grover_marked_state_only_adds_cliffords():
    def t(c):
        ops = prepare(c, A)[0].count_ops()
        return ops.get("t", 0) + ops.get("tdg", 0)

    assert t(grover(5, marked=0b01011)) == t(grover(5)) == EXPECTED["grover"](5)[0]
