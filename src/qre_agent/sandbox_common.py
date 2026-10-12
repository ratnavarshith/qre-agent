"""What the sandbox's server side, the broker and the child share, without importing qiskit: the
error, the socket's environment variable and the limits on a build. See docs/agent-design.md."""

SOCKET_ENV = "QRE_SANDBOX_SOCKET"  # the sandbox broker's socket, when build_circuit runs behind one
TIMEOUT_S = 60
MAX_QPY_BYTES = 2**20  # what a build may hand back; checked before qiskit parses it


class SandboxError(Exception):
    pass
