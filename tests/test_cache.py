import json
import math
from dataclasses import replace

import pytest
from qiskit import QuantumCircuit

from qre_agent import cache as estimate_cache
from qre_agent import load_assumptions, surface
from qre_agent.cache import EstimateCache, cache_key
from qre_agent.tools import Toolbox

A = load_assumptions()


def t_circuit(extra=None):
    c = QuantumCircuit(2)
    c.h(0)
    c.t(0)
    c.cx(0, 1)
    if extra:
        extra(c)
    c.measure_all()
    return c


@pytest.fixture
def counted(monkeypatch):
    """estimate_surface replaced by a fake that counts its calls."""
    calls = []

    def fake(circuit, a):
        calls.append(circuit)
        return {"physical_qubits": 100 + len(calls), "error": 1e-5, "basis": a.basis_gates}

    monkeypatch.setattr(surface, "estimate_surface", fake)
    return calls


def estimate(directory, circuit):
    tb = Toolbox(timeout=None, cache=EstimateCache(directory))
    tb.circuits["c1"] = circuit
    return tb, tb.call("estimate_surface", {"circuit_id": "c1"})


def test_a_hit_returns_the_identical_result_without_estimating(tmp_path, counted):
    tb1, out1 = estimate(tmp_path, t_circuit())
    tb2, out2 = estimate(tmp_path, t_circuit())
    assert len(counted) == 1
    assert out1 == out2 and tb1.results == tb2.results
    assert json.loads(out2)["basis"] == list(A.basis_gates)  # a miss returns what a hit will
    assert (tb1.cache.hits, tb1.cache.misses, tb2.cache.hits) == (0, 1, 1)


@pytest.mark.compiler
def test_a_hit_returns_the_identical_result_on_both_estimators(tmp_path, monkeypatch):
    def run():
        tb = Toolbox(timeout=None, cache=EstimateCache(tmp_path))
        tb.call("build_benchmark", {"family": "qft", "n": 3})
        outs = [tb.call("estimate_surface", {"circuit_id": "c1"})]
        outs.append(tb.call("estimate_bicycle", {"circuit_id": "c1"}))
        return tb, outs

    tb1, outs1 = run()

    def refuse(*args):
        raise AssertionError("estimated on a hit")

    monkeypatch.setattr("qre_agent.surface.estimate_surface", refuse)
    monkeypatch.setattr("qre_agent.bicycle.estimate_bicycle", refuse)
    tb2, outs2 = run()
    assert outs1 == outs2 and tb1.results == tb2.results
    assert [json.loads(o)["result_id"] for o in outs2] == ["r1", "r2"]
    assert tb2.cache.hits == 2


@pytest.fixture
def fixed_table(monkeypatch):
    """The bicycle key's measurement table hash, settable without generating a table."""
    table = {"sha": "a"}
    monkeypatch.setattr(estimate_cache, "measurement_table", lambda a, code: code)
    monkeypatch.setattr(estimate_cache, "table_sha256", lambda path: table["sha"])
    return table


def test_a_circuit_that_prepares_the_same_has_the_same_key(fixed_table):
    # rz(0) and rz(2π) are dropped by prepare(), with no error
    same = t_circuit(lambda c: (c.rz(0, 1), c.rz(2 * math.pi, 0)))
    for kind in ("surface", "bicycle"):
        assert cache_key(kind, same, A) == cache_key(kind, t_circuit(), A)


@pytest.mark.parametrize(
    "change",
    [
        "circuit",
        "angle",
        "dropped error",
        "kind",
        "physical_error_rate",
        "error_budget",
        "bicycle_code",
        "synthesis",
        "tool version",
        "measurement table",
    ],
)
def test_any_change_to_the_key_misses(change, fixed_table, monkeypatch):
    base = cache_key("bicycle", t_circuit(), A)
    circuit, kind, a = t_circuit(), "bicycle", A
    if change == "circuit":
        circuit = t_circuit(lambda c: c.t(1))
    elif change == "angle":
        circuit = t_circuit(lambda c: c.rz(0.3, 1))
        base = cache_key(kind, circuit, a)
        circuit = t_circuit(lambda c: c.rz(0.3 + 1e-15, 1))
    elif change == "dropped error":  # under prepare()'s ZERO_ANGLE: dropped, but its error counts
        circuit = t_circuit(lambda c: c.rz(1e-10, 1))
    elif change == "kind":
        kind = "surface"
    elif change == "tool version":
        versions = estimate_cache.tool_versions(A) | {"qiskit": "0.0.0"}
        monkeypatch.setattr(estimate_cache, "tool_versions", lambda a: versions)
    elif change == "measurement table":
        fixed_table["sha"] = "b"
    else:
        a = replace(A, **{change: {"physical_error_rate": 1e-4, "error_budget": 1e-2,
                                   "bicycle_code": "gross", "synthesis": "native"}[change]})  # fmt: skip
    assert cache_key(kind, circuit, a) != base


def test_the_measurement_table_is_not_in_a_surface_key(fixed_table):
    before = cache_key("surface", t_circuit(), A)
    fixed_table["sha"] = "b"
    assert cache_key("surface", t_circuit(), A) == before


@pytest.mark.parametrize(
    "content",
    ["", "{not json", "[]", '{"result": {}}', '{"key": "other", "result": {}}', "KEY_WITH_3"],
)
def test_a_corrupted_cache_file_is_ignored_and_replaced(content, tmp_path, counted):
    estimate(tmp_path, t_circuit())
    (path,) = tmp_path.glob("*.json")
    if content == "KEY_WITH_3":
        content = json.dumps({"key": path.stem, "result": 3})
    path.write_text(content, encoding="utf-8")

    tb, out = estimate(tmp_path, t_circuit())
    assert len(counted) == 2 and (tb.cache.hits, tb.cache.misses) == (0, 1)
    assert json.loads(out)["physical_qubits"] == 102
    assert json.loads(path.read_text(encoding="utf-8"))["key"] == path.stem  # rewritten
    assert estimate(tmp_path, t_circuit())[1] == out


def test_an_estimate_that_fails_is_not_cached(tmp_path, monkeypatch):
    def fail(circuit, a):
        raise RuntimeError("compiler exited with 1")

    monkeypatch.setattr(surface, "estimate_surface", fail)
    _, out = estimate(tmp_path, t_circuit())
    assert json.loads(out) == {"error": "RuntimeError: compiler exited with 1"}
    assert list(tmp_path.iterdir()) == []
