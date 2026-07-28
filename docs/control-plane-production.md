# Control-plane production cutover

This runbook moves the macOS control-plane backend from the pickle-backed development
runtime to the PostgreSQL/Redis-backed Agent Server. The live service is the system
LaunchDaemon `com.mobilyze.open-swe-control-plane.backend`, running as `_openswectl`
through `/Library/Application Support/MobilyzeOpenSWEControlPlane/run`. The dashboard
LaunchDaemon is not changed.

The shipped `langgraph.json` deletes threads, runs, and checkpoints after 24 hours. Agent
Server 0.11.1 does not execute that TTL sweep in the in-memory runtime, so the production
backend must use `langgraph up`. The `make production` target defaults to port 2024 for
general use; this host explicitly sets `PRODUCTION_PORT=2029`.

## Host constants and preconditions

Run these commands from one shell as the operator account. Passwordless `sudo -n` must be
available. No command in this runbook starts a service on port 2024.

```bash
set -euo pipefail
BASE=/var/db/mobilyze-open-swe-control-plane
SERVICE_HOME="$BASE/home"
CURRENT="$BASE/current"
STATE_DIR="$BASE/.langgraph_api"
OPS_DIR="/Library/Application Support/MobilyzeOpenSWEControlPlane"
WRAPPER="$OPS_DIR/run"
ENV_FILE="$OPS_DIR/env"
BACKEND_LABEL=com.mobilyze.open-swe-control-plane.backend
DASHBOARD_LABEL=com.mobilyze.open-swe-control-plane.dashboard
BACKEND_PLIST="/Library/LaunchDaemons/$BACKEND_LABEL.plist"
DASHBOARD_PLIST="/Library/LaunchDaemons/$DASHBOARD_LABEL.plist"
LIVE_PORT=2029
STAGE_PORT=2030
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
sudo -n true
sudo -n test -f "$WRAPPER"
sudo -n test -f "$ENV_FILE"
sudo -n test -f "$BACKEND_PLIST"
sudo -n test -f "$DASHBOARD_PLIST"
test -f "$CURRENT/Makefile"
test -f "$CURRENT/langgraph.json"
test -x "$CURRENT/.venv/bin/langgraph"
test "$(sudo -n plutil -extract UserName raw -o - "$BACKEND_PLIST")" = _openswectl
test "$(sudo -n plutil -extract UserName raw -o - "$DASHBOARD_PLIST")" = _openswectl
test "$(sudo -n plutil -extract ProgramArguments.0 raw -o - "$BACKEND_PLIST")" = "$WRAPPER"
test "$(sudo -n plutil -extract ProgramArguments.1 raw -o - "$BACKEND_PLIST")" = backend
test "$(sudo -n stat -f "%Su:%Sg:%Lp" "$ENV_FILE")" = root:_openswectl:640
sudo -n launchctl print "system/$BACKEND_LABEL" >/dev/null
sudo -n launchctl print "system/$DASHBOARD_LABEL" >/dev/null
curl --fail --silent --show-error "http://127.0.0.1:$LIVE_PORT/ok"
if lsof -nP -iTCP:2024 -sTCP:LISTEN | grep .; then
  echo "refusing cutover: port 2024 is unexpectedly in use" >&2
  exit 1
fi
if lsof -nP -iTCP:"$STAGE_PORT" -sTCP:LISTEN | grep .; then
  echo "refusing cutover: staging port $STAGE_PORT is already in use" >&2
  exit 1
fi
make -C "$CURRENT" production-check
sudo -n du -sh "$STATE_DIR"
```

The deployment environment is the root-owned `ENV_FILE` (`root:_openswectl`, mode 0640),
not a repository-adjacent `.env`. The production wrapper sources that file, exports its
values, and materializes `CURRENT/.env` as an `_openswectl`-owned mode-0600 copy because
`langgraph.json` declares `env: ".env"`. The copy is removed after staging or rollback; it
remains while the live production stack needs it. No secret is written to a world-readable
path.

Install the Docker clients, but do not start Colima from the operator account. The wrapper
starts a dedicated VM as `_openswectl` with 4 CPUs, 12 GiB RAM, and 100 GiB disk.

```bash
command -v brew >/dev/null
brew list colima >/dev/null
brew list lima >/dev/null
if ! command -v docker >/dev/null || ! command -v docker-compose >/dev/null; then
  brew install docker docker-compose
fi
command -v colima >/dev/null
command -v limactl >/dev/null
command -v docker >/dev/null
command -v docker-compose >/dev/null
if sudo -n -u _openswectl env HOME="$SERVICE_HOME" /opt/homebrew/bin/colima status >/dev/null 2>&1; then
  echo "refusing cutover: the dedicated _openswectl Colima VM is already running" >&2
  exit 1
fi
```

