"""On-disk cache of estimate results under cache/estimates/, keyed on everything a result depends
on: the circuit after prepare(), the assumptions, the tool versions and, for bicycle, the
measurement table. See docs/agent-design.md (Estimate cache)."""

import hashlib
import json
import os
from functools import cache
from importlib.metadata import version
from pathlib import Path

from . import bicycle
from .assumptions import REPO_ROOT
from .compiler import compiler_versions, measurement_table, table_sha256
from .surface import PACKAGES, prepare

CACHE_DIR = REPO_ROOT / "cache" / "estimates"
SOURCES = ("surface.py", "bicycle.py", "pbc.py", "compiler.py")  # the code behind an estimate


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def circuit_hash(circuit, a):
    """Hash of the circuit both estimators see: prepare()'s output and its dropped error."""
    prepared, dropped = prepare(circuit, a)
    qubits = {q: i for i, q in enumerate(prepared.qubits)}
    clbits = {c: i for i, c in enumerate(prepared.clbits)}
    lines = [f"{prepared.num_qubits} {prepared.num_clbits} {dropped!r}"]
    for inst in prepared.data:
        params = ",".join(map(repr, inst.operation.params))
        q = ",".join(str(qubits[b]) for b in inst.qubits)
        c = ",".join(str(clbits[b]) for b in inst.clbits)
        lines.append(f"{inst.operation.name}({params}) {q} {c}")
    return _sha256("\n".join(lines))


@cache
def tool_versions(a):
    """Package and compiler versions, and a hash of our estimator code."""
    try:
        compiler = compiler_versions(a)
    except FileNotFoundError:  # surface without rotations doesn't need the compiler
        compiler = {}
    here = Path(__file__).parent
    code = _sha256("".join((here / f).read_text(encoding="utf-8") for f in SOURCES))
    return {pkg: version(pkg) for pkg in PACKAGES} | compiler | {"qre_agent_estimators": code}


def cache_key(kind, circuit, a):
    """Key of a `kind` ("surface" or "bicycle") estimate of `circuit` under assumptions `a`."""
    parts = {
        "kind": kind,
        "circuit": circuit_hash(circuit, a),
        "assumptions": a.as_dict(),
        "versions": tool_versions(a),
    }
    if kind == "bicycle" and a.bicycle_code in bicycle.MODULE:  # else the estimate raises
        parts["measurement_table_sha256"] = table_sha256(measurement_table(a, a.bicycle_code))
    return _sha256(json.dumps(parts, sort_keys=True))


class EstimateCache:
    """Results stored as <key>.json. A file that doesn't parse or holds another key is a miss."""

    def __init__(self, directory=CACHE_DIR):
        self.directory = Path(directory)
        self.hits = self.misses = 0

    def _path(self, key):
        return self.directory / f"{key}.json"

    def get(self, key):
        try:
            entry = json.loads(self._path(key).read_text(encoding="utf-8"))
            if entry["key"] == key and isinstance(entry["result"], dict):
                self.hits += 1
                return entry["result"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.misses += 1
        return None

    def put(self, key, result):
        """Store `result`; returns it as a hit will return it (through JSON, so tuples are lists)."""
        text = json.dumps({"key": key, "result": result})
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self._path(key).with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self._path(key))  # atomic: a reader sees the old file or the new one
        return json.loads(text)["result"]
