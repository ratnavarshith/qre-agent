# syntax=docker/dockerfile:1
# The public demo: the API (src/qre_agent/server.py) and the sandbox broker (sandbox_broker.py),
# started by docker/entrypoint.sh. See docs/demo-image.md.
#
#   docker build -t qre-demo .
#   docker run --rm -e OPENROUTER_API_KEY=... -p 8080:8080 --tmpfs /sandbox-tmp:size=64m qre-demo
#
# Stages: compiler (Rust, discarded) -> tables (the compiler's measurement tables, about a minute
# each) -> deps (the Python lock) -> final. `--target test` adds pytest and the tests.

# Base images by tag and digest (tags move; digests don't). Looked up 2026-10-11.
ARG RUST_IMAGE=rust:1.99.0-slim-bookworm@sha256:2c3a22f0a5533ea2dd5a16627bc841228151faa2d4de2644ac9987e4a2f1f2fa
ARG PYTHON_IMAGE=python:3.11.17-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89
ARG DEBIAN_IMAGE=debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
# IBM's bicycle compiler, at the commit the results were made with (as in .github/workflows/ci.yml).
ARG COMPILER_REPO=https://github.com/qiskit-community/bicycle-architecture-compiler.git
ARG COMPILER_COMMIT=bed7e8aba4cc0fb86311b7fa6dad6217c0fce7cb


FROM ${RUST_IMAGE} AS compiler
ARG COMPILER_REPO
ARG COMPILER_COMMIT
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init -q . \
 && git fetch -q --depth 1 "$COMPILER_REPO" "$COMPILER_COMMIT" \
 && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "$COMPILER_COMMIT"
# Upstream doesn't commit a Cargo.lock; ours pins every dependency.
COPY ci/bicycle-Cargo.lock Cargo.lock
RUN cargo build --release --locked -F bicycle_compiler/rsgridsynth -p bicycle_compiler -p bicycle_numerics \
 && mkdir -p /out/target/release \
 && cp target/release/bicycle_compiler target/release/bicycle_numerics /out/target/release/ \
 && strip /out/target/release/* \
 && cp Cargo.lock /out/Cargo.lock \
 && echo "$COMPILER_COMMIT" > /out/COMMIT
# /out is laid out as compiler.compiler_versions expects: <root>/target/release, <root>/Cargo.lock
# and, with no git checkout, <root>/COMMIT.


FROM ${DEBIAN_IMAGE} AS tables
COPY --from=compiler /out/ /opt/bicycle/
WORKDIR /cache
# The search breaks cost ties in HashMap order, so every generated table differs a little (bicycle
# timesteps by about 1%): the tables of this image are these, and their hashes are recorded.
RUN set -eu; pids=""; \
    for code in gross two-gross; do \
      /opt/bicycle/target/release/bicycle_compiler "$code" generate "table_$code" & pids="$pids $!"; \
    done; \
    for pid in $pids; do wait "$pid"; done; \
    sha256sum table_* > tables.sha256; \
    cat tables.sha256; ls -l


FROM ${PYTHON_IMAGE} AS deps
COPY requirements-demo.txt /tmp/requirements-demo.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --no-deps -r /tmp/requirements-demo.txt


FROM ${PYTHON_IMAGE} AS final
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="qre-agent demo" \
      org.opencontainers.image.revision="${GIT_SHA}"
# The server and the sandbox are different users, and neither is root. Their ids are fixed so a
# mounted volume can be given to one of them.
RUN useradd --system --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app \
 && useradd --system --uid 10002 --user-group --no-create-home --shell /usr/sbin/nologin sandbox
COPY --from=deps /opt/venv /opt/venv
COPY --from=compiler /out/ /opt/bicycle/
COPY --from=tables /cache/ /app/cache/
WORKDIR /app
# The code runs from /app/src, so the repository root is /app: assumptions/, budgets.yaml and
# cache/ (the tables) sit where the code looks for them.
COPY src/ src/
COPY assumptions/ assumptions/
COPY budgets.yaml ./
COPY docker/entrypoint.sh /usr/local/bin/qre-entrypoint
RUN chmod 0755 /usr/local/bin/qre-entrypoint \
 && python -m compileall -q /app/src \
 && mkdir -p /run/qre /sandbox-tmp
# No keys here: OPENROUTER_API_KEY comes from the host's secret store at run time.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QDK_PYTHON_TELEMETRY=none \
    QRE_COMPILER_DIR=/opt/bicycle/target/release \
    QRE_SANDBOX_SOCKET=/run/qre/sandbox.sock \
    QRE_SANDBOX_REQUIRED=1 \
    QRE_SANDBOX_TMP=/sandbox-tmp \
    PORT=8080
EXPOSE 8080
# The broker's TMPDIR is /sandbox-tmp: mount a size-limited in-memory volume there (docker:
# --tmpfs /sandbox-tmp:size=64m; Cloud Run: --add-volume type=in-memory,size-limit=64Mi).
# Starts as root only to start the two processes as their own users (see the script).
ENTRYPOINT ["/usr/local/bin/qre-entrypoint"]


# pytest and the tests, to run the sandbox tests as root inside the image.
FROM final AS test
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.txt
COPY pyproject.toml ./
COPY tests/ tests/
ENTRYPOINT []


FROM final
