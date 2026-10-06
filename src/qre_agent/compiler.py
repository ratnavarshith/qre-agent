"""Calls IBM's bicycle compiler binaries by path (never edited from here). See docs/bicycle-interface.md."""

import hashlib
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
            "`cargo build --release -F bicycle_compiler/rsgridsynth` or set QRE_COMPILER_DIR "
            "(default: bicycle-compiler/target/release in the repo)."
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


def _git(root, *args):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def compiler_versions(a):
    """Identify the compiler build: version, git commit, and the Cargo.lock it was built from
    (with the rsgridsynth version in it). Paths are not recorded, they differ per machine."""
    root = Path(a.compiler_dir).parent.parent  # target/release -> repo root
    versions = {"bicycle_compiler": run(a, "bicycle_compiler", ["--version"]).split()[-1]}
    commit = _git(root, "rev-parse", "HEAD")
    if commit:
        dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
        versions["bicycle_compiler_commit"] = commit + ("-dirty" if dirty else "")
    lock = root / "Cargo.lock"
    if lock.is_file():
        packages = tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
        versions["rsgridsynth"] = next(p["version"] for p in packages if p["name"] == "rsgridsynth")
        versions["cargo_lock_sha256"] = hashlib.sha256(lock.read_bytes()).hexdigest()
    return versions
