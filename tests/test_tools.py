import inspect
import json
from pathlib import Path

import pytest
from qiskit import QuantumCircuit

from qre_agent.tools import (
    ALLOWED_IMPORTS,
    SCHEMAS,
    SandboxError,
    Toolbox,
    check_code,
    run_circuit_code,
)

QFT4 = """
from qiskit import QuantumCircuit
from qiskit.circuit.library import QFTGate
circuit = QuantumCircuit(4)
circuit.append(QFTGate(4), range(4))
circuit.measure_all()
"""
EMPTY = "from qiskit import QuantumCircuit\ncircuit = QuantumCircuit(1)\n"
# Reaches os through an allowed module; passes the static check, so the audit hook must stop it.
ESCAPE = "import qiskit.utils.parallel as p\nos = p.os\n"
INSTALLED = (  # a file in the Python install, found without dunder access
    "sp = next(d for d in os.sys.path if d.endswith('site-packages'))\n"
    "installed = os.path.join(sp, 'qiskit', '__init__.py')\n"
)
REPO = Path(__file__).resolve().parents[1]


def test_sandbox_returns_the_circuit():
    circuit = run_circuit_code(QFT4)
    assert circuit.num_qubits == 4
    assert circuit.count_ops()["qft"] == 1


def test_sandbox_times_out():
    with pytest.raises(SandboxError, match="timed out after 2 s"):
        run_circuit_code("while True:\n    pass\n", timeout=2)


@pytest.mark.parametrize(
    "call", ["socket.create_connection(('example.com', 80))", "socket.socket()"]
)
def test_sandbox_blocks_network(call):
    # socket is allowed here only to reach the network layer; agents cannot import it
    with pytest.raises(SandboxError, match="network access is disabled"):
        run_circuit_code(f"import socket\n{call}\n", allowed=(*ALLOWED_IMPORTS, "socket"))


@pytest.mark.parametrize(
    "code, reason",
    [
        ("import os", "import of 'os'"),
        ("from subprocess import run", "import of 'subprocess'"),
        ("import qiskit, urllib.request", "import of 'urllib.request'"),
        ("from . import tools", "import of '.'"),
        ("__import__('os')", "'__import__'"),
        ("open('x', 'w')", "'open'"),
        ("x = ().__class__", "'__class__'"),
    ],
)
def test_sandbox_rejects_forbidden_code_before_running(code, reason):
    with pytest.raises(SandboxError, match=reason):
        check_code(code)
    with pytest.raises(SandboxError, match=reason):
        run_circuit_code(code)


def test_sandbox_does_not_pass_api_keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-a-real-key")
    code = "import os\nassert 'OPENROUTER_API_KEY' not in os.environ\n" + EMPTY
    assert run_circuit_code(code, allowed=(*ALLOWED_IMPORTS, "os")).num_qubits == 1


def test_escape_gets_past_the_static_check():
    check_code(ESCAPE + "os.getcwd()")


@pytest.mark.parametrize(
    "call",
    [
        f"os.sys.modules['builtins'].open({str(REPO / '.env')!r}).read()",
        f"os.open({str(REPO / '.env')!r}, os.O_RDONLY)",
        f"os.open({str(REPO / 'README.md')!r}, os.O_RDONLY)",
        INSTALLED + "os.open(installed, os.O_WRONLY)",  # the Python install is read-only
    ],
)
def test_sandbox_blocks_files_outside_its_run_dir(call):
    with pytest.raises(SandboxError, match="sandbox: opening .* is blocked"):
        run_circuit_code(ESCAPE + call)


@pytest.mark.parametrize(
    "call, event",
    [
        ("os.system('echo hi')", "os.system"),
        ("os.popen('echo hi').read()", "subprocess.Popen"),
        ("os.execv(os.sys.executable, ['python'])", "os.exec"),
        ("os.spawnl(os.P_WAIT, os.sys.executable, 'python')", "os.spawn"),
    ],
)
def test_sandbox_blocks_processes(call, event):
    with pytest.raises(SandboxError, match=f"sandbox: {event} is blocked"):
        run_circuit_code(ESCAPE + call)


