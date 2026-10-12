"""Smoke test of the demo image: starts it with no network and a dummy key, and checks it the way
the host will use it. No LLM call is made (there is no network, and the key is not real).

    python scripts/smoke_image.py IMAGE [--commit SHA] [--lock ci/bicycle-Cargo.lock]

Checks: GET / loads the page (with its policy and no outside URL) and /limits fails closed with no
network; HEAD / and HEAD /health answer 200; /health (versions, the compiler's commit and Cargo.lock hash, the measurement tables'
hashes against the ones recorded in the image); the server and the broker run as different
non-root users; a build_circuit call goes through the broker and runs as the sandbox user, with
none of the server's environment; the sandbox user can't read the server's /proc/<pid>/environ;
the size-limited volume under the broker's temp directory fills up and stops the write; a real
benchmark is estimated on both architectures with the image's compiler and tables; the image has no
Rust toolchain and no key; a start without a key fails with a clear message. Prints the container's
memory at idle and after each step. Exit status 1 if anything failed."""

import argparse
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

KEY = "sk-or-v1-" + "0" * 64  # not a real key
APP_UID, SANDBOX_UID = "10001", "10002"
SURFACE_QUBITS, BICYCLE_QUBITS = 140015, 1226  # 4-qubit QFT at p = 1e-3 (results/comparison)

HEALTH = "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/health', timeout=5).read().decode())"
PROCESSES = """
import json, os
found = {}
for pid in os.listdir('/proc'):
    if not pid.isdigit() or int(pid) == os.getpid():
        continue
    try:
        cmd = open(f'/proc/{pid}/cmdline', 'rb').read().replace(b'\\0', b' ').decode()
        uid = [l.split()[1] for l in open(f'/proc/{pid}/status') if l.startswith('Uid:')][0]
    except OSError:
        continue
    for name in ('server', 'sandbox_broker'):
        if '-m qre_agent.' + name in cmd:
            found[name] = {'pid': pid, 'uid': uid}
print(json.dumps(found))
"""
BROKER_BUILD = """
import os
from qre_agent.tools import run_circuit_code
code = (
    "import qiskit.utils.parallel as p\\nfrom qiskit import QuantumCircuit\\nos = p.os\\n"
    "circuit = QuantumCircuit(1, name=str(os.getuid()) + '|' + ','.join(sorted(os.environ)))\\n"
)
uid, names = run_circuit_code(code).name.split('|')
print(uid); print(names); print(os.getuid())
"""
DISK_FULL = """
from qre_agent.tools import SandboxError, run_circuit_code
head = "import qiskit.utils.parallel as p\\nfrom qiskit import QuantumCircuit\\nos = p.os\\n"
fill = (
    "for i in range(40):\\n"
    "    f = os.sys.modules['builtins'].open(os.getcwd() + '/f' + str(i), 'wb')\\n"
    "    f.write(b'x' * 5_000_000)\\n    f.close()\\n"
)
try:
    run_circuit_code(head + fill + "circuit = QuantumCircuit(1)\\n")
    print('no error')
except SandboxError as e:
    print(str(e).strip().splitlines()[-1] if str(e).strip() else 'empty error')
print(run_circuit_code(head + "circuit = QuantumCircuit(2)\\n").num_qubits)  # and it still works
"""
ESTIMATE = """
import json
from qre_agent.tools import Toolbox
t = Toolbox(timeout=90)
c = json.loads(t.call('build_benchmark', {'family': 'qft', 'n': 4}))
s = json.loads(t.call('estimate_surface', {'circuit_id': c['circuit_id']}))
b = json.loads(t.call('estimate_bicycle', {'circuit_id': c['circuit_id'], 'code': 'two-gross'}))
print(json.dumps({'surface': s.get('physical_qubits', s), 'bicycle': b.get('physical_qubits', b)}))
"""
PAGE = """
import json, re, urllib.request as u
r = u.urlopen('http://127.0.0.1:8080/', timeout=5)
body = r.read().decode()
limits = json.loads(u.urlopen('http://127.0.0.1:8080/limits', timeout=30).read().decode())
heads = {p: u.urlopen(u.Request('http://127.0.0.1:8080' + p, method='HEAD'), timeout=5).status for p in ('/', '/health')}
print(json.dumps({
    'status': r.status, 'type': r.headers['content-type'],
    'csp': r.headers.get('content-security-policy', ''), 'title': '<title>' in body,
    'calls': '/estimate' in body and '/limits' in body,
    'urls': sorted(set(re.findall(r'https?://[A-Za-z0-9./_-]+', body))), 'limits': limits, 'heads': heads,
}))
"""
READ_ENVIRON = "import sys; open('/proc/' + sys.argv[1] + '/environ').read()"


