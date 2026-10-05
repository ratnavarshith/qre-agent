"""Surface vs bicycle across benchmark families, driven by a config file. Writes results.json,
table.md and one plot per physical error rate next to the config.

Run: .venv/Scripts/python scripts/compare.py results/comparison/config.yaml
"""

import json
import platform
import sys
from dataclasses import replace
from pathlib import Path

import matplotlib
import yaml

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

from qre_agent import estimate_bicycle, estimate_surface, load_assumptions
from qre_agent.bicycle import PAPER_ERRORS, instruction_error
from qre_agent.circuits import FAMILIES

ROOT = Path(__file__).resolve().parents[1]
KEEP = (
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
    "dropped_error",
    "error",
)
BICYCLE_KEEP = ("timesteps", "modules", "instruction_counts", "error_breakdown")
# Reference categorical slots 1-3 (dataviz palette.md), in fixed order; marker and line style
# carry identity too, so it never rests on color alone.
STYLE = {
    "surface": {"color": "#2a78d6", "marker": "o", "linestyle": "-"},
    "gross": {"color": "#eb6834", "marker": "s", "linestyle": "--"},
    "two-gross": {"color": "#1baf7a", "marker": "^", "linestyle": ":"},
}
INK, MUTED, SURFACE = "#0b0b0b", "#52514e", "#fcfcfb"


def run(cfg):
    base = replace(load_assumptions(ROOT / cfg["assumptions"]), timestep_ns=cfg["timestep_ns"])
    rows = []
    for family, sizes in cfg["families"].items():
        for n in sizes:
            circuit = FAMILIES[family](n)
            for p in cfg["physical_error_rates"]:
                a = replace(base, physical_error_rate=p)
                for arch in cfg["architectures"]:
                    if arch == "surface":
                        r = estimate_surface(circuit, a)
                        passes = r["error"] <= a.error_budget
                    else:
                        r = estimate_bicycle(circuit, replace(a, bicycle_code=arch))
                        passes = r["passes"]
                    row = {"family": family, "n": n, "p": p, "arch": arch, "passes": passes}
                    row |= {k: r[k] for k in KEEP}
                    if arch != "surface":
                        row |= {k: r[k] for k in BICYCLE_KEEP}
                    rows.append(row)
    return rows, base, r["versions"]


def _key(r):
    return r["family"], r["n"], r["p"]


def crossover_ns(rows, row):
    """Timestep at which bicycle's qubit-seconds equal surface's (runtime is linear in it)."""
    s = next(r for r in rows if _key(r) == _key(row) and r["arch"] == "surface")
    return s["physical_qubit_seconds"] / (row["physical_qubits"] * row["timesteps"] * 1e-9)


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
            "| family | n | p | arch | physical qubits | runtime (ms) | qubit-seconds | error | "
            f"pass | qubit-s at {' / '.join(map(str, cfg['timestep_sweep_ns']))} ns | "
            "crossover timestep (ns) |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r["arch"] == "surface" or not r["passes"]:
            sweep = cross = "n/a"
        else:
            sweep = " / ".join(
                f"{r['physical_qubits'] * r['timesteps'] * t * 1e-9:.3g}"
                for t in cfg["timestep_sweep_ns"]
            )
            cross = f"{crossover_ns(all_rows or rows, r):.1f}"
        lines.append(
            f"| {r['family']} | {r['n']} | {r['p']:.0e} | {r.get('label', r['arch'])} | "
            f"{r['physical_qubits']:,} | {r['runtime_ns'] * 1e-6:.3g} | "
            f"{r['physical_qubit_seconds']:.3g} | {r['error']:.2e} | "
            f"{'yes' if r['passes'] else 'FAILS budget'} | {sweep} | {cross} |"
        )
    return "\n".join(lines) + "\n"


def plot(rows, cfg, p, path):
    """Qubit-seconds vs size, one panel per family; bicycle points only where they pass."""
    families = list(cfg["families"])
    fig, axes = plt.subplots(1, len(families), figsize=(3.2 * len(families), 3.4), sharey=True)
    fig.patch.set_facecolor(SURFACE)
    for ax, family in zip(axes, families):
        ax.set_facecolor(SURFACE)
        for arch in cfg["architectures"]:
            pts = [
                (r["n"], r["physical_qubit_seconds"])
                for r in rows
                if (r["family"], r["p"], r["arch"]) == (family, p, arch) and r["passes"]
            ]
            if pts:
                ax.plot(*zip(*pts), label=arch, linewidth=2, markersize=6, **STYLE[arch])
        ax.set_yscale("log")
        ax.set_xscale("log", base=2)
        ax.set_xticks(cfg["families"][family], [str(n) for n in cfg["families"][family]])
        ax.minorticks_off()
        ax.set_title(family, color=INK, fontsize=11)
        ax.set_xlabel("size n", color=MUTED)
        ax.grid(True, which="major", color="#e4e3df", linewidth=0.8)
        ax.tick_params(colors=MUTED, labelsize=9)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#c9c8c2")
    axes[0].set_ylabel("physical qubit-seconds", color=MUTED)
    handles = []
    for arch in cfg["architectures"]:
        passing = any(r["passes"] for r in rows if (r["p"], r["arch"]) == (p, arch))
        label = arch if passing else f"{arch} (fails budget at every size)"
        handles.append(Line2D([], [], linewidth=2, markersize=6, label=label, **STYLE[arch]))
    fig.legend(handles=handles, loc="upper right", frameon=False, ncol=len(handles), fontsize=9)
    fig.suptitle(
        f"Physical qubit-seconds, p = {p:.0e}, timestep {cfg['timestep_ns']} ns "
        "(bicycle shown only where it meets the error budget)",
        x=0.01,
        ha="left",
        color=INK,
        fontsize=11,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


FACTORY_NOTE = (
    "\n**Note on the p=1e-4 rows.** two-gross at 1e-4 is dominated by the paper's distillation "
    "factory (18,600 qubits; output error 6e-25, which the paper calls very conservative). The "
    "paper uses cultivation only at 1e-3 because no cultivation estimates exist at 1e-4 (Tour de "
    "gross Sec. 2.5, Table 3). Surface uses v3's own factory search. So the 1e-4 rows compare "
    "factory choices as much as architectures.\n"
)
SENSITIVITY_NOTE = (
    "\n**Sensitivity, not the main result.** The same compiled circuits re-scored with paper "
    "Table 2 error rates where bicycle_numerics' gross p=1e-4 model differs 10x: T injection "
    "9.0e-8 (code 8.79e-7) and shift automorphism 6.3e-13 (code 6.07e-14). Timings and qubit "
    "counts are unchanged, so crossovers are too.\n\n"
)


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
    text = table(rows, cfg) + FACTORY_NOTE + SENSITIVITY_NOTE + table(sens, cfg, rows)
    (out / "table.md").write_text(text, encoding="utf-8")
    for p in cfg["physical_error_rates"]:
        plot(rows, cfg, p, out / f"qubit_seconds_p{p:.0e}.png")
    print(text)


if __name__ == "__main__":
    main(sys.argv[1])
