"""Dump the intermediate stages of the comparison benchmarks, so two platforms can be diffed stage
by stage: installed versions, gate counts after prepare(), the rotation angles, the bicycle PBC
ops, and rsgridsynth's T count per angle at the accuracy the estimators use.

Run: .venv/Scripts/python scripts/diagnose_platform.py results/comparison/config.yaml OUT.json
"""

import hashlib
import json
import platform
import sys
from importlib.metadata import distributions
from pathlib import Path

import yaml

from qre_agent import load_assumptions
from qre_agent.circuits import FAMILIES
from qre_agent.compiler import compiler_versions, gridsynth_t_counts
from qre_agent.pbc import to_pbc
from qre_agent.surface import SYNTHESIS_SHARE, _rotation_angles, prepare

ROOT = Path(__file__).resolve().parents[1]


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def main(config_path, out_path):
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    a = load_assumptions(ROOT / cfg["assumptions"])
    rows = []
    for family, sizes in cfg["families"].items():
        for n in sizes:
            circuit = FAMILIES[family](n)
            prepared, dropped = prepare(circuit, a)
            angles = _rotation_angles(prepared)
            ops, _ = to_pbc(circuit, a)
            row = {
                "family": family,
                "n": n,
                "ops": dict(sorted(prepared.count_ops().items())),
                "dropped": dropped,
                "angles": [repr(phi) for phi in angles],
                "pbc_ops": len(ops),
                "pbc_sha": sha(ops),
            }
            if angles:  # surface's gridsynth query, which also fixes bicycle's epsilon
                eps = SYNTHESIS_SHARE * a.error_budget / len(angles)
                row["t_counts"] = gridsynth_t_counts(a, angles, eps / 2)
            rows.append(row)
            print(family, n, len(angles), row["pbc_sha"], flush=True)
    meta = {
        "packages": {d.metadata["Name"].lower(): d.version for d in distributions()},
        "compiler": compiler_versions(a),
        "platform": platform.platform(),
        "python": platform.python_version(),
    }
    Path(out_path).write_text(json.dumps({"meta": meta, "rows": rows}, indent=1), "utf-8")


if __name__ == "__main__":
    main(*sys.argv[1:])
