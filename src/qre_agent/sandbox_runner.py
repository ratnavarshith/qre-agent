"""Runs agent-written Qiskit code for tools.build_circuit in its own process: the code comes on
stdin and must assign a QuantumCircuit to `circuit`, which is written as QPY to argv[1].
Imports were checked before this runs; here sockets are disabled and an audit hook limits
files and processes. A guardrail, not a security boundary. See docs/agent-design.md."""

import os
import socket
import sys
import traceback

from qiskit import QuantumCircuit, qpy

WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
PROCESS_EVENTS = {
    "subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.startfile",
    "os.fork", "os.forkpty",
}  # fmt: skip
# The agent has no reason to change the filesystem other than through open() in its own dir.
FILESYSTEM_EVENTS = {
    "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.truncate", "os.chmod", "os.chown",
    "os.link", "os.symlink", "os.utime", "shutil.rmtree",
}  # fmt: skip


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
        if event in PROCESS_EVENTS or event in FILESYSTEM_EVENTS:
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
    out, code = sys.argv[1], sys.stdin.read()
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
        sys.stderr.write(traceback.format_exc()[-2000:])
        sys.exit(1)


if __name__ == "__main__":
    main()
