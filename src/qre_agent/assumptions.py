import os
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "assumptions" / "default.yaml"


@dataclass(frozen=True)
class Assumptions:
    qec: str
    physical_error_rate: float
    gate_time_ns: int
    two_qubit_gate_time_ns: int
    measurement_time_ns: int
    code_cycle_ns: int
    error_budget: float
    synthesis: str
    bicycle_code: str
    timestep_ns: float
    compiler_dir: str
    cache_dir: str
    basis_gates: tuple[str, ...]
    optimization_level: int
    seed: int

    def as_dict(self):
        """For recording with results; the two directories are left out, they differ per machine."""
        return {k: v for k, v in asdict(self).items() if k not in ("compiler_dir", "cache_dir")}


def _resolve(path):
    path = Path(path)
    return str(path if path.is_absolute() else REPO_ROOT / path)


def load_assumptions(path=DEFAULT_PATH):
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    t = raw.pop("transpile")
    b = raw.pop("bicycle")
    return Assumptions(
        **raw,
        bicycle_code=b["code"],
        timestep_ns=b["timestep_ns"],
        compiler_dir=_resolve(os.environ.get("QRE_COMPILER_DIR", b["compiler_dir"])),
        cache_dir=_resolve(b["cache_dir"]),
        basis_gates=tuple(t["basis_gates"]),
        optimization_level=t["optimization_level"],
        seed=t["seed"],
    )
