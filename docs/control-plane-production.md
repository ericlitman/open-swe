# Control-plane production cutover

This runbook moves a macOS control-plane host from the development-only, pickle-backed
`langgraph dev` runtime to the PostgreSQL/Redis-backed Agent Server started by
`make production`. It targets Colima and the user LaunchAgents
`com.mobilyze.open-swe-control-plane.backend` and
`com.mobilyze.open-swe-control-plane.dashboard`.

The shipped `langgraph.json` deletes threads, their runs, and their checkpoints after 24
hours. Agent Server 0.11.1 does not execute that TTL sweep in the in-memory runtime, so
`make dev` must not be used for the production control plane. `make production` uses
`langgraph up` on port 2024 and fixes the Compose project name to
`open-swe-control-plane`, keeping the PostgreSQL volume stable across release directories.

## License gate

A standalone production Agent Server requires `LANGSMITH_API_KEY` and
`LANGGRAPH_CLOUD_LICENSE_KEY`. The latter is a LangSmith license key with the Enterprise
self-hosted Agent Server entitlement; the server validates it against
`https://beacon.langchain.com` at startup. Obtaining that entitlement from LangChain is the
single human-gated step if the host does not already have a key. A normal LangSmith API
key is not a substitute for the license key.

## Preconditions

Run every command as the user that owns the LaunchAgents. Stop if any assertion fails.

```bash
set -euo pipefail
BACKEND_LABEL=com.mobilyze.open-swe-control-plane.backend
DASHBOARD_LABEL=com.mobilyze.open-swe-control-plane.dashboard
BACKEND_PLIST="$HOME/Library/LaunchAgents/$BACKEND_LABEL.plist"
DASHBOARD_PLIST="$HOME/Library/LaunchAgents/$DASHBOARD_LABEL.plist"
DOMAIN="gui/$(id -u)"
test -f "$BACKEND_PLIST"
test -f "$DASHBOARD_PLIST"
BACKEND_DIR="$(plutil -extract WorkingDirectory raw -o - "$BACKEND_PLIST")"
test -f "$BACKEND_DIR/langgraph.json"
test -f "$BACKEND_DIR/.env"
make -C "$BACKEND_DIR" production-check
launchctl print "$DOMAIN/$BACKEND_LABEL" >/dev/null
launchctl print "$DOMAIN/$DASHBOARD_LABEL" >/dev/null
curl --fail --silent --show-error http://127.0.0.1:2024/ok
```

Check required secrets without printing them. If the license check fails, stop for the
human-gated license step above. When only the repository's existing
`LANGSMITH_API_KEY_PROD` is present, the command adds the standard name consumed by the
standalone server.

```bash
python3 - "$BACKEND_DIR/.env" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
values = {}
for raw in path.read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip().strip('"').strip("'")
if not values.get("LANGGRAPH_CLOUD_LICENSE_KEY"):
    raise SystemExit("missing LANGGRAPH_CLOUD_LICENSE_KEY: obtain the Enterprise self-hosted Agent Server license")
if not values.get("LANGSMITH_API_KEY"):
    if not values.get("LANGSMITH_API_KEY_PROD"):
        raise SystemExit("missing LANGSMITH_API_KEY and LANGSMITH_API_KEY_PROD")
    with path.open("a") as file:
        file.write(f"\nLANGSMITH_API_KEY={values['LANGSMITH_API_KEY_PROD']}\n")
PY
```

Verify Colima and install only the unprivileged Homebrew Docker clients if needed.

```bash
command -v brew >/dev/null
command -v uv >/dev/null
brew list colima >/dev/null
colima status
if ! command -v docker >/dev/null || ! command -v docker-compose >/dev/null; then
  brew install docker docker-compose
fi
docker context use colima
docker info >/dev/null
docker-compose version
```

## Prune legacy in-memory state

