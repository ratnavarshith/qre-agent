"""Checks, inside the deployed image, that the sandbox's limits hold where it runs: Cloud Run has no
`exec`, so this runs as a one-off job of the same image (`qre-entrypoint selfcheck`: the entrypoint
starts the broker as for the server, then runs this as the server's user instead of the server).
Each check sends agent-style code through the broker, as build_circuit does. Prints one line per
check and exits 1 if any failed. No network and no key are needed. See docs/deploy.md."""

import os
import platform
import sys

from .tools import SandboxError, run_circuit_code

HEAD = "import qiskit.utils.parallel as p\nfrom qiskit import QuantumCircuit\nos = p.os\n"
BUILD = "circuit = QuantumCircuit(1)\n"
KEEP = {"PATH", "LC_CTYPE", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "RAYON_NUM_THREADS"}


def attempt(code, timeout=60):
    """(circuit, None) or (None, the error's text)."""
    try:
        return run_circuit_code(code, timeout=timeout), None
    except SandboxError as e:
        return None, str(e)


def last_line(text):
    lines = (text or "").strip().splitlines()
    return lines[-1] if lines else ""


def sandbox_uid():
    import pwd  # Linux only, like the image

    return str(pwd.getpwnam("sandbox").pw_uid)


def check_user():
    circuit, error = attempt(HEAD + "circuit = QuantumCircuit(1, name=str(os.getuid()))\n")
    want = sandbox_uid()
    got = circuit.name if circuit is not None else error  # a circuit with no gates is falsy
    me = os.getuid() if hasattr(os, "getuid") else None
    return circuit is not None and got == want, f"uid {got}, sandbox is {want}, this process {me}"


def check_environment():
    code = HEAD + "circuit = QuantumCircuit(1, name=','.join(sorted(os.environ)))\n"
    circuit, error = attempt(code)
    if circuit is None:
        return False, error
    names = set(circuit.name.split(","))
    return names <= KEEP, ",".join(sorted(names))


def check_memory():
    _, error = attempt("import numpy as np\nx = np.ones(200_000_000)\n" + HEAD + BUILD)  # 1.6 GB
    return error is not None and "MemoryError" in error, last_line(error) or "no error"


def check_cpu():
    _, error = attempt("while True:\n    pass\n" + HEAD + BUILD)
    return error is not None and "CPU time" in error, last_line(error) or "no error"


def check_file_size():
    write = "f = os.sys.modules['builtins'].open(os.getcwd() + '/big', 'wb')\nf.write(b'x' * 20_000_000)\nf.close()\n"
    _, error = attempt(HEAD + write + BUILD)  # 20 MB: over the 10 MB file limit, under the volume
    return error is not None and "File too large" in error, last_line(error) or "no error"


def check_volume():
    fill = (
        "for i in range(40):\n"
        "    f = os.sys.modules['builtins'].open(os.getcwd() + '/f' + str(i), 'wb')\n"
        "    f.write(b'x' * 5_000_000)\n    f.close()\n"
    )  # 200 MB in 5 MB files: under the file limit, over the 64 MiB volume
    _, error = attempt(HEAD + fill + BUILD)
    stopped = error is not None and ("No space left" in error or "may be full" in error)
    after, again = attempt(HEAD + BUILD)
    return (
        stopped and after is not None,
        f"{last_line(error) or 'no error'}; next build: {'ok' if after is not None else again}",
    )


CHECKS = [
    ("the code runs as the sandbox user", check_user),
    ("the code sees none of the server's environment", check_environment),
    ("a 1.6 GB allocation is refused (memory limit)", check_memory),
    ("a CPU hog is stopped at 10 s (CPU limit)", check_cpu),
    ("a 20 MB file is refused (file size limit)", check_file_size),
    ("the temp volume fills and stops the writes", check_volume),
]


def cgroup(name):
    try:
        with open(f"/sys/fs/cgroup/{name}", encoding="ascii") as f:
            return int(f.read()) // 2**20
    except (OSError, ValueError):
        return None


def main(checks=CHECKS):
    uid = os.getuid() if hasattr(os, "getuid") else None
    print(f"kernel {platform.release()}, python {platform.python_version()}, uid {uid}")
    failed = []
    for name, check in checks:
        try:
            ok, detail = check()
        except Exception as e:  # noqa: BLE001 (a check that crashes failed)
            ok, detail = False, f"{type(e).__name__}: {e}"
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}", flush=True)
        if not ok:
            failed.append(name)
    now, peak = cgroup("memory.current"), cgroup("memory.peak")
    if now is None and peak is None:  # Cloud Run doesn't expose them inside the container
        print("memory: not available (no cgroup memory files)")
    else:
        print(f"memory: now {now} MB, peak {peak} MB (cgroup)")
    if failed:
        print(f"selfcheck FAILED: {', '.join(failed)}")
        return 1
    print("selfcheck passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