## Back up and add the production wrapper role

Create timestamped backups before changing the wrapper or plist. The backup-record file
makes rollback independent of variables from this shell.

```bash
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WRAPPER_BACKUP="$WRAPPER.bak.$STAMP"
PLIST_BACKUP="$BACKEND_PLIST.bak.$STAMP"
BACKUP_RECORD="$BASE/cutover-backup.$STAMP"
sudo -n cp -p "$WRAPPER" "$WRAPPER_BACKUP"
sudo -n cp -p "$BACKEND_PLIST" "$PLIST_BACKUP"
printf '%s\n%s\n' "$WRAPPER_BACKUP" "$PLIST_BACKUP" | sudo -n tee "$BACKUP_RECORD" >/dev/null
sudo -n chmod 600 "$BACKUP_RECORD"
```

Prepend an idempotent `production` role. It sources the existing deployment environment,
starts Colima under the wrapper-exported service home, creates the secure `.env`, and
executes the compose-attached production target. The optional second argument is used for
the staged port; the LaunchDaemon uses the live default 2029.

```bash
sudo -n /usr/bin/python3 - "$WRAPPER" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
marker = "OPEN_SWE_PRODUCTION_ROLE"
if marker in text:
    raise SystemExit("production role already exists; inspect the wrapper before continuing")
lines = text.splitlines(keepends=True)
if not lines or not lines[0].startswith("#!"):
    raise SystemExit("wrapper has no shebang")
block = r'''
# OPEN_SWE_PRODUCTION_ROLE
if [ "${1:-}" = "production" ]; then
  ENV_FILE="/Library/Application Support/MobilyzeOpenSWEControlPlane/env"
  export HOME=/var/db/mobilyze-open-swe-control-plane/home
  export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
  set -a
  . "$ENV_FILE"
  set +a
  cd /var/db/mobilyze-open-swe-control-plane
  PORT="${2:-2029}"
  if ! /opt/homebrew/bin/colima status >/dev/null 2>&1; then
    /opt/homebrew/bin/colima start --runtime docker --cpu 4 --memory 12 --disk 100
  fi
  umask 077
  cp "$ENV_FILE" current/.env
  chmod 600 current/.env
  exec /usr/bin/make -C current production PRODUCTION_PORT="$PORT"
fi
'''
path.write_text(lines[0] + block + "".join(lines[1:]))
PY
sudo -n chown root:wheel "$WRAPPER"
sudo -n chmod 755 "$WRAPPER"
```

## Stage the production stack on port 2030

Staging builds the image, starts PostgreSQL and Redis, runs database migrations, and tests
the existing license before the live backend is touched. `langgraph up` receives the
wrapper-sourced environment through the secure materialized `.env`.

```bash
STAGE_LOG="$(mktemp /tmp/open-swe-production-stage.XXXXXX)"
chmod 600 "$STAGE_LOG"
sudo -n -u _openswectl env HOME="$SERVICE_HOME" "$WRAPPER" production "$STAGE_PORT" >"$STAGE_LOG" 2>&1 &
STAGE_PID=$!
STAGE_HEALTHY=0
for attempt in $(seq 1 120); do
  if curl --fail --silent "http://127.0.0.1:$STAGE_PORT/ok" >/dev/null; then
    STAGE_HEALTHY=1
    break
  fi
  if ! kill -0 "$STAGE_PID" 2>/dev/null; then break; fi
  sleep 5
done
if test "$STAGE_HEALTHY" -ne 1; then
  echo "staged Agent Server failed; exact server output follows" >&2
  sed -n '1,240p' "$STAGE_LOG" >&2
  FAIL_CONTAINERS="$(sudo -n -u _openswectl env HOME="$SERVICE_HOME" PATH="$PATH" /opt/homebrew/bin/docker ps -aq --filter label=com.docker.compose.project=open-swe-control-plane 2>/dev/null || true)"
  if test -n "$FAIL_CONTAINERS"; then
    sudo -n -u _openswectl env HOME="$SERVICE_HOME" PATH="$PATH" /opt/homebrew/bin/docker rm -f $FAIL_CONTAINERS || true
  fi
  kill "$STAGE_PID" 2>/dev/null || true
  wait "$STAGE_PID" 2>/dev/null || true
  sudo -n -u _openswectl env HOME="$SERVICE_HOME" /opt/homebrew/bin/colima stop || true
  sudo -n cp -p "$WRAPPER_BACKUP" "$WRAPPER"
  sudo -n rm -f "$CURRENT/.env"
  exit 1
fi
curl --fail --silent --show-error "http://127.0.0.1:$STAGE_PORT/ok"
lsof -nP -iTCP:"$STAGE_PORT" -sTCP:LISTEN | awk -v port="$STAGE_PORT" '$9 == "127.0.0.1:" port {found=1} END {exit !found}'
```