The 0.11.1 in-memory runtime ignores default TTLs, but its explicit `threads.prune` delete
path removes thread, run, and checkpoint data. Create the helper, run it first as a dry run,
inspect its JSON list, then execute it. Busy and interrupted threads are never selected.

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
    client = get_client(url="http://127.0.0.1:2024")
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    offset = 0
    candidates = []
    while True:
        page = await client.threads.search(
            limit=100,
            offset=offset,
            sort_by="updated_at",
            sort_order="asc",
        )
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
uv run --directory "$BACKEND_DIR" python /tmp/open-swe-prune-threads.py
uv run --directory "$BACKEND_DIR" python /tmp/open-swe-prune-threads.py --execute
rm /tmp/open-swe-prune-threads.py
sleep 15
du -sh "$BACKEND_DIR/.langgraph_api"
```

Require an idle cutover window.

```bash
uv run --directory "$BACKEND_DIR" python - <<'PY'
import asyncio
from langgraph_sdk import get_client


async def main():
    client = get_client(url="http://127.0.0.1:2024")
    busy = await client.threads.count(status="busy")
    if busy:
        raise SystemExit(f"refusing cutover with {busy} busy threads")


asyncio.run(main())
PY
```

## Cut over the backend LaunchAgent

Back up the plist, replace only its command, stop the development server, and bootstrap the
persistence-backed target. The dashboard LaunchAgent remains unchanged. The new database
starts empty; deterministic follow-ups cold-start and recover from committed branches.

```bash
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PLIST_BACKUP="$BACKEND_PLIST.$STAMP.bak"
cp "$BACKEND_PLIST" "$PLIST_BACKUP"
python3 - "$BACKEND_PLIST" <<'PY'
import plistlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
with path.open("rb") as file:
    plist = plistlib.load(file)
working_directory = plist.get("WorkingDirectory")
if not working_directory or not (Path(working_directory) / "Makefile").is_file():
    raise SystemExit("backend plist has no valid WorkingDirectory")
arguments = plist.get("ProgramArguments", [])
if not any("langgraph" in str(value) or str(value) == "dev" for value in arguments):
    raise SystemExit(f"unexpected backend ProgramArguments: {arguments!r}")
plist["ProgramArguments"] = ["/usr/bin/make", "-C", working_directory, "production"]
with path.open("wb") as file:
    plistlib.dump(plist, file, sort_keys=False)
PY
launchctl bootout "$DOMAIN/$BACKEND_LABEL"
launchctl bootstrap "$DOMAIN" "$BACKEND_PLIST"
launchctl kickstart -k "$DOMAIN/$BACKEND_LABEL"
```

## Verify

Wait for the image build and database migrations, then verify the API, dashboard, stable
Compose project, and resident footprint.

```bash
for attempt in $(seq 1 120); do
  if curl --fail --silent http://127.0.0.1:2024/ok >/dev/null; then break; fi
  sleep 5
done
curl --fail --silent --show-error http://127.0.0.1:2024/ok
curl --fail --silent --show-error http://127.0.0.1:2024/health >/dev/null
launchctl print "$DOMAIN/$BACKEND_LABEL" >/dev/null
launchctl print "$DOMAIN/$DASHBOARD_LABEL" >/dev/null
docker ps --filter label=com.docker.compose.project=open-swe-control-plane
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}'
```

After 20 normal runs, repeat `docker stats --no-stream` and record API-container RSS and
cold-start duration. Host log forensics for the historical SIGKILL and host-side exit
notification wiring remain separate control-plane operations.

## Rollback

Rollback preserves the PostgreSQL volume for a later retry and restores the pruned
in-memory runtime.

```bash
set -euo pipefail
launchctl bootout "$DOMAIN/$BACKEND_LABEL" || true
CONTAINERS="$(docker ps -aq --filter label=com.docker.compose.project=open-swe-control-plane)"
if test -n "$CONTAINERS"; then docker rm -f $CONTAINERS; fi
cp "$PLIST_BACKUP" "$BACKEND_PLIST"
launchctl bootstrap "$DOMAIN" "$BACKEND_PLIST"
launchctl kickstart -k "$DOMAIN/$BACKEND_LABEL"
for attempt in $(seq 1 60); do
  if curl --fail --silent http://127.0.0.1:2024/ok >/dev/null; then break; fi
  sleep 5
done
curl --fail --silent --show-error http://127.0.0.1:2024/ok
```
