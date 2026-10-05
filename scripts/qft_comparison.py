"""Surface vs bicycle on QFTs, driven by a config file; writes results.json and table.md next to it.

Run: .venv/Scripts/python scripts/qft_comparison.py results/qft_comparison/config.yaml
"""

import json
import platform
import sys
from dataclasses import replace
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from circuits import qft

from qre_agent import estimate_bicycle, estimate_surface, load_assumptions
from qre_agent.bicycle import PAPER_ERRORS, instruction_error

ROOT = Path(__file__).resolve().parents[1]


def run(cfg):
    base = replace(load_assumptions(ROOT / cfg["assumptions"]), timestep_ns=cfg["timestep_ns"])
    rows = []
    for n in cfg["qft_sizes"]:
        for p in cfg["physical_error_rates"]:
            for arch in cfg["architectures"]:
                a = replace(base, physical_error_rate=p)
                if arch == "surface":
                    r = estimate_surface(qft(n), a)
                    passes = r["error"] <= a.error_budget
                else:
                    r = estimate_bicycle(qft(n), replace(a, bicycle_code=arch))
                    passes = r["passes"]
                keep = (
                    "physical_qubits",
                    "runtime_ns",
                    "physical_qubit_seconds",
                    "distance",
                    "logical_qubits",
                    "ts_per_rotation",
                    "t_count",
                    "rotation_count",
                    "synthesis_epsilon",
                    "synthesis_error",
                    "error",
                )
                row = {"n": n, "p": p, "arch": arch, "passes": passes, **{k: r[k] for k in keep}}
                if arch != "surface":
                    row |= {
                        k: r[k]
                        for k in ("timesteps", "modules", "instruction_counts", "error_breakdown")
                    }
                rows.append(row)
    return rows, base, r["versions"]


def crossover_ns(rows, n, p, arch):
    """Timestep at which bicycle's qubit-seconds equal surface's (runtime is linear in it)."""
    s = next(r for r in rows if (r["n"], r["p"], r["arch"]) == (n, p, "surface"))
    b = next(r for r in rows if (r["n"], r["p"], r["arch"]) == (n, p, arch))
    return s["physical_qubit_seconds"] / (b["physical_qubits"] * b["timesteps"] * 1e-9)


def sensitivity(rows, cfg):
    """Bicycle rows re-scored with paper Table 2 error rates (same compiled circuit and timings)."""
    out = []
    for code, p in map(tuple, cfg["paper_error_sensitivity"]):
        for r in rows:
            if (r["arch"], r["p"]) != (code, p):
                continue
            b = r["error_breakdown"]
            instructions = instruction_error(r["instruction_counts"], PAPER_ERRORS[(code, p)])
            error = instructions + b["synthesis"]
            out.append(
                r
                | {
                    "label": f"{code} (paper errors)",
                    "error": error,
                    "passes": error <= b["budget"],
                    "error_breakdown": b | {"instructions": instructions, "total": error},
                }
            )
    return out


def table(rows, cfg, all_rows=None):
    lines = [
        (
            "| QFT | p | arch | physical qubits | runtime (ms) | qubit-seconds | error | pass | "
            f"qubit-s at {' / '.join(map(str, cfg['timestep_sweep_ns']))} ns | "
            "crossover timestep (ns) |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r["arch"] == "surface" or not r["passes"]:
            sweep = cross = "n/a"
        else:
            sweep = " / ".join(
                f"{r['physical_qubits'] * r['timesteps'] * t * 1e-9:.3g}"
                for t in cfg["timestep_sweep_ns"]
            )
            cross = f"{crossover_ns(all_rows or rows, r['n'], r['p'], r['arch']):.1f}"
        lines.append(
            f"| {r['n']} | {r['p']:.0e} | {r.get('label', r['arch'])} | {r['physical_qubits']:,} | "
            f"{r['runtime_ns'] * 1e-6:.3g} | {r['physical_qubit_seconds']:.3g} | {r['error']:.2e} | "
            f"{'yes' if r['passes'] else 'FAILS budget'} | {sweep} | {cross} |"
        )
    return "\n".join(lines) + "\n"


def main(config_path):
    config_path = Path(config_path)
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    rows, base, versions = run(cfg)
    sens = sensitivity(rows, cfg)
    meta = {
        "assumptions": base.as_dict(),
        "versions": versions,
        "seeds": {"transpile": base.seed, "rsgridsynth": "1 (fixed in compiler small_angle.rs)"},
        "hardware": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python": platform.python_version(),
        },
    }
    out = config_path.parent
    results = {"meta": meta, "rows": rows, "paper_error_sensitivity": sens}
    (out / "results.json").write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    text = (
        table(rows, cfg)
        + "\n**Note on the p=1e-4 rows.** two-gross at 1e-4 is dominated by the paper's distillation "
        "factory (18,600 of its 19,383-20,151 qubits; output error 6e-25, which the paper calls very "
        "conservative). The paper uses cultivation only at 1e-3 because no cultivation estimates "
        "exist at 1e-4 (Tour de gross Sec. 2.5, Table 3). Surface uses v3's own factory search. So "
        "the 1e-4 rows compare factory choices as much as architectures.\n"
        + "\n**Sensitivity, not the main result.** The same compiled circuits re-scored with paper "
        "Table 2 error rates where bicycle_numerics' gross p=1e-4 model differs 10x: T injection "
        "9.0e-8 (code 8.79e-7) and shift automorphism 6.3e-13 (code 6.07e-14). Timings and qubit "
        "counts are unchanged, so crossovers are too.\n\n" + table(sens, cfg, rows)
    )
    (out / "table.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main(sys.argv[1])
