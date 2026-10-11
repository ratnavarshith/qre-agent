"""Runs agent-written Qiskit code for tools.build_circuit in its own process: the code comes on
stdin and must assign a QuantumCircuit to `circuit`, which is written as QPY to argv[1]; argv[2]
may be a JSON object that overrides LIMITS. Imports were checked before this runs; here, on Linux,
resource limits are set first, sockets are disabled and an audit hook limits files and processes.
A guardrail, not a security boundary. See docs/agent-design.md."""

import json
import os
import socket
import sys
import traceback

from qiskit import QuantumCircuit, qpy

# Resource limits (Linux), for the process and what it can start. Based on the largest real
# build_circuit calls in 449 past traces (33 qubits, 258 operations, 10 KB of code): 0.6 s of CPU
# (nearly all of it importing qiskit), 66 MB resident (measured on Windows), a 12 KB QPY file. See
# docs/agent-design.md.
LIMITS = {
    "cpu_s": 10,  # CPU seconds (the wall-clock timeout is the caller's)
    # RLIMIT_DATA, as this many MB more than the process maps now, after importing qiskit: that
    # import alone maps 1.1 to 1.3 GB of private writable address space (96 MB of it resident), so
    # an absolute limit would have to be set to the import, not to what a build needs.
    "memory_mb": 512,
    "fsize_mb": 10,  # the largest file the process can write
}
# Also "nproc": RLIMIT_NPROC counts every process and thread of the *user*, so it only means
# something for a user of its own: the sandbox broker passes it (sandbox_broker.NPROC), a process
# on a shared user doesn't (a desktop user can have thousands of threads already).
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
PROCESS_EVENTS = {
    "subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.startfile",
    "os.fork", "os.forkpty",
}  # fmt: skip
# Every ctypes.* event is blocked (dlopen, dlsym, string_at, ...): ctypes is a way around the hook.
# The agent has no reason to change the filesystem other than through open() in its own dir.
FILESYSTEM_EVENTS = {
    "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.truncate", "os.chmod", "os.chown",
    "os.link", "os.symlink", "os.utime", "shutil.rmtree",
}  # fmt: skip


def mapped_data():
    """Bytes of private writable address space the process has mapped now (VmData), which is
    what RLIMIT_DATA counts."""
    with open("/proc/self/status", encoding="ascii") as f:
        for line in f:
            if line.startswith("VmData:"):
                return int(line.split()[1]) * 1024
    raise RuntimeError("no VmData in /proc/self/status")


def apply_limits(limits):
    """Lowers the resource limits (soft and hard, so the code can't raise them again). A no-op
    off Linux. A limit above the current hard limit is left at that."""
    if not sys.platform.startswith("linux"):
        return
    import resource

    mb = 2**20
    wanted = [
        ("RLIMIT_CPU", limits["cpu_s"], 2),  # SIGXCPU at the soft limit, SIGKILL 2 s later
        ("RLIMIT_DATA", mapped_data() + limits["memory_mb"] * mb, 0),
        ("RLIMIT_FSIZE", limits["fsize_mb"] * mb, 0),
        ("RLIMIT_CORE", 0, 0),
    ]
    if "nproc" in limits:
        wanted.append(("RLIMIT_NPROC", limits["nproc"], 0))
    for name, soft, extra in wanted:
        which = getattr(resource, name)
        hard = resource.getrlimit(which)[1]
        hard = soft + extra if hard == resource.RLIM_INFINITY else min(soft + extra, hard)
        resource.setrlimit(which, (min(soft, hard), hard))


def _no_network(*args, **kwargs):
    raise PermissionError("network access is disabled in the sandbox")


def _real(path):
    return os.path.normcase(os.path.realpath(path))


def _inside(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives
        return False


def audit_hook(run_dir):
    """Allow open() in run_dir, and read-only open() in the Python install and site-packages."""
    run_dir = _real(run_dir)
    read_only = {_real(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}

    def hook(event, args):
        if event in PROCESS_EVENTS or event in FILESYSTEM_EVENTS or event.startswith("ctypes."):
            raise PermissionError(f"sandbox: {event} is blocked")
        if event != "open" or isinstance(args[0], int):  # an fd was already opened and checked
            return
        path, mode, flags = args
        path = _real(os.fsdecode(path))
        write = bool(flags & WRITE_FLAGS) or any(c in (mode or "") for c in "wax+")
        if _inside(path, run_dir) or not write and any(_inside(path, r) for r in read_only):
            return
        raise PermissionError(
            f"sandbox: opening {path} for {'writing' if write else 'reading'} is blocked"
        )

    return hook


def main():
    apply_limits(LIMITS | (json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}))
    out, code = sys.argv[1], sys.stdin.buffer.read().decode("utf-8")  # UTF-8, not the code page
    socket.socket.__init__ = _no_network
    socket.getaddrinfo = socket.create_connection = _no_network
    sys.dont_write_bytecode = True
    sys.addaudithook(audit_hook(os.path.dirname(out)))
    scope = {"__name__": "__agent__"}
    try:
        exec(compile(code, "<agent>", "exec"), scope)  # noqa: S102 (the point of this process)
        circuit = scope.get("circuit")
        if not isinstance(circuit, QuantumCircuit):
            raise TypeError(f"`circuit` must be a QuantumCircuit, got {type(circuit).__name__}")
        with open(out, "wb") as f:
            qpy.dump(circuit, f)
    except Exception:  # noqa: BLE001 (any failure goes back to the agent)
        sys.stderr.buffer.write(traceback.format_exc()[-2000:].encode("utf-8"))
        sys.exit(1)


if __name__ == "__main__":
    main()
