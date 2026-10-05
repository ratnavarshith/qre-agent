from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "assumptions" / "default.yaml"


@dataclass(frozen=True)
class Assumptions:
    qec: str
    physical_error_rate: float
    gate_time_ns: int
    two_qubit_gate_time_ns: int
    measurement_time_ns: int
    code_cycle_ns: int
    error_budget: float
    basis_gates: tuple[str, ...]
    optimization_level: int
    seed: int

    def as_dict(self):
        return asdict(self)


def load_assumptions(path=DEFAULT_PATH):
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    t = raw.pop("transpile")
    return Assumptions(
        **raw,
        basis_gates=tuple(t["basis_gates"]),
        optimization_level=t["optimization_level"],
        seed=t["seed"],
    )
