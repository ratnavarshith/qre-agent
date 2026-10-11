import io
import logging
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from qiskit import QuantumCircuit, qpy

from qre_agent import sandbox_broker, server
from qre_agent.sandbox_broker import MAX_REQUEST, describe, exchange, handle, receive, send
from qre_agent.tools import SOCKET_ENV, TIMEOUT_S, SandboxError, run_circuit_code

linux = sys.platform.startswith("linux")
euid = os.geteuid() if hasattr(os, "geteuid") else None
BUILD = "from qiskit import QuantumCircuit\ncircuit = QuantumCircuit(2)\n"
ESCAPE = "import qiskit.utils.parallel as p\nos = p.os\n"  # as in test_tools.py


def qpy_bytes(circuit=None):
    out = io.BytesIO()
    qpy.dump(circuit or QuantumCircuit(3), out)
    return out.getvalue()


def talk(request, build):
    """Sends `request` (a message or raw bytes) to handle() over a socket pair; its reply."""
    a, b = socket.socketpair()
    raw = request if isinstance(request, bytes) else None
    t = threading.Thread(target=handle, args=(b, build))
    t.start()
    if raw is not None:
        a.sendall(raw)
    else:
        send(a, request)
    reply = receive(a, 2**24)
    t.join(5)
    assert not t.is_alive() and b.fileno() == -1  # the broker closed its end
    a.close()
    return reply


def test_a_build_is_answered_with_the_qpy_bytes():
    seen = []

    def build(code, timeout):
        seen.append((code, timeout))
        return b"\x00qpy\xff"

    reply = talk({"code": "x = 1", "timeout": 5}, build)
    assert reply["ok"] and sandbox_broker.base64.b64decode(reply["qpy"]) == b"\x00qpy\xff"
    assert seen == [("x = 1", 5.0)]


def test_the_broker_builds_with_the_process_limit_of_its_own_user(monkeypatch):
    seen = []
    monkeypatch.setattr(sandbox_broker, "build_qpy", lambda *a, **k: seen.append((a, k)) or b"")
    sandbox_broker.build_with_nproc("x", 5)
    assert seen == [(("x", 5), {"limits": {"nproc": 32}})]


def test_a_ping_is_answered_without_a_build():
    assert talk({"ping": True}, lambda *a: 1 / 0) == {"ok": True}


def test_the_clients_timeout_cannot_raise_the_brokers_cap():
    seen = []
    talk({"code": "x", "timeout": 10_000}, lambda code, t: seen.append(t) or b"")
    assert seen == [TIMEOUT_S]


def test_a_sandbox_error_goes_back_as_text():
    def build(code, timeout):
        raise SandboxError("NameError: x")

    assert talk({"code": "x"}, build) == {"ok": False, "error": "NameError: x"}


@pytest.mark.parametrize(
    "request_",
    [
        {"timeout": 5},  # no code
        {"code": 7},
        {"code": "x", "timeout": "soon"},
        b"\x00\x00\x00\x05nope!",  # not JSON
        struct.pack(">I", MAX_REQUEST + 1),  # too long: refused before it is read
        b"\x00\x00",  # cut off
    ],
)
def test_a_bad_request_gets_a_bad_request_reply_and_runs_nothing(request_):
    def build(code, timeout):
        raise AssertionError("must not run")

    if isinstance(request_, bytes) and len(request_) < 4:  # the client hangs up mid-header
        a, b = socket.socketpair()
        a.sendall(request_)
        a.shutdown(socket.SHUT_WR)
        handle(b, build)  # must return, not raise
        assert receive(a, 1024) == {"ok": False, "error": "bad request"}
        return
    assert talk(request_, build) == {"ok": False, "error": "bad request"}


