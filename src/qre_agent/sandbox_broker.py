"""The sandbox broker: a small server that runs as an unprivileged user of its own and builds
circuits for the demo server, which runs as another user. The server sends the agent's code over a
Unix socket; the broker runs it as `tools.build_qpy` does (a child with the minimal environment,
resource limits, sockets disabled and the audit hook) and sends back the circuit as QPY. So the
code runs as a user that can't read the server's environment, files or memory, and nothing is
shared between the two but the socket. Start it (as that user, with an empty environment):

    python -m qre_agent.sandbox_broker /run/qre/sandbox.sock

and give the server QRE_SANDBOX_SOCKET=/run/qre/sandbox.sock. See docs/agent-design.md."""

import base64
import json
import logging
import os
import socket
import struct
import sys
import threading

from .tools import MAX_QPY_BYTES, SOCKET_ENV, TIMEOUT_S, SandboxError, build_qpy, check_size

log = logging.getLogger("qre_agent.sandbox")
MAX_REQUEST = 256 * 1024  # bytes: the code is at most a few KB
MAX_REPLY = 2 * MAX_QPY_BYTES  # bytes: a QPY file of at most MAX_QPY_BYTES, as base64 in JSON
MAX_BUSY = 2  # builds at once, as many as the server's run slots: 2 x 512 MB at worst
READ_S = 10  # a client has this long to send its request: a silent one must not hold a place
NPROC = 32  # processes and threads of the sandbox user: 2 builds at one or two threads each


def send(conn, message):
    data = json.dumps(message).encode()
    conn.sendall(struct.pack(">I", len(data)) + data)


def read_exactly(conn, n):
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise EOFError("connection closed")
        data += chunk
    return data


class TooLarge(ValueError):
    pass


def receive(conn, limit):
    """One length-prefixed JSON message; ValueError if it is not JSON, TooLarge if it is over
    `limit` bytes (refused before it is read)."""
    (n,) = struct.unpack(">I", read_exactly(conn, 4))
    if n > limit:
        raise TooLarge(f"message of {n} bytes is over the {limit} limit")
    return json.loads(read_exactly(conn, n))


def handle(conn, build=build_qpy):
    """Answers one request on `conn` and closes it. `build(code, timeout) -> bytes`."""
    try:
        conn.settimeout(READ_S)
        request = receive(conn, MAX_REQUEST)
        if request.get("ping"):
            send(conn, {"ok": True})
            return
        timeout = min(float(request.get("timeout", TIMEOUT_S)), TIMEOUT_S)  # the broker's cap
        if not isinstance(code := request["code"], str):
            raise TypeError("code is not text")
        try:
            data = build(code, timeout)
        except SandboxError as e:
            send(conn, {"ok": False, "error": str(e)})
        else:
            send(conn, {"ok": True, "qpy": base64.b64encode(data).decode()})
    except (EOFError, ValueError, KeyError, TypeError, AttributeError, OSError) as e:
        log.warning("bad request: %s", type(e).__name__)
        try:
            send(conn, {"ok": False, "error": "bad request"})
        except OSError:
            pass
    finally:
        conn.close()


def exchange(conn, code, timeout):
    """The QPY bytes for `code`, over a connected socket; SandboxError if the broker says no, or
    can't be reached."""
    try:
        send(conn, {"code": code, "timeout": timeout})
        reply = receive(conn, MAX_REPLY)
    except TooLarge:
        raise SandboxError(
            f"the circuit file is over the {MAX_QPY_BYTES / 2**20:g} MB limit"
        ) from None
    except (OSError, EOFError, ValueError) as e:
        raise SandboxError(f"the sandbox broker failed ({type(e).__name__})") from None
    if not reply.get("ok"):
        raise SandboxError(str(reply.get("error", "the sandbox broker failed")))
    text = reply["qpy"]
    check_size(len(text) // 4 * 3 - text.count("=", -2))  # the decoded size, before decoding
    return base64.b64decode(reply["qpy"])


def connect(path, timeout):
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        conn.connect(path)
        return conn
    except (OSError, AttributeError) as e:  # AttributeError: no AF_UNIX (Windows)
        raise SandboxError(f"the sandbox broker is unavailable ({type(e).__name__})") from None


def ask(path, code, timeout=TIMEOUT_S):
    """The QPY bytes for `code`, built by the broker at `path`. No fallback to building it here."""
    with connect(path, timeout + 10) as conn:  # the build's own timeout comes first
        return exchange(conn, code, timeout)


def check(path):
    """RuntimeError, at startup, if the broker at `path` doesn't answer a ping."""
    try:
        with connect(path, 5) as conn:
            send(conn, {"ping": True})
            if not receive(conn, 1024).get("ok"):
                raise ValueError("bad reply")
    except (SandboxError, OSError, EOFError, ValueError) as e:
        raise RuntimeError(f"the sandbox broker at {path} does not answer ({e})") from None


def describe(environ=os.environ):
    """(mode, a log line): how build_circuit will run in this process."""
    if path := environ.get(SOCKET_ENV):
        return "broker", (
            f"sandbox: build_circuit runs behind the broker at {path}, as a separate user, "
            "with resource limits"
        )
    linux = sys.platform.startswith("linux")
    if linux and os.geteuid() == 0:
        return "root", (
            f"sandbox: WARNING no broker ({SOCKET_ENV} is unset) and running as root: "
            "build_circuit's child runs as root, with resource limits (not enforced for root)"
        )
    limits = "with resource limits" if linux else "without resource limits (Linux only)"
    return "local", (
        f"sandbox: no broker ({SOCKET_ENV} is unset): build_circuit's child runs as the same "
        f"user as this process, {limits}"
    )


def build_with_nproc(code, timeout):
    return build_qpy(code, timeout, limits={"nproc": NPROC})


def admit(conn, build, busy):
    """Starts a thread to answer `conn`, or, when `busy` (a semaphore of MAX_BUSY) has no place,
    says so and closes it. A refused request never reaches a build."""
    if not busy.acquire(blocking=False):
        try:
            conn.settimeout(READ_S)
            receive(conn, MAX_REQUEST)  # read it first: closing on unread data resets the peer
            send(conn, {"ok": False, "error": "the sandbox broker is busy"})
        except (OSError, EOFError, ValueError):
            pass
        conn.close()
        return

    def work():
        try:
            handle(conn, build)
        finally:
            busy.release()

    threading.Thread(target=work, daemon=True).start()


def serve(path, build=build_with_nproc):
    """Listens on the Unix socket `path` (any local user may connect) until killed. Refuses to
    run as root: the point is a user with nothing to lose."""
    if os.geteuid() == 0:
        raise SystemExit("the sandbox broker must not run as root: start it as the sandbox user")
    if os.path.exists(path):
        os.unlink(path)  # a stale socket from an earlier run
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, 0o666)  # the server runs as another user and has to connect
    server.listen(8)
    busy = threading.BoundedSemaphore(MAX_BUSY)
    log.info("sandbox broker listening on %s as uid %d", path, os.geteuid())
    while True:
        admit(server.accept()[0], build, busy)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if len(sys.argv) != 2 or not hasattr(socket, "AF_UNIX") or not hasattr(os, "geteuid"):
        raise SystemExit("usage (Linux): python -m qre_agent.sandbox_broker SOCKET_PATH")
    serve(sys.argv[1])


if __name__ == "__main__":
    main()
