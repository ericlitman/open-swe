# Control-plane production cutover

This runbook moves a macOS control-plane host from the development-only, pickle-backed
`langgraph dev` runtime to the PostgreSQL/Redis-backed Agent Server, run as a docker
compose stack ([deploy/compose.yaml](../deploy/compose.yaml)) under the existing system
LaunchDaemon. It was executed on studio2 on 2026-07-28 for OSWE-177; every command below
is the form that actually worked there, including the failures the original revision of
this document did not anticipate.

Host shape this targets (verify each fact on your host before running anything):

- Backend and dashboard are **system LaunchDaemons**
  (`/Library/LaunchDaemons/com.mobilyze.open-swe-control-plane.{backend,dashboard}.plist`),
  root-owned, `UserName` `_openswectl`, executing an ops wrapper
  `"/Library/Application Support/MobilyzeOpenSWEControlPlane/run" <role>`.
- The wrapper sources the deployment env from
  `"/Library/Application Support/MobilyzeOpenSWEControlPlane/env"` (root:_openswectl 0640)
  and exports `HOME=/var/db/mobilyze-open-swe-control-plane/home`.
- The release checkout is `/opt/mobilyze/open-swe-control-plane/current` (root-owned
  symlink into `releases/<sha>`); the live port is 2029. There is no repo-adjacent `.env`.
- Colima is the container runtime; the VM belongs to `_openswectl` (not a login user), so
  the stack survives logouts. All privileged steps use passwordless `sudo -n`.

## Why not `langgraph up`

`langgraph-cli` 0.4.30 paired with api 0.11.1 is broken: `langgraph up` tags an image
containing no project code and no entrypoint (`docker inspect` shows `Entrypoint=null`,
`Cmd=["python3"]`, `/deps` and `/api` absent), starts the stack, watches the api service
exit 0 instantly, and itself exits 0. Nothing in its output reports the failure. The
Dockerfile the same CLI *renders* is correct — so production builds the rendered
Dockerfile with plain `docker build` and runs the pinned compose file instead
(`make production-image` / `make production`). Do not reintroduce `langgraph up` without
verifying the built image's entrypoint and contents.

## License

Resolved empirically on studio2: the existing `LANGSMITH_API_KEY` boots the standalone
Agent Server fully licensed (`api_variant=licensed` throughout startup logs). No
`LANGGRAPH_CLOUD_LICENSE_KEY` was needed. If a staged startup on your host rejects the
key instead, preserve the exact server error; only then is obtaining
`LANGGRAPH_CLOUD_LICENSE_KEY` a human-gated step. Add it to the ops env file with the
timestamped-backup convention and restage.

## Session-survival warning for every long step

Image builds and attached compose runs take longer than interactive tool sessions allow:
harness-managed shells kill backgrounded work at ~10 minutes, and plainly detached
children get reaped with their process group. Run anything long-lived under a **one-shot
LaunchDaemon** (template below) — launchd parentage is immune to session cleanup. The
same failure mode is why the live service itself is a LaunchDaemon and not a shell.

## Preconditions

```bash
set -euo pipefail
DEPLOYMENT_ROOT=/opt/mobilyze/open-swe-control-plane
CURRENT="$DEPLOYMENT_ROOT/current"
BASE=/var/db/mobilyze-open-swe-control-plane
SERVICE_HOME="$BASE/home"
OPS_DIR="/Library/Application Support/MobilyzeOpenSWEControlPlane"
WRAPPER="$OPS_DIR/run"
ENV_FILE="$OPS_DIR/env"
BACKEND_LABEL=com.mobilyze.open-swe-control-plane.backend
BACKEND_PLIST="/Library/LaunchDaemons/$BACKEND_LABEL.plist"
LIVE_PORT=2029
STAGE_PORT=2030
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
sudo -n true
sudo -n test -f "$WRAPPER" && sudo -n test -f "$ENV_FILE" && sudo -n test -f "$BACKEND_PLIST"
test -f "$CURRENT/Makefile" && test -x "$CURRENT/.venv/bin/langgraph"
test "$(sudo -n plutil -extract UserName raw -o - "$BACKEND_PLIST")" = _openswectl
curl --fail --silent --show-error "http://127.0.0.1:$LIVE_PORT/ok"
lsof -nP -iTCP:"$STAGE_PORT" -sTCP:LISTEN | grep . && exit 1 || true
make -C "$CURRENT" production-check
command -v docker >/dev/null || brew install docker docker-compose
brew list colima >/dev/null
```

