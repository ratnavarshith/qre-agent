import pytest
from qiskit import QuantumCircuit

from qre_agent import selfcheck
from qre_agent.tools import SandboxError


def fake(result):
    """run_circuit_code that returns a circuit named `result`, or raises SandboxError(result)."""

    def run(code, timeout=60):
        if isinstance(result, Exception):
            raise result
        return QuantumCircuit(1, name=result)

    return run


@pytest.mark.parametrize(
    ("check", "outcome", "ok"),
    [
        (selfcheck.check_environment, "OMP_NUM_THREADS,PATH,RAYON_NUM_THREADS", True),
        (selfcheck.check_environment, "OPENROUTER_API_KEY,PATH", False),
        (
            selfcheck.check_memory,
            SandboxError("Traceback ...\nnumpy._core._exceptions._ArrayMemoryError: MemoryError"),
            True,
        ),
        (selfcheck.check_memory, "c", False),  # the allocation went through: no limit
        (selfcheck.check_cpu, SandboxError("the code used more than 10 s of CPU time"), True),
        (
            selfcheck.check_cpu,
            SandboxError("timed out after 60 s"),
            False,
        ),  # only the wall clock stopped it
        (selfcheck.check_file_size, SandboxError("OSError: [Errno 27] File too large"), True),
        (selfcheck.check_file_size, "c", False),
    ],
)
def test_each_check_passes_only_on_the_limits_doing_their_job(monkeypatch, check, outcome, ok):
    monkeypatch.setattr(selfcheck, "run_circuit_code", fake(outcome))
    assert check()[0] is ok


def test_the_volume_check_needs_the_write_stopped_and_the_next_build_to_work(monkeypatch):
    calls = []

    def run(code, timeout=60):
        calls.append(code)
        if len(calls) == 1:
            raise SandboxError(
                "the code exited with code 120 and no error output (the temp directory may be full)"
            )
        return QuantumCircuit(1)

    monkeypatch.setattr(selfcheck, "run_circuit_code", run)
    assert selfcheck.check_volume()[0] is True
    monkeypatch.setattr(selfcheck, "run_circuit_code", fake("c"))  # the volume never filled
    assert selfcheck.check_volume()[0] is False


def test_main_reports_each_check_and_fails_if_one_fails(capsys):
    def boom():
        raise RuntimeError("broker gone")

    assert selfcheck.main([("good", lambda: (True, "fine")), ("bad", boom)]) == 1
    out = capsys.readouterr().out
    assert "ok   good: fine" in out and "FAIL bad: RuntimeError: broker gone" in out
    assert "selfcheck FAILED: bad" in out
    assert selfcheck.main([("good", lambda: (True, "fine"))]) == 0
    assert "selfcheck passed" in capsys.readouterr().out


def test_the_user_check_compares_the_uid_the_code_saw(monkeypatch):
    """The circuit has no instructions, so it is falsy: the check must not mistake it for a
    failure (the first CI run did)."""
    monkeypatch.setattr(selfcheck, "sandbox_uid", lambda: "10002")
    monkeypatch.setattr(selfcheck, "run_circuit_code", fake("10002"))
    assert selfcheck.check_user()[0] is True
    monkeypatch.setattr(selfcheck, "run_circuit_code", fake("10001"))  # it ran as the server's user
    assert selfcheck.check_user()[0] is False
    monkeypatch.setattr(selfcheck, "run_circuit_code", fake(SandboxError("broker unavailable")))
    assert selfcheck.check_user()[0] is False