If staging reports a license rejection, preserve and report the exact error from
`STAGE_LOG`. Only then is obtaining `LANGGRAPH_CLOUD_LICENSE_KEY` for self-hosted Agent
Server use the single human-gated step. After the key is supplied out of band as
`NEW_LANGGRAPH_CLOUD_LICENSE_KEY`, add it with the existing timestamped backup convention,
then restart this runbook from the preconditions.

```bash
test -n "${NEW_LANGGRAPH_CLOUD_LICENSE_KEY:-}"
ENV_BACKUP="$ENV_FILE.pre-$(date -u +%Y%m%dT%H%M%SZ)"
sudo -n cp -p "$ENV_FILE" "$ENV_BACKUP"
printf '\nLANGGRAPH_CLOUD_LICENSE_KEY=%s\n' "$NEW_LANGGRAPH_CLOUD_LICENSE_KEY" | sudo -n tee -a "$ENV_FILE" >/dev/null
sudo -n chown root:_openswectl "$ENV_FILE"
sudo -n chmod 640 "$ENV_FILE"
unset NEW_LANGGRAPH_CLOUD_LICENSE_KEY
```

Stop the staged stack while retaining its PostgreSQL volume for the live start.

```bash
sudo -n -u _openswectl env HOME="$SERVICE_HOME" PATH="$PATH" /opt/homebrew/bin/docker ps --filter label=com.docker.compose.project=open-swe-control-plane
STAGE_CONTAINERS="$(sudo -n -u _openswectl env HOME="$SERVICE_HOME" PATH="$PATH" /opt/homebrew/bin/docker ps -aq --filter label=com.docker.compose.project=open-swe-control-plane)"
if test -n "$STAGE_CONTAINERS"; then
  sudo -n -u _openswectl env HOME="$SERVICE_HOME" PATH="$PATH" /opt/homebrew/bin/docker rm -f $STAGE_CONTAINERS
fi
kill "$STAGE_PID" 2>/dev/null || true
wait "$STAGE_PID" 2>/dev/null || true
sudo -n rm -f "$CURRENT/.env"
rm -f "$STAGE_LOG"
```

## Guard and prune the live in-memory server

Refuse cutover while runs are busy. The explicit in-memory `threads.prune` delete path
removes thread, run, and checkpoint data even though the default TTL sweep is unimplemented.
The first invocation is a dry run; inspect it before executing the second.

```bash
cat > /tmp/open-swe-prune-threads.py <<'PY'
import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta

from langgraph_sdk import get_client


def timestamp(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


async def run(execute):
    client = get_client(url="http://127.0.0.1:2029")
    busy = await client.threads.count(status="busy")
    if busy:
        raise SystemExit(f"refusing cutover with {busy} busy threads")
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    offset = 0
    candidates = []
    while True:
        page = await client.threads.search(limit=100, offset=offset, sort_by="updated_at", sort_order="asc")
        if not page:
            break
        candidates.extend(
            str(thread["thread_id"])
            for thread in page
            if thread.get("status") in {"idle", "error"}
            and timestamp(thread["updated_at"]) < cutoff
        )
        offset += len(page)
    if execute:
        for start in range(0, len(candidates), 100):
            await client.threads.prune(candidates[start : start + 100], strategy="delete")
    print(json.dumps({"cutoff": cutoff.isoformat(), "execute": execute, "count": len(candidates), "thread_ids": candidates}, indent=2))


parser = argparse.ArgumentParser()
parser.add_argument("--execute", action="store_true")
args = parser.parse_args()
asyncio.run(run(args.execute))
PY
"$CURRENT/.venv/bin/python" /tmp/open-swe-prune-threads.py
"$CURRENT/.venv/bin/python" /tmp/open-swe-prune-threads.py --execute
rm /tmp/open-swe-prune-threads.py
sleep 15
sudo -n du -sh "$STATE_DIR"
```

## Switch the system LaunchDaemon

Change only the wrapper role argument from `backend` to `production`, then replace the live
service in the system launchd domain.

```bash
sudo -n /usr/bin/python3 - "$BACKEND_PLIST" <<'PY'
import plistlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
with path.open("rb") as file:
    plist = plistlib.load(file)
expected = "/Library/Application Support/MobilyzeOpenSWEControlPlane/run"
arguments = plist.get("ProgramArguments", [])
if not arguments or arguments[0] != expected or arguments.count("backend") != 1:
    raise SystemExit(f"unexpected backend ProgramArguments: {arguments!r}")
plist["ProgramArguments"] = ["production" if value == "backend" else value for value in arguments]
with path.open("wb") as file:
    plistlib.dump(plist, file, sort_keys=False)
PY
sudo -n chown root:wheel "$BACKEND_PLIST"
sudo -n chmod 644 "$BACKEND_PLIST"
sudo -n launchctl bootout "system/$BACKEND_LABEL"
sudo -n launchctl bootstrap system "$BACKEND_PLIST"
sudo -n launchctl kickstart -k "system/$BACKEND_LABEL"
```