Normalize service-home ownership — root-context runs leave root-owned droppings under
the service home that later break colima and uv as `_openswectl`:

```bash
sudo -n chown -R _openswectl:_openswectl "$BASE/home" "$BASE/cache"
```

## Materialize the container env

`langgraph.json` declares `env: ".env"` and compose reads `env_file`, but the deployment
env lives in the root-owned wrapper file. Materialize a filtered copy into the release
dir. **The filter is load-bearing**: the ops env file sets `PATH=` for the host daemon,
and if that line reaches the container it shadows the image's interpreter — the server
dies with `python3: not found`, `uvicorn: not found`, `core-api-grpc command not found`.

```bash
sudo -n sh -c "umask 077; grep -vE '^PATH=' '$ENV_FILE' > '$CURRENT/.env'"
sudo -n chown _openswectl:_openswectl "$CURRENT/.env"
sudo -n chmod 600 "$CURRENT/.env"
```

`_openswectl` cannot *create* files in the root-owned release dir, so this root-side step
must be repeated after **every `release-activate`** (each release is a new directory).
The wrapper's production role refreshes the file's contents thereafter (overwriting an
existing `_openswectl`-owned file works; creating one does not).

## Back up, then add the production wrapper role

```bash
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
sudo -n cp -p "$WRAPPER" "$WRAPPER.bak.$STAMP"
sudo -n cp -p "$BACKEND_PLIST" "$BACKEND_PLIST.bak.$STAMP"
printf '%s\n%s\n' "$WRAPPER.bak.$STAMP" "$BACKEND_PLIST.bak.$STAMP" | sudo -n tee "$BASE/cutover-backup.$STAMP" >/dev/null
sudo -n chmod 600 "$BASE/cutover-backup.$STAMP"
```

Insert this role after the wrapper's shebang (guard on the marker comment; refuse if
`OPEN_SWE_PRODUCTION_ROLE` is already present). Deployed form:

```sh
# OPEN_SWE_PRODUCTION_ROLE
if [ "${1:-}" = "production" ]; then
  ENV_FILE="/Library/Application Support/MobilyzeOpenSWEControlPlane/env"
  DEPLOYMENT_ROOT=/opt/mobilyze/open-swe-control-plane
  export HOME=/var/db/mobilyze-open-swe-control-plane/home
  export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
  export XDG_CACHE_HOME=/var/db/mobilyze-open-swe-control-plane/cache
  cd /var/db/mobilyze-open-swe-control-plane
  PORT="${2:-2029}"
  if ! /opt/homebrew/bin/colima status >/dev/null 2>&1; then
    /opt/homebrew/bin/colima start --runtime docker --cpu 4 --memory 12 --disk 100
  fi
  umask 077
  grep -vE '^PATH=' "$ENV_FILE" > "$DEPLOYMENT_ROOT/current/.env"
  chmod 600 "$DEPLOYMENT_ROOT/current/.env"
  OPEN_SWE_API_PORT="$PORT" exec /opt/homebrew/bin/docker-compose \
    --project-name open-swe-control-plane \
    -f "/Library/Application Support/MobilyzeOpenSWEControlPlane/compose.yaml" up
fi
```

Copy [deploy/compose.yaml](../deploy/compose.yaml) to `"$OPS_DIR/compose.yaml"` and set
its `env_file` to the absolute `$CURRENT/.env` path (the ops copy pins absolute paths;
the repo copy stays parameterized). Build the image once, from the rendered Dockerfile:

