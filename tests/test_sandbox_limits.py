import os
import sys

import pytest

from qre_agent.sandbox_runner import LIMITS
from qre_agent.tools import ALLOWED_IMPORTS, SandboxError, killed_by, run_circuit_code

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="resource limits: Linux")
not_root = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root is exempt from some limits"
)
BUILD = "from qiskit import QuantumCircuit\ncircuit = QuantumCircuit(1)\n"
ESCAPE = "import qiskit.utils.parallel as p\nos = p.os\n"  # as in test_tools.py
# The shape of the largest real build_circuit call in the traces: 33 qubits, a few hundred gates.
BIG = """
import math
from qiskit import QuantumCircuit
circuit = QuantumCircuit(33)
for i in range(33):
    circuit.h(i)
for r in range(6):
    for i in range(32):
        circuit.cp(math.pi / 2 ** (r + 1), i, i + 1)
        circuit.cx(i, i + 1)
circuit.measure_all()
"""


def test_the_limits_are_the_documented_values():
    assert LIMITS == {"cpu_s": 10, "memory_mb": 512, "fsize_mb": 10}


@pytest.mark.parametrize(
    ("returncode", "text"),
    [
        (-24, "used more than 10 s of CPU time"),
        (-25, "wrote a file over 10 MB"),
        (-9, "was killed (out of memory?)"),
        (-11, "was killed by SIGSEGV"),
        (-200, "was killed by signal 200"),
        (1, None),
        (0, None),
    ],
)
def test_a_signal_is_reported_in_words(returncode, text):
    assert killed_by(returncode, LIMITS) == text


@linux
def test_a_real_build_runs_inside_half_the_memory_limit():
    """The headroom: the build of a 33-qubit circuit with a few hundred gates passes with the
    memory limit halved."""
    assert run_circuit_code(BIG, limits={"memory_mb": LIMITS["memory_mb"] // 2}).num_qubits == 33


@linux
def test_a_memory_bomb_gets_a_memory_error_and_the_next_build_works():
    bomb = "import numpy as np\nx = np.ones(200_000_000)\n"  # 1.6 GB
    with pytest.raises(SandboxError, match="MemoryError"):
        run_circuit_code(bomb + BUILD)
    assert run_circuit_code(BUILD).num_qubits == 1


@linux
def test_a_cpu_hog_is_killed_at_the_cpu_limit_long_before_the_timeout():
    with pytest.raises(SandboxError, match="the code used more than 2 s of CPU time"):
        run_circuit_code("while True:\n    pass\n", timeout=60, limits={"cpu_s": 2})


@linux
def test_a_file_over_the_size_limit_cannot_be_written():
    write = (
        ESCAPE
        + "f = os.sys.modules['builtins'].open(os.getcwd() + '/big', 'wb')\n"
        + "f.write(b'x' * 3_000_000)\nf.flush()\n"
    )
    with pytest.raises(SandboxError, match="File too large"):
        run_circuit_code(write + BUILD, limits={"fsize_mb": 1})
    run_circuit_code(write.replace("3_000_000", "300_000") + BUILD, limits={"fsize_mb": 1})


@linux
@not_root
def test_the_process_limit_stops_new_threads():
    threads = "import threading\nthreading.Thread(target=print).start()\n"
    with pytest.raises(SandboxError, match="can't start new thread"):
        run_circuit_code(
            threads + BUILD, limits={"nproc": 1}, allowed=(*ALLOWED_IMPORTS, "threading")
        )
    run_circuit_code(
        threads + BUILD, allowed=(*ALLOWED_IMPORTS, "threading")
    )  # the default is fine


@linux
@not_root
def test_the_code_cannot_raise_its_own_limits():
    raise_ = (
        "import resource\n"
        "resource.setrlimit(resource.RLIMIT_DATA, (2**40, 2**40))\n"
    )  # the hard limit was lowered too
    with pytest.raises(SandboxError, match="not allowed to raise maximum limit"):
        run_circuit_code(raise_ + BUILD, allowed=(*ALLOWED_IMPORTS, "resource"))


@linux
def test_the_child_has_one_thread_per_library_and_no_other_variables(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "0" * 64)
    code = (
        ESCAPE
        + "from qiskit import QuantumCircuit\ncircuit = QuantumCircuit(1, name=','.join(sorted(os.environ)))\n"
    )
    names = run_circuit_code(code).name.split(",")
    assert "OPENROUTER_API_KEY" not in names
    assert {"OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "RAYON_NUM_THREADS"} <= set(names)
    assert set(names) <= {"PATH", "TEMP", "TMP", "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                          "RAYON_NUM_THREADS", "LC_CTYPE", "PYTHONUTF8"}  # fmt: skip