def test_exchange_returns_the_bytes_or_raises_the_brokers_error():
    def over_a_pair(build):
        a, b = socket.socketpair()
        threading.Thread(target=handle, args=(b, build), daemon=True).start()
        with a:
            return exchange(a, "x", 5)

    assert over_a_pair(lambda c, t: b"data") == b"data"

    def fail(code, timeout):
        raise SandboxError("timed out after 5 s")

    with pytest.raises(SandboxError, match="timed out after 5 s"):
        over_a_pair(fail)


def test_a_broker_that_hangs_up_is_a_sandbox_error():
    a, b = socket.socketpair()
    b.close()
    with pytest.raises(SandboxError, match="the sandbox broker failed"), a:
        exchange(a, "x", 5)


def test_with_a_socket_configured_the_code_goes_to_the_broker_and_is_checked_first(monkeypatch):
    monkeypatch.setenv(SOCKET_ENV, "/run/qre/sandbox.sock")
    asked = []

    def ask(path, code, timeout):
        asked.append((path, code, timeout))
        return qpy_bytes(QuantumCircuit(3))

    monkeypatch.setattr(sandbox_broker, "ask", ask)
    monkeypatch.setattr("qre_agent.tools.build_qpy", lambda *a, **k: 1 / 0)  # never run here
    assert run_circuit_code(BUILD, timeout=7).num_qubits == 3
    assert asked == [("/run/qre/sandbox.sock", BUILD, 7)]
    with pytest.raises(SandboxError, match="import of 'os' is not allowed"):
        run_circuit_code("import os\n")
    assert len(asked) == 1  # the static check refused it before the broker saw it


def test_with_the_broker_down_there_is_no_fallback_to_running_the_code_here(monkeypatch, tmp_path):
    monkeypatch.setenv(SOCKET_ENV, str(tmp_path / "nobody-listens.sock"))
    monkeypatch.setattr("qre_agent.tools.build_qpy", lambda *a, **k: 1 / 0)
    with pytest.raises(SandboxError, match="the sandbox broker is unavailable"):
        run_circuit_code(BUILD)


@pytest.mark.parametrize(
    ("environ", "platform", "uid", "mode", "text"),
    [
        (
            {SOCKET_ENV: "/s"},
            "linux",
            1000,
            "broker",
            "behind the broker at /s, as a separate user",
        ),
        ({}, "linux", 1000, "local", "same user as this process, with resource limits"),
        ({}, "linux", 0, "root", "WARNING no broker"),
        ({}, "win32", None, "local", "without resource limits (Linux only)"),
    ],
)
def test_the_mode_line_says_how_build_circuit_runs(monkeypatch, environ, platform, uid, mode, text):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os, "geteuid", lambda: uid, raising=False)
    got_mode, line = describe(environ)
    assert got_mode == mode and text in line and line.startswith("sandbox: ")


def test_startup_logs_the_mode_and_can_require_a_broker(caplog):
    with caplog.at_level(logging.INFO, logger="qre_agent.demo"):
        server.check_sandbox({})
    assert "no broker" in caplog.text  # "WARNING no broker" when the tests run as root
    with pytest.raises(RuntimeError, match="QRE_SANDBOX_REQUIRED=1 needs the broker"):
        server.check_sandbox({"QRE_SANDBOX_REQUIRED": "1"})


def test_startup_waits_for_a_broker_that_is_still_starting(monkeypatch):
    attempts = []

    def connect(path, timeout):
        attempts.append(path)
        if len(attempts) < 4:  # its socket exists, but it is not listening yet
            raise SandboxError("the sandbox broker is unavailable (ConnectionRefusedError)")
        a, b = socket.socketpair()
        threading.Thread(target=handle, args=(b, lambda *x: b""), daemon=True).start()
        return a

    monkeypatch.setattr(sandbox_broker, "connect", connect)
    sandbox_broker.check("/run/qre/sandbox.sock", wait_s=5)
    assert len(attempts) == 4  # refused three times, then it answered


def test_startup_fails_when_the_configured_broker_does_not_answer(tmp_path):
    with pytest.raises(RuntimeError, match="does not answer"):
        server.check_sandbox({SOCKET_ENV: str(tmp_path / "x.sock")}, wait_s=0)