```bash
cd "$CURRENT" && .venv/bin/langgraph dockerfile "$OPS_DIR/generated.Dockerfile"
sudo -n -u _openswectl env HOME="$SERVICE_HOME" PATH="$PATH" docker build \
  -f "$OPS_DIR/generated.Dockerfile" -t open-swe-cp-api:current "$CURRENT"
```

## Stage on port 2030

One-shot staging daemon (delete it afterward — its `RunAtLoad` would race the live stack
at next boot):

```bash
sudo -n /usr/bin/python3 - <<'PY'
import plistlib, pathlib
plist = {
    "Label": "com.mobilyze.open-swe-control-plane.stage",
    "ProgramArguments": ["/Library/Application Support/MobilyzeOpenSWEControlPlane/run", "production", "2030"],
    "UserName": "_openswectl",
    "RunAtLoad": True,
    "KeepAlive": False,
    "WorkingDirectory": "/var/db/mobilyze-open-swe-control-plane",
    "StandardOutPath": "/var/log/mobilyze-open-swe-control-plane/stage.log",
    "StandardErrorPath": "/var/log/mobilyze-open-swe-control-plane/stage.log",
}
p = pathlib.Path("/Library/LaunchDaemons/com.mobilyze.open-swe-control-plane.stage.plist")
with p.open("wb") as f: plistlib.dump(plist, f, sort_keys=False)
p.chmod(0o644)
PY
sudo -n launchctl bootstrap system /Library/LaunchDaemons/com.mobilyze.open-swe-control-plane.stage.plist
until curl --fail --silent "http://127.0.0.1:$STAGE_PORT/ok" >/dev/null; do sleep 5; done
curl --fail --silent --show-error "http://127.0.0.1:$STAGE_PORT/ok"
```

On failure read `/var/log/mobilyze-open-swe-control-plane/stage.log` and the api
container logs. Success here proves image correctness, database migrations, and the
license — before the live service is touched.

## Migrate the store and crons (required)

The LangGraph **store** holds the dashboard's operating state — team settings (including
auto-merge mode), user profiles, enabled repos, review styles, plan records — and crons
hold the nightly analyzer registrations. None of that is thread state: skipping this step
silently resets dashboard configuration. With the dev server still live on 2029 and the
staged server on 2030 (ran in seconds on 377 items / 824 namespaces on studio2):

```python
from langgraph_sdk import get_sync_client
src = get_sync_client(url="http://127.0.0.1:2029")
dst = get_sync_client(url="http://127.0.0.1:2030")
namespaces, offset = [], 0
while True:
    page = src.store.list_namespaces(limit=100, offset=offset)
    if not page: break
    namespaces.extend(tuple(n) for n in page); offset += len(page)
for ns in namespaces:
    off = 0
    while True:
        items = src.store.search_items(list(ns), limit=100, offset=off)
        items = items.get("items", []) if isinstance(items, dict) else items.items
        if not items: break
        for it in items:
            dst.store.put_item(list(ns), it["key"], it["value"])
        off += len(items)
crons = src.crons.search(limit=100)  # save payloads; recreate on the live server post-switch
```

Thread/checkpoint history is intentionally **not** migrated (the documented continuity
tradeoff): deterministic thread ids cold-start and recover from committed branches.
Do not bother pruning the dev server to shrink the switchover: on studio2, pruning 217
threads persisted the deletions but did not shrink the checkpoint pickles, and the next
dev boot still rehydrated 13.5 GB — evidence that config-only retention could never fix
the in-memory runtime.

## Switch the live LaunchDaemon

Stop the staging daemon first, then swap the live role argument `backend` → `production`
(plistlib edit asserting the expected existing ProgramArguments), and replace the
service:

