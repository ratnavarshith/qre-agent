#!/bin/bash
# Container entrypoint. Runs as root only to start two processes as users of their own, then
# waits: the sandbox broker as `sandbox` (an empty environment: no keys) and the demo server as
# `app`. If either stops, the other is stopped and the container exits, so the host restarts it.
# See docs/demo-image.md.
set -eu

SOCKET="${QRE_SANDBOX_SOCKET:-/run/qre/sandbox.sock}"
SANDBOX_TMP="${QRE_SANDBOX_TMP:-/sandbox-tmp}"
as_sandbox=(setpriv --reuid=sandbox --regid=sandbox --clear-groups --no-new-privs)
as_app=(setpriv --reuid=app --regid=app --clear-groups --no-new-privs)

mkdir -p "$(dirname "$SOCKET")" "$SANDBOX_TMP"
chmod 0755 "$(dirname "$SOCKET")"
chown sandbox:sandbox "$(dirname "$SOCKET")"
# The broker's temp directory belongs to the sandbox user alone. A volume that can't be given to it
# (some hosts mount as root) is made writable by everyone instead: the sticky bit keeps the users
# from each other's files, but it is then no longer private, so say so.
if chown sandbox:sandbox "$SANDBOX_TMP" && chmod 0700 "$SANDBOX_TMP"; then
  :
else
  chmod 1777 "$SANDBOX_TMP"
  echo "entrypoint: WARNING $SANDBOX_TMP could not be given to the sandbox user; it is world-writable" >&2
fi

# The broker's environment is only what is listed here.
"${as_sandbox[@]}" env -i PATH="$PATH" HOME=/nonexistent TMPDIR="$SANDBOX_TMP" \
  PYTHONPATH="$PYTHONPATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 QDK_PYTHON_TELEMETRY=none \
  python -m qre_agent.sandbox_broker "$SOCKET" &
broker=$!

for _ in $(seq 1 150); do  # up to 15 s for the socket
  [ -S "$SOCKET" ] && break
  kill -0 "$broker" 2>/dev/null || { echo "entrypoint: the sandbox broker exited" >&2; exit 1; }
  sleep 0.1
done
[ -S "$SOCKET" ] || { echo "entrypoint: no sandbox socket at $SOCKET" >&2; kill "$broker"; exit 1; }

# `qre-entrypoint selfcheck` runs the sandbox's self-check (qre_agent/selfcheck.py) instead of the
# server, with the broker started exactly as it is for the server.
program=qre_agent.server
[ "${1:-}" = selfcheck ] && program=qre_agent.selfcheck
HOME=/home/app "${as_app[@]}" python -m "$program" &
server=$!

trap 'kill -TERM "$server" "$broker" 2>/dev/null || true' TERM INT
status=0
wait -n "$server" "$broker" || status=$?
kill -TERM "$server" "$broker" 2>/dev/null || true
wait 2>/dev/null || true
exit "$status"
