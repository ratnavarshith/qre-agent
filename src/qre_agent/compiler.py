"""Calls IBM's bicycle compiler binaries by path (never edited from here). See docs/bicycle-interface.md."""

import json
import os
import subprocess
import tomllib
from pathlib import Path

EXE = ".exe" if os.name == "nt" else ""


def binary(a, name):
    path = Path(a.compiler_dir) / f"{name}{EXE}"
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} not found. Build the compiler with "
            "`cargo build --release -F bicycle_compiler/rsgridsynth` or fix bicycle.compiler_dir."
        )
    return path


def run(a, name, args, stdin=None):
    env = {**os.environ, "RUST_LOG": "warn"}
    cmd = [str(binary(a, name)), *args]
    r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=env, check=False)
    if r.returncode:
        raise RuntimeError(f"{name} exited with {r.returncode}: {r.stderr.strip()[-2000:]}")
    return r.stdout


def measurement_table(a, code):
    path = Path(a.cache_dir) / f"table_{code}"
    if not path.is_file():  # about a minute per code; cached afterwards
        path.parent.mkdir(parents=True, exist_ok=True)
        run(a, "bicycle_compiler", [code, "generate", str(path)])
    return path


def compile_pbc(a, code, ops, accuracy):
    """Compile PBC ops to bicycle instructions. Returns the compiler's stdout, one line per op."""
    stdin = "".join(json.dumps(op, separators=(",", ":")) + "\n" for op in ops)
    table = str(measurement_table(a, code))
    args = [code, "--measurement-table", table, "--accuracy", repr(accuracy)]
    return run(a, "bicycle_compiler", args, stdin)


def t_injections(line):
    return sum("TGate" in instr[0][1] for instr in json.loads(line))


def gridsynth_t_counts(a, angles, accuracy):
    """T count of each rotation exp(i·phi/2·Z), from the compiler's own gridsynth (rsgridsynth)."""
    ops = [{"Rotation": {"basis": ["Z"], "angle": repr(float(phi))}} for phi in angles]
    out = compile_pbc(a, a.bicycle_code, ops, accuracy)
    return [t_injections(line) for line in out.splitlines()]


def compiler_versions(a):
    """Versions of the compiler and of the rsgridsynth in the Cargo.lock it was built from."""
    versions = {"bicycle_compiler": run(a, "bicycle_compiler", ["--version"]).split()[-1]}
    lock = Path(a.compiler_dir).parent.parent / "Cargo.lock"  # target/release -> repo root
    if lock.is_file():
        packages = tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
        versions["rsgridsynth"] = next(p["version"] for p in packages if p["name"] == "rsgridsynth")
    return versions