```bash
sudo -n launchctl bootout system/com.mobilyze.open-swe-control-plane.stage || true
sudo -n rm -f /Library/LaunchDaemons/com.mobilyze.open-swe-control-plane.stage.plist
# ... plist role swap ...
sudo -n launchctl bootout "system/$BACKEND_LABEL"
sudo -n launchctl bootstrap system "$BACKEND_PLIST" || { sleep 5; sudo -n launchctl bootstrap system "$BACKEND_PLIST"; }
sudo -n launchctl kickstart -k "system/$BACKEND_LABEL"
until curl --fail --silent "http://127.0.0.1:$LIVE_PORT/ok" >/dev/null; do sleep 5; done
```

`launchctl bootstrap` immediately after `bootout` can fail with `5: Input/output error`
while the old job drains — that is the retry above, and it must not be skipped: on
studio2 the first bootstrap failed exactly this way and the plane stayed down until the
retry. Verify afterward: `/ok` and `/health` on 2029, containers healthy and bound to
`127.0.0.1:2029->8000`, nothing listening on 2024/2030, no `langgraph dev` process, the
dashboard daemon untouched, and a store spot-check through 2029 (e.g. a known plan
record) proving the migrated data is served. Recreate the saved crons against 2029.

## Rollback from a fresh shell

Derives everything from the newest `cutover-backup.*` record; preserves the PostgreSQL
volume; restores the pruned in-memory backend.

```bash
set -euo pipefail
BASE=/var/db/mobilyze-open-swe-control-plane
SERVICE_HOME="$BASE/home"
OPS_DIR="/Library/Application Support/MobilyzeOpenSWEControlPlane"
BACKEND_LABEL=com.mobilyze.open-swe-control-plane.backend
BACKEND_PLIST="/Library/LaunchDaemons/$BACKEND_LABEL.plist"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
BACKUP_RECORD="$(sudo -n /bin/sh -c 'ls -1t "$1"/cutover-backup.* | sed -n "1p"' sh "$BASE")"
WRAPPER_BACKUP="$(sudo -n sed -n '1p' "$BACKUP_RECORD")"; PLIST_BACKUP="$(sudo -n sed -n '2p' "$BACKUP_RECORD")"
sudo -n test -f "$WRAPPER_BACKUP" && sudo -n test -f "$PLIST_BACKUP"
sudo -n launchctl bootout "system/$BACKEND_LABEL" || true
CONTAINERS="$(sudo -n -u _openswectl env HOME="$SERVICE_HOME" PATH="$PATH" docker ps -aq --filter label=com.docker.compose.project=open-swe-control-plane)"
test -n "$CONTAINERS" && sudo -n -u _openswectl env HOME="$SERVICE_HOME" PATH="$PATH" docker stop $CONTAINERS
sudo -n cp -p "$WRAPPER_BACKUP" "$OPS_DIR/run"
sudo -n cp -p "$PLIST_BACKUP" "$BACKEND_PLIST"
sudo -n -u _openswectl env HOME="$SERVICE_HOME" /opt/homebrew/bin/colima stop || true
sudo -n launchctl bootstrap system "$BACKEND_PLIST" || { sleep 5; sudo -n launchctl bootstrap system "$BACKEND_PLIST"; }
sudo -n launchctl kickstart -k "system/$BACKEND_LABEL"
until curl --fail --silent "http://127.0.0.1:2029/ok" >/dev/null; do sleep 5; done
```

Store/cron writes made after the cutover exist only in PostgreSQL and are not reflected
back into the pickles by rollback.

## Post-cutover

Studio2 outcome (2026-07-28): api container 219 MiB vs the dev runtime's 13,554 MiB
fresh-boot; ThreadTTL (delete / 1440 min / hourly sweep) active in the runtime config;
`api_variant=licensed`. Remaining operational notes: re-materialize `current/.env` after
each release-activate (until the ops installer does it); the colima VM cap (12 GiB) can
be tuned down once the soak confirms headroom; exit *notification* for unplanned backend
exits is tracked as a follow-up — launchd KeepAlive plus container restart policies
already handle restart-on-crash.