# A real broker on a Unix socket, as the user running the tests (not root).


def start_broker(path, **popen):
    """The broker as a subprocess; waits for its socket."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "qre_agent.sandbox_broker", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        **popen,
    )
    for _ in range(300):
        if os.path.exists(path) or proc.poll() is not None:
            break
        time.sleep(0.05)
    assert os.path.exists(path), proc.stderr.read().decode()
    return proc


@pytest.fixture
def broker(tmp_path, monkeypatch):
    path = tmp_path / "sandbox.sock"
    proc = start_broker(path, env=os.environ | {"PYTHONUNBUFFERED": "1"})
    monkeypatch.setenv(SOCKET_ENV, str(path))
    yield proc
    proc.kill()
    proc.wait()


@pytest.mark.skipif(not linux or euid == 0, reason="a Unix socket, as a non-root user")
def test_a_real_broker_builds_circuits_and_survives_a_memory_bomb(broker):
    server.check_sandbox({SOCKET_ENV: os.environ[SOCKET_ENV]})  # the startup ping
    assert run_circuit_code(BUILD).num_qubits == 2
    with pytest.raises(SandboxError, match="MemoryError"):
        run_circuit_code("import numpy as np\nx = np.ones(200_000_000)\n" + BUILD)
    with pytest.raises(SandboxError, match=r"timed out after 3(.0)? s"):
        run_circuit_code("while True:\n    pass\n", timeout=3)
    assert run_circuit_code(BUILD).num_qubits == 2
    assert oct(os.stat(os.environ[SOCKET_ENV]).st_mode & 0o777) == "0o666"


# A broker as another, unprivileged user: needs root (CI runs these with sudo; the Docker image
# will). QRE_REQUIRE_ROOT_TESTS=1 turns a skip into a failure, so CI can't skip them silently.

NEED_ROOT = os.environ.get("QRE_REQUIRE_ROOT_TESTS") == "1"
root_only = pytest.mark.skipif(
    not (linux and euid == 0) and not NEED_ROOT, reason="needs Linux and root"
)


@pytest.fixture(scope="module")
def sandbox_user():
    import pwd

    assert linux and euid == 0, "QRE_REQUIRE_ROOT_TESTS=1 but not running as root on Linux"
    entry = pwd.getpwnam(os.environ.get("QRE_TEST_SANDBOX_USER", "nobody"))
    assert entry.pw_uid != 0
    return entry


def as_user(user, code):
    """Python `code` run as `user` with an empty environment: (returncode, output)."""
    r = subprocess.run(
        [sys.executable, "-I", "-c", code],
        user=user.pw_uid, group=user.pw_gid, extra_groups=[],
        env={"PATH": os.environ.get("PATH", "")}, capture_output=True, text=True, cwd="/", check=False,
    )  # fmt: skip
    return r.returncode, r.stdout + r.stderr


@pytest.fixture
def user_broker(sandbox_user, monkeypatch):
    socket_dir = Path(tempfile.mkdtemp(prefix="qre-sock-"))
    socket_dir.chmod(0o777)  # the sandbox user creates its socket here
    path = socket_dir / "sandbox.sock"
    proc = start_broker(
        path, user=sandbox_user.pw_uid, group=sandbox_user.pw_gid, extra_groups=[], cwd="/",
        env={"PATH": os.environ.get("PATH", "")},
    )  # fmt: skip
    monkeypatch.setenv(SOCKET_ENV, str(path))
    yield proc
    proc.kill()
    proc.wait()


@root_only
def test_the_code_runs_as_the_sandbox_user_and_the_server_gets_the_circuit(
    user_broker, sandbox_user
):
    code = (
        ESCAPE
        + "from qiskit import QuantumCircuit\ncircuit = QuantumCircuit(1, name=str(os.getuid()))\n"
    )
    circuit = run_circuit_code(code)
    assert circuit.name == str(sandbox_user.pw_uid) and os.getuid() == 0


@root_only
def test_the_code_cannot_see_the_servers_environment(user_broker, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "0" * 64)
    monkeypatch.setenv("QRE_TEST_SECRET", "hunter2")
    code = (
        ESCAPE
        + "from qiskit import QuantumCircuit\ncircuit = QuantumCircuit(1, name=repr(sorted(os.environ.items())))\n"
    )
    seen = run_circuit_code(code).name
    assert "hunter2" not in seen and "OPENROUTER" not in seen and "QRE_TEST_SECRET" not in seen
    assert "OPENBLAS_NUM_THREADS" in seen


@root_only
def test_the_sandbox_user_cannot_read_the_servers_files_or_process(sandbox_user):
    directory = Path(tempfile.mkdtemp(prefix="qre-server-"))
    directory.chmod(0o755)  # anyone can look in; only the file's mode keeps the user out
    secret = directory / "server-file"
    secret.write_text("key")
    secret.chmod(0o600)  # owned by the server's user
    attempts = {
        "file": f"open({str(secret)!r}).read()",
        "environ": f"open('/proc/{os.getpid()}/environ').read()",
        "mem": f"open('/proc/{os.getpid()}/mem', 'rb').read(1)",
        "write": f"open({str(directory / 'planted')!r}, 'w').write('x')",
    }
    for name, attempt in attempts.items():
        returncode, output = as_user(sandbox_user, attempt)
        assert returncode != 0 and "PermissionError" in output, (name, output)
    assert not (directory / "planted").exists()
    assert as_user(sandbox_user, "print('ok')")[1].strip() == "ok"  # it could run at all


@root_only
def test_the_inner_layer_still_blocks_the_same_reads_inside_the_broker(user_broker, tmp_path):
    secret = tmp_path / "server-file"
    secret.write_text("key")  # the audit hook refuses before the OS is asked
    with pytest.raises(SandboxError, match="sandbox: opening .* is blocked"):
        run_circuit_code(ESCAPE + f"os.open({str(secret)!r}, os.O_RDONLY)\n" + BUILD)


@root_only
def test_limits_hold_behind_the_broker_and_it_keeps_answering(user_broker):
    with pytest.raises(SandboxError, match="MemoryError"):
        run_circuit_code("import numpy as np\nx = np.ones(200_000_000)\n" + BUILD)
    assert run_circuit_code(BUILD).num_qubits == 2
    assert user_broker.poll() is None


@root_only
def test_a_dead_broker_is_an_error_not_a_local_run(user_broker):
    assert run_circuit_code(BUILD).num_qubits == 2
    user_broker.kill()
    user_broker.wait()
    with pytest.raises(SandboxError, match="the sandbox broker"):
        run_circuit_code(BUILD)


@root_only
def test_the_broker_will_not_start_as_root(tmp_path):
    r = subprocess.run(
        [sys.executable, "-m", "qre_agent.sandbox_broker", str(tmp_path / "s.sock")],
        capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert r.returncode != 0 and "must not run as root" in r.stderr


# At most two builds at once, and a bound on what is parsed.


def test_the_broker_builds_two_at_once_and_refuses_a_third():
    assert sandbox_broker.MAX_BUSY == 2
    gate, started, calls = threading.Event(), threading.Semaphore(0), []

    def build(code, timeout):
        calls.append(code)
        started.release()
        gate.wait(10)
        return b"qpy"

    busy = threading.BoundedSemaphore(sandbox_broker.MAX_BUSY)
    clients = []
    for name in ("one", "two"):
        a, b = socket.socketpair()
        send(a, {"code": name})
        sandbox_broker.admit(b, build, busy)
        clients.append(a)
    assert started.acquire(timeout=10) and started.acquire(timeout=10)  # both are building

    a, b = socket.socketpair()
    send(a, {"code": "three"})
    sandbox_broker.admit(b, build, busy)
    assert receive(a, 1024) == {"ok": False, "error": "the sandbox broker is busy"}
    assert sorted(calls) == ["one", "two"]  # the third never reached a build

    gate.set()
    for client in clients:
        assert receive(client, 1024)["ok"]
    for _ in range(200):  # the place is given back just after the reply
        if busy.acquire(blocking=False):
            break
        time.sleep(0.05)
    else:
        pytest.fail("no place came free")


def test_a_circuit_file_over_the_limit_is_refused_before_it_is_read_or_parsed(monkeypatch):
    monkeypatch.setattr("qre_agent.tools.MAX_QPY_BYTES", 1000)
    monkeypatch.setattr("qre_agent.tools.qpy.load", lambda *a: pytest.fail("parsed"))
    many = (
        "from qiskit import QuantumCircuit\ncircuit = QuantumCircuit(1)\n" + "circuit.x(0)\n" * 100
    )
    with pytest.raises(
        SandboxError, match=r"the circuit file is [\d,]+ bytes, over the .* MB limit"
    ):
        run_circuit_code(many)


def test_a_real_circuit_over_1_mb_is_refused():
    code = "from qiskit import QuantumCircuit\ncircuit = QuantumCircuit(1)\n"
    code += "for _ in range(30_000):\n    circuit.x(0)\n"  # 43 bytes a gate: about 1.3 MB
    with pytest.raises(
        SandboxError, match=r"the circuit file is [\d,]+ bytes, over the 1 MB limit"
    ):
        run_circuit_code(code)
    assert run_circuit_code(code.replace("30_000", "20_000")).size() == 20_000  # 0.9 MB passes


def test_bytes_from_the_broker_over_the_limit_are_not_parsed(monkeypatch):
    monkeypatch.setenv(SOCKET_ENV, "/run/qre/sandbox.sock")
    monkeypatch.setattr(sandbox_broker, "ask", lambda *a: b"\0" * (2**20 + 1))
    monkeypatch.setattr("qre_agent.tools.qpy.load", lambda *a: pytest.fail("parsed"))
    with pytest.raises(SandboxError, match="over the 1 MB limit"):
        run_circuit_code(BUILD)


def test_a_broker_reply_over_the_limit_is_refused_by_size_before_decoding():
    def reply_with(size):
        """The broker's end sends a reply of `size` bytes from a thread: a socket pair's buffer is
        smaller than the reply, so sending first would block."""
        a, b = socket.socketpair()
        data = sandbox_broker.base64.b64encode(b"\0" * size).decode()
        threading.Thread(target=send, args=(b, {"ok": True, "qpy": data}), daemon=True).start()
        with a:
            return exchange(a, "x", 5)

    with pytest.raises(
        SandboxError, match="the circuit file is 1,048,5.. bytes, over the 1 MB limit"
    ):
        reply_with(2**20 + 1)
    assert len(reply_with(2**20)) == 2**20  # exactly the limit passes


def test_a_reply_frame_over_the_limit_is_refused_unread():
    a, b = socket.socketpair()
    b.sendall(struct.pack(">I", sandbox_broker.MAX_REPLY + 1))  # a header, nothing after it
    with pytest.raises(SandboxError, match="over the 1 MB limit"):
        exchange(a, "x", 5)


def test_a_client_that_sends_nothing_does_not_hold_a_place(monkeypatch):
    monkeypatch.setattr(sandbox_broker, "READ_S", 0.2)
    busy = threading.BoundedSemaphore(1)
    a, b = socket.socketpair()  # `a` never sends
    sandbox_broker.admit(b, lambda *x: pytest.fail("built"), busy)
    for _ in range(100):
        if busy.acquire(blocking=False):
            break
        time.sleep(0.05)
    else:
        pytest.fail("the silent client still holds the place")
    assert receive(a, 1024) == {"ok": False, "error": "bad request"}