## Verify the live service

```bash
LIVE_HEALTHY=0
for attempt in $(seq 1 120); do
  if curl --fail --silent "http://127.0.0.1:$LIVE_PORT/ok" >/dev/null; then
    LIVE_HEALTHY=1
    break
  fi
  sleep 5
done
test "$LIVE_HEALTHY" -eq 1
curl --fail --silent --show-error "http://127.0.0.1:$LIVE_PORT/ok"
curl --fail --silent --show-error "http://127.0.0.1:$LIVE_PORT/health" >/dev/null
lsof -nP -iTCP:"$LIVE_PORT" -sTCP:LISTEN | awk -v port="$LIVE_PORT" '$9 == "127.0.0.1:" port {found=1} END {exit !found}'
if lsof -nP -iTCP:2024 -sTCP:LISTEN | grep .; then exit 1; fi
sudo -n launchctl print "system/$BACKEND_LABEL" >/dev/null
sudo -n launchctl print "system/$DASHBOARD_LABEL" >/dev/null
sudo -n -u _openswectl env HOME="$SERVICE_HOME" PATH="$PATH" /opt/homebrew/bin/docker ps --filter label=com.docker.compose.project=open-swe-control-plane
sudo -n -u _openswectl env HOME="$SERVICE_HOME" PATH="$PATH" /opt/homebrew/bin/docker stats --no-stream --format 'table {{.Name}}	{{.MemUsage}}'
```

After 20 normal runs, repeat `docker stats --no-stream`, record API-container RSS and
cold-start duration, and confirm the 24-hour sweep removes expired PostgreSQL-backed
threads. Historical SIGKILL forensics and host-side exit notification remain separate host
operations.

## Rollback from a fresh shell

Rollback derives the latest explicit backup record, so it does not depend on variables from
the cutover shell. It preserves the PostgreSQL volume for diagnosis and restores the pruned
in-memory backend.

```bash
set -euo pipefail
BASE=/var/db/mobilyze-open-swe-control-plane
SERVICE_HOME="$BASE/home"
CURRENT="$BASE/current"
OPS_DIR="/Library/Application Support/MobilyzeOpenSWEControlPlane"
WRAPPER="$OPS_DIR/run"
BACKEND_LABEL=com.mobilyze.open-swe-control-plane.backend
BACKEND_PLIST="/Library/LaunchDaemons/$BACKEND_LABEL.plist"
LIVE_PORT=2029
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
BACKUP_RECORD="$(sudo -n /bin/sh -c 'ls -1t "$1"/cutover-backup.* | sed -n "1p"' sh "$BASE")"
test -n "$BACKUP_RECORD"
WRAPPER_BACKUP="$(sudo -n sed -n '1p' "$BACKUP_RECORD")"
PLIST_BACKUP="$(sudo -n sed -n '2p' "$BACKUP_RECORD")"
sudo -n test -f "$WRAPPER_BACKUP"
sudo -n test -f "$PLIST_BACKUP"
sudo -n launchctl bootout "system/$BACKEND_LABEL" || true
CONTAINERS="$(sudo -n -u _openswectl env HOME="$SERVICE_HOME" PATH="$PATH" /opt/homebrew/bin/docker ps -aq --filter label=com.docker.compose.project=open-swe-control-plane)"
if test -n "$CONTAINERS"; then
  sudo -n -u _openswectl env HOME="$SERVICE_HOME" PATH="$PATH" /opt/homebrew/bin/docker rm -f $CONTAINERS
fi
sudo -n cp -p "$WRAPPER_BACKUP" "$WRAPPER"
sudo -n cp -p "$PLIST_BACKUP" "$BACKEND_PLIST"
sudo -n rm -f "$CURRENT/.env"
sudo -n -u _openswectl env HOME="$SERVICE_HOME" /opt/homebrew/bin/colima stop || true
sudo -n launchctl bootstrap system "$BACKEND_PLIST"
sudo -n launchctl kickstart -k "system/$BACKEND_LABEL"
for attempt in $(seq 1 60); do
  if curl --fail --silent "http://127.0.0.1:$LIVE_PORT/ok" >/dev/null; then break; fi
  sleep 5
done
curl --fail --silent --show-error "http://127.0.0.1:$LIVE_PORT/ok"
if lsof -nP -iTCP:2024 -sTCP:LISTEN | grep .; then exit 1; fi
```