def test_sandbox_blocks_ctypes():
    reach = ESCAPE + "import numpy.ctypeslib as cl\nct = cl.ctypes\n"
    for call in ("ct.pythonapi.Py_GetVersion()", "ct.string_at(id(0), 1)", "ct.CDLL('x')"):
        with pytest.raises(SandboxError, match="sandbox: ctypes.[a-z_]+ is blocked"):
            run_circuit_code(reach + call)


def test_sandbox_blocks_filesystem_changes(tmp_path):
    with pytest.raises(SandboxError, match="sandbox: os.remove is blocked"):
        run_circuit_code(ESCAPE + f"os.remove({str(tmp_path / 'x')!r})")


def test_sandbox_allows_its_run_dir_and_reading_the_python_install():
    code = (
        ESCAPE
        + INSTALLED
        + "os.close(os.open('scratch.txt', os.O_WRONLY | os.O_CREAT))\n"
        + "os.close(os.open(installed, os.O_RDONLY))\n"
        + EMPTY
    )
    assert run_circuit_code(code).num_qubits == 1


@pytest.mark.parametrize(
    "code, error",
    [
        ("x = 1 / 0", "ZeroDivisionError"),
        ("circuit = 3", "must be a QuantumCircuit"),
        ("import os", "not allowed"),
        ("def f(:", "SyntaxError"),
    ],
)
def test_build_circuit_returns_the_error_to_the_agent(code, error):
    out = json.loads(Toolbox().call("build_circuit", {"code": code}))
    assert error in out["error"]


def test_schemas_match_the_tool_signatures():
    for schema in SCHEMAS:
        params = inspect.signature(getattr(Toolbox, schema["name"])).parameters
        required = [n for n, p in params.items() if n != "self" and p.default is p.empty]
        assert list(schema["parameters"]["properties"]) == [n for n in params if n != "self"]
        assert schema["parameters"]["required"] == required


def test_unknown_tool_and_wrong_result_type_are_errors():
    tb = Toolbox()
    assert "unknown tool" in json.loads(tb.call("rm", {}))["error"]
    tb.results["r1"] = {"architecture": "bicycle"}
    out = tb.call("verify", {"surface_result": "r1", "bicycle_result": "r1", "final_answer": ""})
    assert "not surface" in json.loads(out)["error"]


@pytest.mark.compiler
def test_tools_end_to_end_on_qft4():
    tb = Toolbox()
    circuit = json.loads(tb.call("build_circuit", {"code": QFT4}))
    s = json.loads(tb.call("estimate_surface", {"circuit_id": circuit["circuit_id"]}))
    b = json.loads(tb.call("estimate_bicycle", {"circuit_id": "c1", "code": "two-gross"}))
    assert (s["result_id"], b["result_id"]) == ("r1", "r2")
    assert "frontier" not in s and s["circuit"] == b["circuit"] == {
        "circuit_id": "c1",
        "num_qubits": 4,
    }

    keys = ("result_id", "architecture", "physical_qubits", "runtime_ns", "physical_qubit_seconds")
    answer = {
        "summary": (
            f"Surface: {s['physical_qubits']:,} physical qubits, "
            f"{s['physical_qubit_seconds']:.3g} qubit-seconds. Two-gross: "
            f"{b['physical_qubits']:,} qubits, {b['physical_qubit_seconds']:.1f} qubit-seconds "
            "at p = 1e-3."
        ),
        "estimates": [{k: r[k] for k in keys} for r in (s, b)],
    }
    args = {"surface_result": "r1", "bicycle_result": "r2", "final_answer": json.dumps(answer)}
    assert json.loads(tb.call("verify", args)) == {"passed": True, "failures": []}

    answer["summary"] += " That saves about 5000 qubits."
    args["final_answer"] = json.dumps(answer)
    assert json.loads(tb.call("verify", args))["failures"][0]["check"] == "numbers_match"