def docker(*args, timeout=600):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


class Smoke:
    def __init__(self, image, commit, lock):
        self.image, self.commit, self.lock = image, commit, lock
        self.name = f"qre-smoke-{uuid.uuid4().hex[:8]}"
        self.failures, self.memory = [], []

    def check(self, name, ok, detail=""):
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""), flush=True)
        if not ok:
            self.failures.append(name)

    def run_python(self, code, *args, user="app"):
        r = docker("exec", "--user", user, self.name, "python", "-c", code, *args)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def cgroup(self, name):
        r = docker("exec", self.name, "cat", f"/sys/fs/cgroup/{name}")
        return int(r.stdout) if r.returncode == 0 and r.stdout.strip().isdigit() else None

    def note_memory(self, when):
        current, peak = self.cgroup("memory.current"), self.cgroup("memory.peak")
        self.memory.append((when, current, peak))

    def start(self):
        r = docker(
            "run", "-d", "--name", self.name, "--network", "none", "--memory", "2g",
            "--memory-swap", "2g", "--tmpfs", "/sandbox-tmp:size=64m",
            "-e", f"OPENROUTER_API_KEY={KEY}", self.image,
        )  # fmt: skip
        if r.returncode:
            sys.exit(f"docker run failed: {r.stderr}")
        start = time.monotonic()
        while time.monotonic() - start < 240:
            if docker("inspect", "-f", "{{.State.Running}}", self.name).stdout.strip() != "true":
                print(docker("logs", self.name).stdout + docker("logs", self.name).stderr)
                sys.exit("the container exited before it was ready")
            code, out, _ = self.run_python(HEALTH)
            if code == 0:
                self.ready_s = time.monotonic() - start
                return json.loads(out)
            time.sleep(1)
        print(docker("logs", self.name).stderr)
        sys.exit("the server was not ready in 240 s")

    def stop(self):
        docker("rm", "-f", self.name)

    def test_health(self, health):
        v = health.get("versions", {})
        self.check(
            "/health is ok and accepting runs", health.get("ok") and health.get("accepting_runs")
        )
        self.check(
            "versions: qdk, qiskit, compiler",
            all(k in v for k in ("qdk", "qiskit", "bicycle_compiler")),
            json.dumps(v),
        )
        self.check(
            "the compiler's commit is the pinned one",
            v.get("bicycle_compiler_commit") == self.commit,
            str(v.get("bicycle_compiler_commit")),
        )
        lock = hashlib.sha256(Path(self.lock).read_bytes()).hexdigest()
        self.check("the compiler's Cargo.lock is ours", v.get("cargo_lock_sha256") == lock)
        recorded = docker("exec", self.name, "cat", "/app/cache/tables.sha256").stdout.split()
        recorded = dict(
            zip([n.removeprefix("table_") for n in recorded[1::2]], recorded[0::2], strict=True)
        )
        tables = health.get("measurement_tables", {})
        self.check(
            "the measurement tables' hashes are the recorded ones",
            tables == recorded and len(tables) == 2,
            json.dumps(tables),
        )

    def test_page(self):
        code, out, err = self.run_python(PAGE)
        got = json.loads(out) if code == 0 and out else {}
        self.check(
            "GET / serves the page",
            got.get("status") == 200
            and got.get("type", "").startswith("text/html")
            and got.get("title")
            and got.get("calls"),
            (err or out)[-300:],
        )
        self.check(
            "the page's policy forbids everything but itself",
            "default-src 'none'" in got.get("csp", "") and "unsafe" not in got.get("csp", ""),
            got.get("csp", ""),
        )
        self.check(
            "the page names no URL but the repository",
            got.get("urls") == ["https://github.com/ratnavarshith/qre-agent"],
            str(got.get("urls")),
        )
        self.check(
            "HEAD / and HEAD /health answer 200",
            got.get("heads") == {"/": 200, "/health": 200},
            str(got.get("heads")),
        )
        # no network: the demo can't read its key's budget, so it must say it isn't accepting runs
        limits = got.get("limits", {})
        self.check(
            "with the budget unreadable, /limits says not accepting (budget_unknown)",
            limits.get("accepting_runs") is False and limits.get("reason") == "budget_unknown",
            str(limits),
        )

    def test_users(self):
        code, out, err = self.run_python(PROCESSES)
        found = json.loads(out) if code == 0 else {}
        self.check(
            "the server runs as app", found.get("server", {}).get("uid") == APP_UID, str(found)
        )
        self.check(
            "the broker runs as sandbox",
            found.get("sandbox_broker", {}).get("uid") == SANDBOX_UID,
            str(found),
        )
        if "server" in found:
            code, _, err = self.run_python(READ_ENVIRON, found["server"]["pid"], user="sandbox")
            self.check(
                "the sandbox user can't read the server's environ",
                code != 0 and "PermissionError" in err,
                err.splitlines()[-1] if err else "it could",
            )

    def test_broker_build(self):
        code, out, err = self.run_python(BROKER_BUILD)
        lines = out.splitlines()
        self.check(
            "build_circuit through the broker ran as the sandbox user",
            code == 0 and lines[:1] == [SANDBOX_UID],
            err[-300:] or out,
        )
        if code == 0:
            names = lines[1].split(",")
            self.check(
                "the child saw none of the server's environment",
                not any(
                    k in n for n in names for k in ("KEY", "TOKEN", "SECRET", "QRE_", "OPENROUTER")
                ),
                lines[1],
            )
            self.check("the calling process is the app user", lines[2] == APP_UID, lines[2])
        self.note_memory("after a build through the broker")

    def test_volume(self):
        code, out, err = self.run_python(DISK_FULL)
        lines = out.splitlines()
        # The write stops with "No space left on device"; the child usually can't even write its
        # traceback to the full volume, so the error may be the broker's own message.
        stopped = bool(lines) and ("No space left" in lines[0] or "may be full" in lines[0])
        self.check(
            "the size-limited volume stops a runaway write, and builds still work",
            code == 0 and stopped and lines[-1:] == ["2"],
            (err or out)[-300:],
        )

    def test_estimate(self):
        start = time.monotonic()
        code, out, err = self.run_python(ESTIMATE)
        took = time.monotonic() - start
        got = json.loads(out) if code == 0 and out else {}
        self.check(
            "a real estimate: surface qubits",
            got.get("surface") == SURFACE_QUBITS,
            f"{got} in {took:.1f} s {err[-200:]}",
        )
        self.check(
            "a real estimate: two-gross qubits", got.get("bicycle") == BICYCLE_QUBITS, str(got)
        )
        self.note_memory("after a real estimate on both architectures")

    def test_image(self):
        r = docker(
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            self.image,
            "-c",
            "command -v cargo rustc git; id -u app; id -u sandbox; env",
        )
        self.check(
            "no Rust toolchain or git in the image",
            "cargo" not in r.stdout and "rustc" not in r.stdout and "/git" not in r.stdout,
        )
        self.check(
            "no key in the image's environment",
            "OPENROUTER_API_KEY" not in r.stdout
            and "ANTHROPIC_API_KEY" not in r.stdout
            and "sk-or" not in r.stdout,
        )
        self.check("the image sets QRE_SANDBOX_REQUIRED=1", "QRE_SANDBOX_REQUIRED=1" in r.stdout)
        r = docker("run", "--rm", "--network", "none", self.image)
        text = r.stdout + r.stderr
        self.check(
            "a start without a key fails and says why",
            r.returncode != 0 and "OPENROUTER_API_KEY" in text and "cannot start the demo" in text,
            text[-200:],
        )

    def report_memory(self):
        def mb(x):
            return "n/a" if x is None else f"{x / 2**20:.0f} MB"

        print("\ncontainer memory (cgroup, 2 GiB limit):")
        for when, current, peak in self.memory:
            print(f"  {when:<48} now {mb(current):>8}, peak so far {mb(peak):>8}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image")
    parser.add_argument("--commit", default="bed7e8aba4cc0fb86311b7fa6dad6217c0fce7cb")
    parser.add_argument("--lock", default="ci/bicycle-Cargo.lock")
    args = parser.parse_args()
    smoke = Smoke(args.image, args.commit, args.lock)
    try:
        health = smoke.start()
        print(f"ready after {smoke.ready_s:.0f} s")
        time.sleep(3)
        smoke.note_memory("idle, ready")
        smoke.test_health(health)
        smoke.test_page()
        smoke.test_users()
        smoke.test_broker_build()
        smoke.test_volume()
        smoke.test_estimate()
        smoke.test_image()
    finally:
        print(docker("logs", smoke.name).stderr[-1500:])
        smoke.report_memory()
        smoke.stop()
    if smoke.failures:
        sys.exit(f"FAILED: {', '.join(smoke.failures)}")
    print("all checks passed")


if __name__ == "__main__":
    main()
