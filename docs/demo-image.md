# The demo image

Phase 4, PR 4. Files: `Dockerfile`, `docker/entrypoint.sh`, `.dockerignore`, `requirements-demo.txt`, `scripts/smoke_image.py`, and the `image` job in `.github/workflows/ci.yml`. It runs the API (`docs/demo-api.md`) and the sandbox broker (`docs/agent-design.md`, Sandbox) in one container.

```
docker build -t qre-demo .
docker run --rm -p 8080:8080 --tmpfs /sandbox-tmp:size=64m -e OPENROUTER_API_KEY=... qre-demo
```

## Stages

| stage | what | notes |
|---|---|---|
| `compiler` | `rust:1.99.0-slim-bookworm`; fetches IBM's compiler at `bed7e8a` (the commit is checked), copies in `ci/bicycle-Cargo.lock`, builds `bicycle_compiler` and `bicycle_numerics` with `--locked -F bicycle_compiler/rsgridsynth`, strips them | the Rust toolchain stays in this stage. Output laid out as `compiler.compiler_versions` reads it: `target/release/`, `Cargo.lock`, and a `COMMIT` file (there is no git checkout) |
| `tables` | runs `bicycle_compiler gross generate` and `two-gross generate` and records `tables.sha256` | each code in its own directory: the compiler checks its output directory by creating and deleting `dummy_file_check`, and two runs in one directory delete each other's (the first build failed on it) |
| `deps` | a venv from `requirements-demo.txt` | 37 pins, a subset of `requirements.txt` with the same versions (`pip-compile --constraint=requirements.txt --extra=demo`): no matplotlib, pytest or ruff |
| `final` | `python:3.11.17-slim-bookworm` plus the above, `src/`, `assumptions/`, `budgets.yaml` | no Rust, git, `.env` or key. The code runs from `/app/src`, so the repository root is `/app` and the tables sit in `/app/cache/` where the code looks |
| `test` | `final` plus pytest and `tests/` | only for CI (`--target test`) |

Base images are pinned by digest as well as tag (looked up 2026-10-11). The tables are not reproducible: the compiler breaks cost ties in `HashMap` order, so each build makes tables that differ a little (bicycle timesteps by about 1%). The tables of an image are fixed by its layer cache and their sha256s are in `/app/cache/tables.sha256`, in `/healthz` (`measurement_tables`) and in every result.

## Processes and users

`docker/entrypoint.sh` starts as root only to start two processes, then waits and exits if either stops (so the host restarts the container):

- the sandbox broker as `sandbox` (uid 10002), with `env -i` and `TMPDIR=/sandbox-tmp`, no keys in its environment;
- the server as `app` (uid 10001, home `/home/app`: QDK writes under `HOME`), after the broker's socket answers a ping.

Both go through `setpriv --clear-groups --no-new-privs`. The socket is `/run/qre/sandbox.sock` (directory owned by `sandbox`, socket mode 0666). The image sets `QRE_SANDBOX_SOCKET`, `QRE_SANDBOX_REQUIRED=1`, `QRE_COMPILER_DIR=/opt/bicycle/target/release` and `QDK_PYTHON_TELEMETRY=none` (by default the `qdk` package sends usage data to Microsoft's Application Insights in release builds). Run it with `OPENROUTER_API_KEY` from the host's secret store: nothing in the image holds a key.

**The broker's temp directory** is `/sandbox-tmp`, for a size-limited in-memory volume: `--tmpfs /sandbox-tmp:size=64m` with Docker, `--add-volume name=sandbox-tmp,type=in-memory,size-limit=64Mi` and a mount on Cloud Run. The entrypoint gives it to `sandbox` (mode 0700). If the host mounts it so that can't be done, it is made world-writable with the sticky bit and a warning is printed. When the volume is full a build stops (the smoke test fills it); the child often can't write its own traceback to the full volume, so the error is "the code exited with code 120 and no error output (the temp directory may be full)".

## CI (`image` job)

Runs when a file that goes into the image changed (see the `case` in the job), on `workflow_dispatch`, and for a new branch. Steps: build with `docker/build-push-action` and the GitHub Actions layer cache; fail above 1.2 GB; `scripts/smoke_image.py`; build the `test` target; run `tests/test_sandbox_broker.py` and `tests/test_sandbox_limits.py` inside the image, as root with `QRE_REQUIRE_ROOT_TESTS=1` and the image's `sandbox` user, and again as `app`. `scripts/smoke_image.py IMAGE` can be run on any machine with Docker.

The smoke test starts the image with `--network none`, `--memory 2g` and a dummy key, and checks: `/healthz` (versions, the compiler's commit, the Cargo.lock hash, the table hashes against `tables.sha256`); the server runs as `app` and the broker as `sandbox`, and `sandbox` can't read the server's `/proc/<pid>/environ`; a `build_circuit` through the broker runs as uid 10002 with only `PATH`, `LC_CTYPE` and the three thread variables in its environment; the full volume stops a write and builds still work; a 4-qubit QFT estimated on both architectures with the image's compiler and tables gives 140,015 surface and 1,226 two-gross physical qubits; no `cargo`, `rustc` or git, and no key variable in the image; a start without a key exits with "cannot start the demo: OPENROUTER_API_KEY is missing". No LLM call is made.

## Measured (GitHub Actions `ubuntu-24.04`, 2 vCPU, 2026-10-11)

| | |
|---|---|
| image size | **857 MB** uncompressed (limit 1,200 MB): venv 422 MB, tables 289 MB, the compiler 7 MB, the app 0.2 MB, the rest the Python base |
| cold build (no cache) | **5 min 4 s**: compiler 43 s (`cargo build`), tables 81 s (both codes in parallel), venv 25 s, the rest the base images and the final stage; exporting the layer cache took 136 s of that |
| rebuild after a code change, layers from the GitHub cache | **77 s** (importing the cached layers dominates: 20 s for the compiler layer, 10 s for the venv, 7 s for the tables) |
| memory at idle | **208 MB** (cgroup `memory.current`; peak since start 220 MB); the server has imported qiskit and QDK and hashed the tables |
| memory after a build through the broker | 208 MB, peak 361 MB |
| memory after one real estimate on both architectures | 208 MB, **peak 962 MB** (an estimate's child process and the bicycle compiler's table load; I did not separate the two) |
| start to ready | 3 s |

These are one run each, so no spread. The peak of 962 MB is one estimate at a time (the server runs them one at a time); two sandbox builds that each use their whole allowance (about 600 MB each) together with the server (about 210 MB) and an estimate (up to 960 MB) would pass 2 GiB, so the 2 GiB instance is tight in that worst case; the in-memory temp volume also counts against it. Nothing here measured that case. The plan estimated 0.7 to 0.75 GB for the image and 1.2 to 1.3 GB at peak.