def test_every_enum_is_a_string_enum():
    # Gemini's function-calling schema allows enum on STRING only; a numeric enum made Gemini call
    # estimate_bicycle with {} in every trial task.
    for schema in SCHEMAS:
        for name, prop in schema["parameters"]["properties"].items():
            if "enum" in prop:
                assert prop["type"] == "string", (schema["name"], name)
                assert all(isinstance(v, str) for v in prop["enum"]), (schema["name"], name)


@pytest.mark.parametrize("p, expected", [("1e-3", 1e-3), ("1e-4", 1e-4), (1e-4, 1e-4)])
def test_bicycle_error_rate_is_converted_from_its_string(monkeypatch, p, expected):
    seen = []
    monkeypatch.setattr(
        "qre_agent.tools.bicycle.estimate_bicycle",
        lambda circuit, a: seen.append(a.physical_error_rate) or {},
    )
    tb = Toolbox()
    tb.circuits["c1"] = QuantumCircuit(1)
    out = json.loads(tb.call("estimate_bicycle", {"circuit_id": "c1", "physical_error_rate": p}))
    assert "error" not in out and seen == [expected]


def test_bicycle_error_rate_outside_the_models_is_a_clear_error():
    tb = Toolbox()
    tb.circuits["c1"] = QuantumCircuit(1)
    out = tb.call("estimate_bicycle", {"circuit_id": "c1", "physical_error_rate": "1e-2"})
    assert "'1e-3' or '1e-4'" in json.loads(out)["error"]


TWO = "from qiskit import QuantumCircuit\ncircuit = QuantumCircuit(2)\n"
RESET_CODE = [
    TWO + "circuit.reset(0)\n",
    TWO + "circuit.initialize([0, 1], 0)\n",
    TWO
    + "inner = QuantumCircuit(1)\ninner.reset(0)\ncircuit.append(inner.to_instruction(), [1])\n",
]


@pytest.mark.parametrize("code", RESET_CODE)
def test_build_circuit_rejects_resets(code):
    tb = Toolbox()
    error = json.loads(tb.call("build_circuit", {"code": code}))["error"]
    assert "reset" in error and "bicycle compiler" in error
    assert tb.circuits == {}


def test_build_circuit_description_warns_about_resets():
    (schema,) = [s for s in SCHEMAS if s["name"] == "build_circuit"]
    assert "reset" in schema["description"] and "initialize" in schema["description"]


@pytest.mark.parametrize(
    "family, n, qubits",
    [("qft", 4, 4), ("qpe", 4, 5), ("tfim", 4, 4), ("adder", 8, 18), ("grover", 8, 14)],
)
def test_build_benchmark_stores_the_family_circuit(family, n, qubits):
    from qre_agent.circuits import FAMILIES

    tb = Toolbox()
    out = json.loads(tb.call("build_benchmark", {"family": family, "n": n}))
    assert (out["circuit_id"], out["family"], out["n"], out["num_qubits"]) == (
        "c1",
        family,
        n,
        qubits,
    )
    assert tb.circuits["c1"] == FAMILIES[family](n)


@pytest.mark.parametrize(
    "args, error",
    [
        ({"family": "shor", "n": 4}, "unknown family 'shor'"),
        ({"family": "grover", "n": 2}, "n >= 3"),
    ],
)
def test_build_benchmark_errors(args, error):
    assert error in json.loads(Toolbox().call("build_benchmark", args))["error"]


def test_system_prompt_points_named_families_to_build_benchmark():
    from qre_agent.agent import system_prompt
    from qre_agent.assumptions import load_assumptions

    text = system_prompt(load_assumptions())
    assert "build_benchmark" in text and "ripple-carry adder" in text
