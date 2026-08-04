#!/usr/bin/env python3
"""openswe-run: dispatch one Open SWE ticket via the Linear-comment path and watch it.

Token-frugal single-run sibling of openswe-wave. Run state changes go through
@openswe Linear comments (product path); runs are never created, resumed, or
mutated through the LangGraph API. The exceptions are `approve` and `reject`,
which transition the plan record exactly as the dashboard's endpoints do,
because a comment alone leaves the run merge-ineligible (draft PR, auto-merge
never armed). Every command appends evidence to <stable-root>/handoffs/.

Stdlib-only. The openswe_wave module is imported for its pure classifiers and
live watching subprocesses wave-monitor, both taken from this skill's vendored
copies or, in a checkout, the sibling openswe-wave. They run under a resolved
interpreter (control-plane venv or uv) because their live paths need httpx +
langgraph_sdk, absent from system python3 (see dogfood log 2026-07-26).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
DEFAULT_STABLE_ROOT = str(Path.home() / "projects" / "open-swe")
LINEAR_URL = "https://api.linear.app/graphql"
LOCAL_LANGGRAPH_ENDPOINTS = (
    ("http://127.0.0.1:2029", "studio2-tunnel:2029"),
    ("http://127.0.0.1:12029", "studio2-tunnel:12029"),
)
LOCAL_LANGGRAPH_URL = LOCAL_LANGGRAPH_ENDPOINTS[0][0]
CONTROL_PLANE_PYTHON = "/opt/mobilyze/open-swe-control-plane/current/.venv/bin/python"
LINEAR_ENV_ERROR = "LINEAR_API_KEY is not set. Set it with: export LINEAR_API_KEY=..."
LANGGRAPH_ENV_ERROR = (
    "No healthy LangGraph endpoint was found. Set LANGGRAPH_URL to a healthy endpoint, "
    "or start a supported Studio2 tunnel on local port 2029 or 12029."
)
GH_ENV_ERROR = (
    "GH_TOKEN is not set and `gh auth token` produced nothing. "
    "Set it with: export GH_TOKEN=$(gh auth token)  # or a PAT with repo scope"
)
LINEAR_MENTION_SCOPE_ERROR = (
    "Linear workspace app grant is stripped of app:mentionable (OSWE-152 mechanism). "
    "Run this full-scope client-credentials mint, discard the token, then retry this "
    "command as-is:\n"
    "curl -fsS https://api.linear.app/oauth/token "
    "--data-urlencode grant_type=client_credentials "
    '--data-urlencode client_id="$LINEAR_CLIENT_ID" '
    '--data-urlencode client_secret="$LINEAR_CLIENT_SECRET" '
    "--data-urlencode scope=read,write,app:assignable,app:mentionable >/dev/null"
)
WAKE_TIMEOUT_EXIT = 3
CHILD_FAILURE_EXIT = 4
HANDOFF_TIMEOUT_SECONDS = 60.0
HANDOFF_POLL_INTERVAL_SECONDS = 2.0
HANDOFF_SNAPSHOT_TIMEOUT_SECONDS = 10
LINEAR_AGENT_ERROR_PREFIX = "❌ **Agent Error**"
PHASE_TIMEOUT_MINUTES = {"plan": 30.0, "delivery": 90.0}
WATCH_INTERVAL_SECONDS = 60.0
WATCH_START_TIMEOUT_SECONDS = 70.0
WATCH_ACTION_PHASE = {
    "approval": "delivery",
    "rejection": "plan",
    "comment": "delivery",
    "nudge": "delivery",
}
# Matches unfilled template placeholders like <TICKET> or <owner/repo>; the
# ':' exclusion spares autolinks like <https://...>.
PLACEHOLDER_RE = re.compile(r"<[A-Za-z][^>:\n]*>")
FENCED_CODE_RE = re.compile(
    r"^ {0,3}(?:"
    r"(?P<backticks>`{3,})(?!`)[^`\n]*\n.*?^ {0,3}(?P=backticks)`*(?!`)[ \t]*(?:\n|$)"
    r"|(?P<tildes>~{3,})(?!~)[^\n]*\n.*?^ {0,3}(?P=tildes)~*(?!~)[ \t]*(?:\n|$)"
    r")",
    re.DOTALL | re.MULTILINE,
)
INLINE_CODE_RE = re.compile(r"(?<![\\`])(?P<ticks>`+)(?!`).*?(?<!`)(?P=ticks)(?!`)", re.DOTALL)
# Remove the mention workaround when OSWE-144 closes.
DIRECTIVE_MENTION_RE = re.compile(r"@openswe\b", re.IGNORECASE)
# Remove the repository-directive workaround when OSWE-166 closes.
REPO_DIRECTIVE_RE = re.compile(
    r"\brepo(?:\s+|:\s*)[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\b",
    re.IGNORECASE,
)

DISPATCH_TEMPLATE = """@openswe repo {repo} — Execute {ticket} only.

Enter plan mode first. Re-anchor all cited paths and symbols against `{ref}`, state any refuted premise as a Challenge, and do not implement until approval is posted in this Linear thread.

Required scope: {scope}.
Boundaries: {boundaries}.
Verification: {verify}.
Code standard: smallest root-cause change; no speculative validation or layered defenses; the diff must be acceptable upstream.
PR body: include the Linear reference and `Closes {ticket}` as a standalone line. Let normal Open SWE Review and required CI run; do not directly merge or bypass gates.
"""

BUNDLE_DISPATCH_TEMPLATE = """@openswe repo {repo} — Execute one ticket bundle with primary {primary} and included tickets {included}.

Enter plan mode first. Read and reconcile every bundle ticket before planning: {members}. Re-anchor all cited paths and symbols against `{ref}`, state any refuted premise as a Challenge, and do not implement until the combined plan is approved in this Linear thread.

Treat the bundle as one atomic scope on the primary thread and one thread-stable branch. Included tickets must not be dispatched independently.
Required scope: {scope}.
Boundaries: {boundaries}.
Verification: {verify}.
Code standard: smallest root-cause change; no speculative validation or layered defenses; the diff must be acceptable upstream.
Open or update exactly one PR for the bundle. Its body must include the Linear references and these standalone closing lines:
{closing_lines}
Let normal Open SWE Review and required CI run; do not directly merge or bypass gates.
"""

BUNDLE_MANIFEST_TAG = "bundle-manifest"

NUDGE_TEMPLATE = """@openswe Status check on {ticket}: no visible progress for {minutes} minutes. Post a brief status update in this thread (current step, and the blocker if you are blocked)."""


class RunError(RuntimeError):
    """A named, actionable failure. Message must tell the caller what to run."""


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stable_root() -> Path:
    return Path(os.environ.get("OPENSWE_STABLE_ROOT") or DEFAULT_STABLE_ROOT)


# --------------------------------------------------------------------------- dogfood log


def _exclude_handoffs(root: Path) -> None:
    """Add handoffs to a checkout-local Git exclude when available."""
    try:
        probe = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "info/exclude",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        exclude_text = probe.stdout.strip()
        if probe.returncode != 0 or not exclude_text:
            return
        exclude = Path(exclude_text)
        existing = exclude.read_text() if exclude.exists() else ""
        if "handoffs/" not in existing.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write("handoffs/\n")
    except OSError:
        return


def ensure_handoffs() -> Path:
    """Create <stable-root>/handoffs/ and keep it locally ignored in Git checkouts."""
    root = stable_root()
    handoffs = root / "handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)
    _exclude_handoffs(root)
    return handoffs


def log_path(ticket: str, *, new_run: bool = False) -> Path:
    handoffs = ensure_handoffs()
    candidates = sorted(handoffs.glob(f"{ticket.upper()}-*-run.md"))
    if candidates:
        latest = candidates[-1]
        dispatched = any(
            re.match(r"^- \S+ \[cmd\] dispatched ", line)
            for line in latest.read_text().splitlines()
        )
        if not new_run or not dispatched:
            return latest
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = handoffs / f"{ticket.upper()}-{stamp}-run.md"
    if not path.exists():
        header = (
            f"# openswe-run dogfood log — {ticket.upper()}\n\n"
            f"Started {now_iso()} on {os.uname().nodename} by openswe-run.\n"
            f"Purpose: evidence of process/substrate/ergonomics issues while running Open SWE.\n"
            f"Tags: [cmd] actions, [wake] monitor wakes, [note] observations, "
            f"[ISSUE] dogfood findings, [error] failures.\n\n"
        )
        path.write_text(header)
    return path


def known_ids_path(ticket: str) -> Path:
    run_log = log_path(ticket)
    return run_log.with_name(f"{run_log.stem}-known-comment-ids.json")


def watch_state_path(ticket: str) -> Path:
    run_log = log_path(ticket)
    return run_log.with_name(f"{run_log.stem}-watch.json")


def _atomic_write_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _read_json_object(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _watch_lock_path(state_path: Path) -> Path:
    return state_path.with_suffix(state_path.suffix + ".lock")


def _watch_lock_held(state_path: Path) -> bool:
    lock_path = _watch_lock_path(state_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def _run_repo(ticket: str) -> str:
    path = log_path(ticket)
    pattern = re.compile(r"^- \S+ \[cmd\] dispatched \S+ to ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) ")
    for line in reversed(path.read_text().splitlines()):
        match = pattern.match(line)
        if match:
            return match.group(1)
    raise RunError(
        f"No dispatched repository is recorded for {ticket.upper()}; "
        "start the ticket before posting a monitor-lane action."
    )


def post_watch_context(ticket: str, action: str, repo: str | None = None) -> dict:
    phase = WATCH_ACTION_PHASE[action]
    state_path = watch_state_path(ticket)
    return {
        "ticket": ticket.upper(),
        "repo": repo or _run_repo(ticket),
        "phase": phase,
        "interval_seconds": WATCH_INTERVAL_SECONDS,
        "timeout_minutes": PHASE_TIMEOUT_MINUTES[phase],
        "state_path": str(state_path),
        "output_path": str(state_path.with_name(f"{state_path.stem}-output.jsonl")),
        "error_path": str(state_path.with_name(f"{state_path.stem}-error.log")),
    }


def _watch_state_matches(state: dict | None, expected: dict) -> bool:
    return bool(
        state
        and state.get("status") == "ready"
        and state.get("ticket") == expected["ticket"]
        and state.get("repo") == expected["repo"]
        and state.get("phase") == expected["phase"]
        and state.get("interval_seconds") == expected["interval_seconds"]
        and state.get("timeout_minutes") == expected["timeout_minutes"]
    )


def _watch_result(status: str, expected: dict) -> dict:
    return {
        "status": status,
        "phase": expected["phase"],
        "interval_seconds": expected["interval_seconds"],
        "timeout_minutes": expected["timeout_minutes"],
    }


def ensure_post_watch(expected: dict) -> dict:
    state_path = Path(expected["state_path"])
    state = _read_json_object(state_path)
    if _watch_lock_held(state_path):
        state = _read_json_object(state_path)
        if _watch_state_matches(state, expected):
            return _watch_result("verified", expected)
        raise RunError(
            f"A live watch exists for {expected['ticket']}, but it does not match the required "
            f"{expected['phase']} watch (interval {expected['interval_seconds']:g}s, timeout "
            f"{expected['timeout_minutes']:g}m). The posted action was handed off, but the "
            "required watch could not be verified."
        )

    token = uuid.uuid4().hex
    ready_path = state_path.with_name(f".{state_path.name}.{token}.ready")
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "watch",
        "--ticket",
        expected["ticket"],
        "--repo",
        expected["repo"],
        "--phase",
        expected["phase"],
        "--interval",
        str(expected["interval_seconds"]),
        "--timeout-min",
        str(expected["timeout_minutes"]),
        "--managed-state-file",
        str(state_path),
        "--managed-token",
        token,
        "--ready-file",
        str(ready_path),
    ]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    error_path = Path(expected["error_path"])
    stderr_redirected = not sys.stderr.isatty()
    stderr_offset = error_path.stat().st_size if stderr_redirected and error_path.exists() else 0
    with ExitStack() as stack:
        stdout = (
            None
            if sys.stdout.isatty()
            else stack.enter_context(Path(expected["output_path"]).open("a"))
        )
        stderr = stack.enter_context(error_path.open("a")) if stderr_redirected else None
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
    deadline = time.monotonic() + WATCH_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        state = _read_json_object(state_path)
        if _watch_state_matches(state, expected) and state.get("token") == token:
            if _watch_lock_held(state_path):
                ready_path.unlink(missing_ok=True)
                return _watch_result("rearmed", expected)
        if process.poll() is not None:
            break
        time.sleep(0.1)
    state = _read_json_object(state_path)
    if _watch_state_matches(state, expected) and _watch_lock_held(state_path):
        ready_path.unlink(missing_ok=True)
        return _watch_result("verified", expected)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    ready_path.unlink(missing_ok=True)
    detail = (
        f"watch process exited with status {process.poll()}" if process.poll() is not None else ""
    )
    if detail and stderr_redirected:
        try:
            with error_path.open() as handle:
                handle.seek(stderr_offset)
                lines = handle.read().splitlines()
        except OSError:
            lines = []
        last_stderr = next((line for line in reversed(lines) if line.strip()), None)
        if last_stderr:
            detail += f"; last stderr: {last_stderr}"
    dogfood(
        expected["ticket"],
        "error",
        f"posted action handed off but {expected['phase']} watch re-arm failed: {detail}",
    )
    raise RunError(
        f"The action was posted and handed off, but no live {expected['phase']} watch could be "
        f"verified for {expected['ticket']} (interval {expected['interval_seconds']:g}s, timeout "
        f"{expected['timeout_minutes']:g}m)" + (f": {detail}" if detail else ".")
    )


def dogfood(ticket: str, tag: str, text: str) -> None:
    """Append one evidence line to the run's dogfood log."""
    path = log_path(ticket)
    clean = " ".join(text.split())
    with path.open("a") as fh:
        fh.write(f"- {now_iso()} [{tag}] {clean}\n")


def write_bundle_manifest(ticket: str, issues: list[dict]) -> None:
    """Persist the latest normalized dispatch topology in the primary run log."""
    members = [
        {"identifier": str(issue["identifier"]).strip().upper(), "issue_id": str(issue["id"])}
        for issue in issues
    ]
    payload = {"version": 1, "primary": members[0], "members": members}
    dogfood(ticket, BUNDLE_MANIFEST_TAG, json.dumps(payload, separators=(",", ":")))


def load_bundle_manifest(ticket: str, path: Path | None = None) -> dict | None:
    """Recover and validate the latest bundle record without creating a log."""
    if path is None:
        handoffs = stable_root() / "handoffs"
        if not handoffs.is_dir():
            return None
        candidates = sorted(handoffs.glob(f"{ticket.upper()}-*-run.md"))
        if not candidates:
            return None
        path = candidates[-1]
    record_pattern = re.compile(rf"^- \S+ \[{re.escape(BUNDLE_MANIFEST_TAG)}\] (?P<payload>.*)$")
    records = [
        match.group("payload")
        for line in path.read_text().splitlines()
        if (match := record_pattern.match(line))
    ]
    if not records:
        return None
    try:
        manifest = json.loads(records[-1])
    except json.JSONDecodeError as exc:
        raise RunError(f"Malformed bundle manifest in {path}: invalid JSON ({exc.msg})") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise RunError(f"Malformed bundle manifest in {path}: expected version 1 object")
    primary = manifest.get("primary")
    members = manifest.get("members")
    if not isinstance(primary, dict) or not isinstance(members, list) or not members:
        raise RunError(f"Malformed bundle manifest in {path}: expected primary and 1+ members")
    normalized = []
    seen_ids: set[str] = set()
    seen_identifiers: set[str] = set()
    for item in members:
        if not isinstance(item, dict):
            raise RunError(f"Malformed bundle manifest in {path}: member is not an object")
        identifier = item.get("identifier")
        issue_id = item.get("issue_id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise RunError(f"Malformed bundle manifest in {path}: member identifier is missing")
        if not isinstance(issue_id, str) or not issue_id.strip():
            raise RunError(f"Malformed bundle manifest in {path}: member issue_id is missing")
        member = {"identifier": identifier.strip().upper(), "issue_id": issue_id.strip()}
        identifier_key = member["identifier"].casefold()
        issue_key = member["issue_id"].casefold()
        if identifier_key in seen_identifiers or issue_key in seen_ids:
            raise RunError(
                f"Malformed bundle manifest in {path}: duplicate member {member['identifier']}"
            )
        seen_identifiers.add(identifier_key)
        seen_ids.add(issue_key)
        normalized.append(member)
    normalized_primary = {
        "identifier": str(primary.get("identifier") or "").strip().upper(),
        "issue_id": str(primary.get("issue_id") or "").strip(),
    }
    if normalized_primary != normalized[0]:
        raise RunError(f"Malformed bundle manifest in {path}: primary must be the first member")
    return {"version": 1, "primary": normalized_primary, "members": normalized}


def bundle_identifiers(
    ticket: str,
    primary_identifier: str,
    path: Path | None = None,
    *,
    primary_issue_id: str | None = None,
) -> list[str]:
    """Return canonical bundle identifiers, or the primary for a legacy run."""
    manifest = load_bundle_manifest(ticket, path)
    if manifest is None:
        return [primary_identifier.strip().upper()]
    expected = manifest["primary"]
    if expected["identifier"] != primary_identifier.strip().upper() or (
        primary_issue_id is not None and expected["issue_id"] != primary_issue_id.strip()
    ):
        raise RunError(
            f"Bundle manifest primary {expected['identifier']} ({expected['issue_id']}) does not "
            f"match resolved primary {primary_identifier} ({primary_issue_id or 'id unavailable'})"
        )
    return [member["identifier"] for member in manifest["members"]]


# --------------------------------------------------------------------------- linear (urllib)


def _is_linear_mention_scope_error(errors: object) -> bool:
    text = json.dumps(errors)
    return "App user not valid" in text and "lack the required scope" in text


def linear_gql(query: str, variables: dict) -> dict:
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        raise RunError(LINEAR_ENV_ERROR)
    payload = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        LINEAR_URL,
        data=payload,
        headers={"Authorization": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RunError(f"Linear request failed: {exc}") from exc
    if data.get("errors"):
        if _is_linear_mention_scope_error(data["errors"]):
            raise RunError(LINEAR_MENTION_SCOPE_ERROR)
        raise RunError(f"Linear GraphQL returned errors: {data['errors']}")
    return data.get("data") or {}


ISSUE_QUERY = """
query RunIssue($id: String!) {
  issue(id: $id) {
    id identifier title url completedAt canceledAt
    state { type name }
    team { id key name visibility }
  }
}
"""

WEBHOOKS_QUERY = """
query RunWebhooks($cursor: String) {
  webhooks(first: 100, after: $cursor) {
    nodes {
      id enabled allPublicTeams resourceTypes teamIds
      team { id key name }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

SNAPSHOT_QUERY = """
query RunSnapshot($id: String!, $cursor: String) {
  viewer { id name }
  issue(id: $id) {
    id identifier
    comments(first: 100, after: $cursor) {
      nodes { id body url createdAt parent { id } user { id name } }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def resolve_issue(ticket: str) -> dict:
    issue = linear_gql(ISSUE_QUERY, {"id": ticket}).get("issue")
    if not issue:
        raise RunError(f"Linear issue {ticket} was not found")
    return issue


def linear_webhooks() -> list[dict]:
    variables: dict = {}
    webhooks: list[dict] = []
    while True:
        connection = linear_gql(WEBHOOKS_QUERY, variables).get("webhooks") or {}
        webhooks.extend(node for node in connection.get("nodes") or [] if isinstance(node, dict))
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return webhooks
        cursor = page.get("endCursor")
        if not cursor:
            raise RunError("Linear webhook pagination returned no end cursor")
        variables = {"cursor": cursor}


def webhook_covers_team(webhook: dict, team_id: str, team_visibility: str) -> bool:
    if webhook.get("enabled") is not True or "Comment" not in (webhook.get("resourceTypes") or []):
        return False
    team = webhook.get("team") or {}
    if team.get("id") == team_id or team_id in (webhook.get("teamIds") or []):
        return True
    return webhook.get("allPublicTeams") is True and team_visibility == "public"


def require_webhook_coverage(issue: dict) -> None:
    team = issue.get("team") or {}
    team_id = str(team.get("id") or "")
    team_name = str(team.get("key") or team.get("name") or "unknown")
    team_visibility = str(team.get("visibility") or "")
    if not team_id:
        raise RunError(
            f"Linear issue {issue.get('identifier') or issue.get('id')} returned no team, "
            "so webhook coverage cannot be checked."
        )
    try:
        webhooks = linear_webhooks()
    except RunError as exc:
        raise RunError(
            f"Could not read Linear webhook configuration for team {team_name}. "
            "Use a workspace-admin LINEAR_API_KEY (or OAuth token with admin scope), then retry. "
            f"Linear error: {exc}"
        ) from exc
    if not any(webhook_covers_team(webhook, team_id, team_visibility) for webhook in webhooks):
        if team_visibility == "public":
            action = "Provision a workspace webhook with allPublicTeams=true"
        else:
            action = (
                f"Provision an enabled Comment webhook explicitly scoped to team {team_name}; "
                "allPublicTeams covers public teams only"
            )
        raise RunError(
            f"No enabled Linear Comment webhook covers team {team_name}. "
            f"{action} before dispatching this ticket."
        )


def resolve_bundle(primary_ticket: str, included_tickets: list[str]) -> list[dict]:
    """Resolve and canonicalize every member, refusing aliases of the same issue."""
    issues = []
    for requested in [primary_ticket, *included_tickets]:
        issue = resolve_issue(requested)
        identifier = str(issue.get("identifier") or "").strip().upper()
        issue_id = str(issue.get("id") or "").strip()
        if not identifier or not issue_id:
            raise RunError(f"Linear issue {requested} returned no canonical identifier or id")
        issues.append({**issue, "identifier": identifier, "id": issue_id})
    seen_ids: dict[str, str] = {}
    seen_identifiers: dict[str, str] = {}
    for issue in issues:
        identifier = issue["identifier"]
        issue_id = issue["id"]
        duplicate = seen_ids.get(issue_id.casefold()) or seen_identifiers.get(identifier.casefold())
        if duplicate is not None:
            raise RunError(
                f"Duplicate bundle member {identifier}: it repeats {duplicate}. "
                "List each Linear issue exactly once and do not include the primary again."
            )
        seen_ids[issue_id.casefold()] = identifier
        seen_identifiers[identifier.casefold()] = identifier
    return issues


def linear_snapshot(issue_id: str) -> dict:
    """Viewer identity plus every comment (paginated), stdlib transport."""
    variables: dict = {"id": issue_id}
    comments: list[dict] = []
    viewer: dict = {}
    while True:
        data = linear_gql(SNAPSHOT_QUERY, variables)
        issue = data.get("issue")
        if not issue:
            raise RunError(f"Linear issue {issue_id} was not returned")
        viewer = viewer or (data.get("viewer") or {})
        connection = issue.get("comments") or {}
        comments.extend(node for node in connection.get("nodes") or [] if isinstance(node, dict))
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return {"viewer": viewer, "comments": comments}
        variables = {"id": issue_id, "cursor": page.get("endCursor")}


def post_comment(issue_id: str, body: str) -> dict:
    mutation = """
    mutation RunComment($input: CommentCreateInput!) {
      commentCreate(input: $input) { success comment { id } }
    }
    """
    result = linear_gql(mutation, {"input": {"issueId": issue_id, "body": body}})
    created = result.get("commentCreate") or {}
    comment = created.get("comment") or {}
    if created.get("success") is not True or not comment.get("id"):
        raise RunError("Linear comment was not accepted")
    return comment


def find_dispatch_rejection(issue_id: str, parent_comment_id: str) -> dict | None:
    for comment in reversed(linear_snapshot(issue_id)["comments"]):
        parent = comment.get("parent") or {}
        body = str(comment.get("body") or "")
        if parent.get("id") != parent_comment_id or not body.startswith(LINEAR_AGENT_ERROR_PREFIX):
            continue
        return {
            "reason": body[len(LINEAR_AGENT_ERROR_PREFIX) :].strip(),
            "url": str(comment.get("url") or ""),
        }
    return None


# --------------------------------------------------------------------------- environment


def probe_langgraph(url: str) -> bool:
    for _ in range(2):
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/ok", timeout=3) as response:
                return bool(json.loads(response.read()).get("ok"))
        except Exception:
            pass
    return False


def resolve_langgraph_endpoint(*, exclude: set[str] | None = None) -> tuple[str, str] | None:
    excluded = exclude or set()
    candidates: list[tuple[str, str]] = []
    configured = os.environ.get("LANGGRAPH_URL")
    if configured:
        candidates.append((configured.rstrip("/"), "environment"))
    candidates.extend(LOCAL_LANGGRAPH_ENDPOINTS)
    seen: set[str] = set()
    for url, provenance in candidates:
        if url in seen or url in excluded:
            continue
        seen.add(url)
        if probe_langgraph(url):
            return url, provenance
    return None


def gh_auth_token() -> str:
    if not shutil.which("gh"):
        return ""
    probe = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
    return probe.stdout.strip() if probe.returncode == 0 else ""


def skill_checkout_warning() -> str | None:
    root_probe = subprocess.run(
        ["git", "-C", str(SKILL_DIR), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if root_probe.returncode != 0:
        return None
    root = root_probe.stdout.strip()
    refresh_failed = False
    try:
        refresh = subprocess.run(
            ["git", "-C", root, "fetch", "--quiet", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        refresh_failed = refresh.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        refresh_failed = True
    upstream_probe = subprocess.run(
        ["git", "-C", root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if upstream_probe.returncode != 0:
        return None
    upstream = upstream_probe.stdout.strip()
    behind_probe = subprocess.run(
        ["git", "-C", root, "rev-list", "--count", f"HEAD..{upstream}"],
        capture_output=True,
        text=True,
        check=False,
    )
    behind = int(behind_probe.stdout.strip()) if behind_probe.returncode == 0 else 0
    if behind:
        stale = f"; origin refresh failed, using stale {upstream}" if refresh_failed else ""
        return (
            f"skill checkout is {behind} commit{'s' if behind != 1 else ''} behind {upstream}{stale}. "
            f"Update it with: git -C {root} pull"
        )
    if refresh_failed:
        return f"origin refresh failed; stale {upstream} comparison found no known skill drift"
    return None


def ensure_env(ticket: str, *, langgraph: bool, github: bool) -> list[str]:
    """Fail fast with copy-pasteable exports; auto-derive what is safely derivable."""
    notes: list[str] = []
    if not os.environ.get("LINEAR_API_KEY"):
        raise RunError(LINEAR_ENV_ERROR)
    if langgraph:
        configured = os.environ.get("LANGGRAPH_URL")
        configured_url = configured.rstrip("/") if configured else None
        endpoint = resolve_langgraph_endpoint()
        if endpoint is None:
            raise RunError(LANGGRAPH_ENV_ERROR)
        url, provenance = endpoint
        os.environ["LANGGRAPH_URL"] = url
        if url != configured_url:
            action = "failed over" if configured else "auto-set"
            notes.append(f"{action} LANGGRAPH_URL={url} ({provenance}; /ok probe passed)")
    if github and not os.environ.get("GH_TOKEN"):
        token = gh_auth_token()
        if token:
            os.environ["GH_TOKEN"] = token
            notes.append("auto-derived GH_TOKEN from `gh auth token`")
        else:
            raise RunError(GH_ENV_ERROR)
    for note in notes:
        dogfood(ticket, "note", note)
    return notes


def _monitor_python_error(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            [*command, "-c", "import httpx, langgraph_sdk"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if result.returncode == 0:
        return None
    return (
        result.stderr.strip()[-400:]
        or result.stdout.strip()[-400:]
        or (f"probe exited {result.returncode}")
    )


def resolve_monitor_python() -> list[str]:
    """Interpreter for the vendored wave-monitor (needs httpx + langgraph_sdk)."""
    override = os.environ.get("OPENSWE_RUN_PYTHON")
    if override:
        try:
            command = [str(Path(override).expanduser())]
        except RuntimeError as exc:
            raise RunError(f"OPENSWE_RUN_PYTHON is invalid: {exc}") from exc
        if error := _monitor_python_error(command):
            raise RunError(
                "OPENSWE_RUN_PYTHON does not provide httpx + langgraph_sdk: "
                f"{error}. Set it to one Python executable path with those modules installed."
            )
        return command
    control_plane = [CONTROL_PLANE_PYTHON]
    if Path(CONTROL_PLANE_PYTHON).exists() and not _monitor_python_error(control_plane):
        return control_plane
    if shutil.which("uv"):
        # --no-project: this runs from the target repo checkout, and without it uv
        # resolves that repo's pyproject first and fails before installing these.
        uv_command = [
            "uv",
            "run",
            "--no-project",
            "--with",
            "httpx",
            "--with",
            "langgraph-sdk",
            "python",
        ]
        if not _monitor_python_error(uv_command):
            return uv_command
    raise RunError(
        "No interpreter with httpx + langgraph_sdk. Fix one of: "
        f"export OPENSWE_RUN_PYTHON=<python-with-deps>; install the control plane venv "
        f"({CONTROL_PLANE_PYTHON}); or install uv (brew install uv)."
    )


# Statuses the product refuses to transition (plan_api._reject_shared_content, and
# its 409 on editing a settled plan). Mirrored here so this path is no more
# permissive than the dashboard's.
PLAN_STATUS_LOCKED = ("shared", "cancelled")
PLAN_RESULT_SENTINEL = "OPENSWE_PLAN_RESULT "

PLAN_STATUS_SNIPPET = r"""
import asyncio, json, os
from langgraph_sdk import get_client

NAMESPACE = ("plan", "content")
SENTINEL = "OPENSWE_PLAN_RESULT "
LOCKED = ("shared", "cancelled")
THREAD = os.environ["OPENSWE_PLAN_THREAD"]
STATUS = os.environ["OPENSWE_PLAN_STATUS"]
PLAN_MODE = os.environ["OPENSWE_PLAN_MODE"] == "1"
URL = os.environ.get("LANGGRAPH_URL") or "http://127.0.0.1:2029"


def _value(item):
    return (item.get("value") if isinstance(item, dict) else item) or {}


class Refused(Exception):
    pass


async def main():
    client = get_client(url=URL)
    # A missing item comes back as HTTP 200 with a null body, not a 404, so an
    # absent plan is indistinguishable from an empty one here — refuse both
    # rather than fabricate a record the product never stored.
    existing = _value(await client.store.get_item(NAMESPACE, THREAD))
    previous = existing.get("status")
    if not existing or not str(existing.get("markdown") or "").strip():
        raise Refused(
            "no plan record with content is stored for this thread; the agent may "
            "have posted its plan to Linear without landing a save_plan write"
        )
    if previous in LOCKED:
        raise Refused(f"refusing to transition a {previous!r} plan")
    record = {"markdown": existing["markdown"], "status": STATUS}
    plan_file_path = existing.get("plan_file_path")
    if isinstance(plan_file_path, str) and plan_file_path:
        record["plan_file_path"] = plan_file_path
    await client.store.put_item(NAMESPACE, THREAD, record)
    confirmed = _value(await client.store.get_item(NAMESPACE, THREAD)).get("status")
    # Metadata mirrors the record for the dashboard's benefit; the product treats
    # this as best-effort (plan_store._merge_thread_metadata swallows it), and the
    # record above is what decides eligibility. Never fail the write over it.
    metadata_ok = True
    try:
        await client.threads.update(
            thread_id=THREAD, metadata={"plan_status": STATUS, "plan_mode": PLAN_MODE}
        )
    except Exception:
        metadata_ok = False
    print(SENTINEL + json.dumps(
        {"previous": previous, "status": confirmed, "metadata_ok": metadata_ok}
    ))


try:
    asyncio.run(main())
except Refused as exc:
    print(SENTINEL + json.dumps({"refused": str(exc)}))
    raise SystemExit(3)
"""


def set_plan_status(thread_id: str, status: str, *, plan_mode: bool) -> dict:
    """Transition the product's plan record, as the dashboard's endpoints do.

    An @openswe comment makes the agent act but never transitions this record,
    and the product reads it to decide `auto_merge_eligible`. Left untransitioned,
    the run is computed merge-ineligible: the PR opens as a draft and auto-merge
    is never armed. This writes what `set_plan_status(...)` writes in the product.

    Callers must do this before the comment that dispatches: eligibility is
    resolved at run creation (server.py `_auto_merge_eligible`) and re-checked
    when the PR is opened (`open_pull_request`), so a later write is too late.
    """
    command = [*resolve_monitor_python(), "-c", PLAN_STATUS_SNIPPET]
    environ = {
        **os.environ,
        "OPENSWE_PLAN_THREAD": thread_id,
        "OPENSWE_PLAN_STATUS": status,
        "OPENSWE_PLAN_MODE": "1" if plan_mode else "0",
    }
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=environ, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise RunError(
            f"Plan-status write to {status!r} timed out against thread {thread_id}. "
            "It may still have landed — re-read the plan before retrying."
        ) from exc
    payload = {}
    for line in reversed(result.stdout.splitlines()):
        if not line.startswith(PLAN_RESULT_SENTINEL):
            continue
        candidate = json.loads(line[len(PLAN_RESULT_SENTINEL) :])
        if isinstance(candidate, dict):
            payload = candidate
            break
    if payload.get("refused"):
        raise RunError(f"Plan store refused the {status!r} transition: {payload['refused']}")
    if result.returncode != 0:
        raise RunError(
            f"Could not set the plan record to {status!r} on thread {thread_id}, so the "
            "run would be computed merge-ineligible. Refusing to continue. "
            f"Error: {result.stderr.strip()[-400:]}"
        )
    if payload.get("status") != status:
        raise RunError(
            f"Plan store did not accept the {status!r} transition for thread "
            f"{thread_id} (status is {payload.get('status')!r})."
        )
    return payload


SIBLING_WAVE_SCRIPTS = SKILL_DIR.parent / "openswe-wave" / "scripts"


def wave_scripts_dir() -> Path:
    """Where the wave scripts live: beside us if copied there, else the sibling skill.

    The supported deployment is a git checkout with the skill directories
    symlinked into ~/.claude/skills, so the sibling resolution is the normal
    path; a stray copied layout still works via the first branch.
    """
    if (SCRIPTS_DIR / "openswe_wave.py").is_file():
        return SCRIPTS_DIR
    if (SIBLING_WAVE_SCRIPTS / "openswe_wave.py").is_file():
        return SIBLING_WAVE_SCRIPTS
    raise RunError(
        "openswe_wave.py is neither beside this script nor present at "
        f"{SIBLING_WAVE_SCRIPTS}. Deploy from a git checkout of the repo so the "
        "sibling openswe-wave skill is present (see SKILL.md, Deployment)."
    )


def import_wave_module():
    """Import openswe_wave (pure classifiers only; lazy net deps)."""
    sys.path.insert(0, str(wave_scripts_dir()))
    try:
        import openswe_wave  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise RunError(
            f"openswe_wave.py was found under {wave_scripts_dir()} but could not be imported: {exc}"
        ) from exc
    return openswe_wave


# --------------------------------------------------------------------------- guards


def read_body(args: argparse.Namespace) -> str:
    if getattr(args, "body_file", None):
        text = sys.stdin.read() if args.body_file == "-" else Path(args.body_file).read_text()
    else:
        raise RunError("Provide --body-file <path> (or --body-file - to read stdin)")
    if not text.strip():
        raise RunError("Comment body is empty")
    return text


def guard_body_hygiene(body: str) -> None:
    first_line = body.splitlines()[0] if body.splitlines() else ""
    mentions = list(DIRECTIVE_MENTION_RE.finditer(body))
    if not re.match(r"^@openswe(?:\s|$)", first_line, flags=re.IGNORECASE) or len(mentions) != 1:
        raise RunError(
            "Body must begin exactly with one @openswe mention on the first line; "
            "later or duplicate mentions are refused."
        )
    directives = list(REPO_DIRECTIVE_RE.finditer(body))
    allowed = re.match(
        r"^@openswe (repo(?:\s+[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))\b",
        body,
        flags=re.IGNORECASE,
    )
    allowed_span = allowed.span(1) if allowed else None
    if any(match.span() != allowed_span for match in directives):
        raise RunError(
            "A repository directive is allowed only immediately after the first-line "
            "@openswe mention as `repo owner/name` or `repo:owner/name`."
        )


def guard_start_repo_directive(body: str, repo: str) -> None:
    match = re.match(
        r"^@openswe repo(?:\s+|:)(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b",
        body,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise RunError(
            "A custom start body must specify the resolved repository immediately after "
            "@openswe as `repo owner/name` or `repo:owner/name`."
        )
    body_repo = match.group("repo")
    if body_repo.casefold() != repo.casefold():
        raise RunError(
            f"Custom start body repository {body_repo!r} does not match --repo {repo!r}."
        )


def guard_placeholders(ticket: str, body: str, force: bool) -> None:
    body_without_code = INLINE_CODE_RE.sub("", FENCED_CODE_RE.sub("", body))
    leftovers = sorted(set(PLACEHOLDER_RE.findall(body_without_code)))
    if leftovers and not force:
        raise RunError(
            f"Body still contains unfilled template placeholders: {', '.join(leftovers)}. "
            "Fill them in, or pass --force if they are intentional."
        )
    if leftovers and force:
        dogfood(ticket, "note", f"--force posted body with placeholders: {', '.join(leftovers)}")


def guard_bundle_membership(body: str, identifiers: list[str]) -> None:
    """Require every canonical bundle identifier in a custom or approval body."""
    missing = [
        identifier
        for identifier in identifiers
        if re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(identifier)}(?![A-Za-z0-9_-])",
            body,
            flags=re.IGNORECASE,
        )
        is None
    ]
    if missing:
        raise RunError(
            "Bundle body is missing member identifier(s): "
            f"{', '.join(missing)}. Name every bundle member explicitly."
        )


def has_closing_line(body: object, identifier: str) -> bool:
    """Return whether a PR body has the standalone closing line."""
    if not isinstance(body, str):
        return False
    return (
        re.search(
            rf"^[ \t]*Closes[ \t]+{re.escape(identifier)}[ \t]*$",
            body,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        is not None
    )


def guard_terminal_issue(ticket: str, issue: dict, force: bool) -> None:
    state = issue.get("state") or {}
    state_type = str(state.get("type") or "").lower()
    timestamp_field = {"completed": "completedAt", "canceled": "canceledAt"}.get(state_type)
    if timestamp_field is None:
        return
    state_name = str(state.get("name") or state_type)
    timestamp = issue.get(timestamp_field)
    detail = f"state {state_name!r} (type {state_type!r})"
    if timestamp:
        detail += f"; completion evidence: {timestamp_field}={timestamp}"
    if force:
        dogfood(
            ticket,
            "cmd",
            f"--force overriding terminal Linear issue {issue['identifier']}: {detail} before dispatch",
        )
        return
    dogfood(ticket, "error", f"refused dispatch for {issue['identifier']}: {detail}")
    raise RunError(
        f"Refusing to dispatch {issue['identifier']}: Linear {detail}. "
        "Pass --force to override this terminal-state guard."
    )


HANDOFF_BASELINE_SENTINEL = "OPENSWE_HANDOFF_BASELINE "
HANDOFF_RESULT_SENTINEL = "OPENSWE_HANDOFF_RESULT "
HANDOFF_MONITOR_SNIPPET = r"""
import asyncio, json, os, sys, time
from langgraph_sdk import get_client

BASELINE_SENTINEL = "OPENSWE_HANDOFF_BASELINE "
RESULT_SENTINEL = "OPENSWE_HANDOFF_RESULT "
THREAD = os.environ["OPENSWE_HANDOFF_THREAD"]
ACTION = os.environ["OPENSWE_HANDOFF_ACTION"]
TICKET = os.environ["OPENSWE_HANDOFF_TICKET"]
PLAN_CONTEXT = json.loads(os.environ.get("OPENSWE_HANDOFF_PLAN_CONTEXT") or "null")
TIMEOUT_SECONDS = float(os.environ["OPENSWE_HANDOFF_TIMEOUT"])
POLL_INTERVAL_SECONDS = float(os.environ["OPENSWE_HANDOFF_POLL_INTERVAL"])
URL = os.environ.get("LANGGRAPH_URL") or "http://127.0.0.1:2029"


def missing(exc):
    response = getattr(exc, "response", None)
    return (
        getattr(exc, "status_code", None) == 404
        or getattr(response, "status_code", None) == 404
        or "404" in str(exc)
    )


async def snapshot(client):
    try:
        thread = await client.threads.get(THREAD)
    except Exception as exc:
        if missing(exc):
            return {"thread_status": "missing", "run_ids": []}
        raise
    if not thread:
        return {"thread_status": "missing", "run_ids": []}
    runs = await client.runs.list(THREAD, limit=100)
    run_ids = sorted(
        str(run.get("run_id") or run.get("id"))
        for run in runs
        if isinstance(run, dict) and (run.get("run_id") or run.get("id"))
    )
    return {
        "thread_status": str(thread.get("status") or "missing"),
        "run_ids": run_ids,
    }


def observed(baseline, current):
    new_run = bool(set(current["run_ids"]) - set(baseline["run_ids"]))
    if baseline["thread_status"] == "busy":
        return new_run
    return new_run or current["thread_status"] == "busy"


async def main():
    client = get_client(url=URL)
    baseline = await snapshot(client)
    print(BASELINE_SENTINEL + json.dumps(baseline), flush=True)
    sys.stdin.readline()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    final = baseline
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            async with asyncio.timeout(remaining):
                final = await snapshot(client)
        except Exception as exc:
            final = {"error": str(exc)}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if "error" not in final and observed(baseline, final):
            print(RESULT_SENTINEL + json.dumps({"handoff": final}), flush=True)
            return
        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))
    evidence = {
        "action": ACTION,
        "ticket": TICKET,
        "thread_id": THREAD,
        "baseline": baseline,
        "final": final,
        "timeout_seconds": TIMEOUT_SECONDS,
    }
    if PLAN_CONTEXT is not None:
        evidence["plan_status_nontransactional"] = PLAN_CONTEXT
    print(
        RESULT_SENTINEL
        + json.dumps(
            {"error": "LangGraph handoff timeout: " + json.dumps(evidence, sort_keys=True)}
        ),
        flush=True,
    )


asyncio.run(main())
"""


def _stop_handoff_process(process: subprocess.Popen) -> tuple[str, str]:
    if process.poll() is None:
        process.terminate()
    stdout, stderr = process.communicate()
    return stdout, stderr


def _start_handoff_process(
    action: str,
    ticket: str,
    thread_id: str,
    *,
    plan_context: dict | None = None,
) -> subprocess.Popen:
    command = [*resolve_monitor_python(), "-c", HANDOFF_MONITOR_SNIPPET]
    environ = {
        **os.environ,
        "OPENSWE_HANDOFF_THREAD": thread_id,
        "OPENSWE_HANDOFF_ACTION": action,
        "OPENSWE_HANDOFF_TICKET": ticket.upper(),
        "OPENSWE_HANDOFF_TIMEOUT": str(HANDOFF_TIMEOUT_SECONDS),
        "OPENSWE_HANDOFF_POLL_INTERVAL": str(HANDOFF_POLL_INTERVAL_SECONDS),
        "OPENSWE_HANDOFF_PLAN_CONTEXT": json.dumps(plan_context),
    }
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=environ,
    )
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], HANDOFF_SNAPSHOT_TIMEOUT_SECONDS)
    if not ready:
        stdout, stderr = _stop_handoff_process(process)
        detail = stderr.strip()[-400:] or stdout.strip()[-400:]
        raise RunError(
            f"LangGraph handoff baseline timed out for thread {thread_id}"
            + (f": {detail}" if detail else "")
        )
    line = process.stdout.readline().strip()
    if not line.startswith(HANDOFF_BASELINE_SENTINEL):
        stdout, stderr = _stop_handoff_process(process)
        detail = stderr.strip()[-400:] or "\n".join((line, stdout.strip()))[-400:]
        raise RunError(f"LangGraph handoff baseline failed for thread {thread_id}: {detail}")
    return process


def _dispatch_rejection_message(issue_id: str, parent_comment_id: str) -> str | None:
    rejection = find_dispatch_rejection(issue_id, parent_comment_id)
    if rejection is None:
        return None
    return "Open SWE dispatch rejected: {} ({})".format(rejection["reason"], rejection["url"])


def _raise_if_dispatch_rejected(
    process: subprocess.Popen,
    issue_id: str | None,
    parent_comment_id: str | None,
) -> None:
    if issue_id is None or parent_comment_id is None:
        return
    try:
        rejection = _dispatch_rejection_message(issue_id, parent_comment_id)
    except Exception:
        _stop_handoff_process(process)
        raise
    if rejection is not None:
        _stop_handoff_process(process)
        raise RunError(rejection)


def _await_handoff(
    process: subprocess.Popen,
    thread_id: str,
    *,
    issue_id: str | None = None,
    parent_comment_id: str | None = None,
) -> dict:
    deadline = time.monotonic() + HANDOFF_TIMEOUT_SECONDS + 10
    while True:
        _raise_if_dispatch_rejected(process, issue_id, parent_comment_id)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _raise_if_dispatch_rejected(process, issue_id, parent_comment_id)
            _stop_handoff_process(process)
            raise RunError(f"LangGraph handoff monitor overran for thread {thread_id}")
        try:
            stdout, stderr = process.communicate(
                timeout=min(HANDOFF_POLL_INTERVAL_SECONDS, remaining)
            )
            break
        except subprocess.TimeoutExpired:
            continue
    payload = None
    for line in reversed(stdout.splitlines()):
        if not line.startswith(HANDOFF_RESULT_SENTINEL):
            continue
        payload = json.loads(line[len(HANDOFF_RESULT_SENTINEL) :])
        break
    if not isinstance(payload, dict):
        detail = stderr.strip()[-400:] or stdout.strip()[-400:]
        raise RunError(f"LangGraph handoff monitor failed for thread {thread_id}: {detail}")
    _raise_if_dispatch_rejected(process, issue_id, parent_comment_id)
    if payload.get("error"):
        raise RunError(str(payload["error"]))
    final = payload.get("handoff")
    if process.returncode != 0 or not isinstance(final, dict):
        detail = stderr.strip()[-400:] or stdout.strip()[-400:]
        raise RunError(f"LangGraph handoff monitor failed for thread {thread_id}: {detail}")
    return final


def _post_with_handoff(
    action: str,
    ticket: str,
    issue_id: str,
    body: str,
    thread_id: str,
    *,
    plan_context: dict | None = None,
) -> dict:
    process = _start_handoff_process(action, ticket, thread_id, plan_context=plan_context)
    try:
        comment = post_comment(issue_id, body)
    except Exception:
        _stop_handoff_process(process)
        raise
    try:
        assert process.stdin is not None
        process.stdin.write("posted\n")
        process.stdin.flush()
    except Exception as exc:
        stdout, stderr = _stop_handoff_process(process)
        detail = stderr.strip()[-400:] or stdout.strip()[-400:]
        raise RunError(
            f"Linear comment was posted, but the LangGraph handoff monitor failed "
            f"before polling thread {thread_id}: {detail}"
        ) from exc
    return _await_handoff(
        process,
        thread_id,
        issue_id=issue_id,
        parent_comment_id=str(comment["id"]),
    )


# --------------------------------------------------------------------------- commands


def cmd_env(args: argparse.Namespace) -> int:
    endpoint = resolve_langgraph_endpoint()
    report = {
        "linear_api_key": bool(os.environ.get("LINEAR_API_KEY")),
        "langgraph_url": endpoint[0] if endpoint else None,
        "langgraph_url_provenance": endpoint[1] if endpoint else None,
        "gh_token": bool(os.environ.get("GH_TOKEN")) or bool(gh_auth_token()),
        "stable_root": str(stable_root()),
        "handoffs": str(ensure_handoffs()),
    }
    try:
        report["monitor_python"] = " ".join(resolve_monitor_python())
    except RunError as exc:
        report["monitor_python"] = f"UNRESOLVED: {exc}"
    missing = [k for k in ("linear_api_key", "langgraph_url", "gh_token") if not report[k]]
    report["ready"] = not missing and not str(report["monitor_python"]).startswith("UNRESOLVED")
    print(json.dumps(report, indent=2))
    errors = {
        "linear_api_key": LINEAR_ENV_ERROR,
        "langgraph_url": LANGGRAPH_ENV_ERROR,
        "gh_token": GH_ENV_ERROR,
    }
    for key in missing:
        print(f"openswe-run: {errors[key]}", file=sys.stderr)
    warning = skill_checkout_warning()
    if warning:
        print(f"openswe-run: warning: {warning}", file=sys.stderr)
    return 0 if report["ready"] else 2


def cmd_start(args: argparse.Namespace) -> int:
    if not args.dry_run:
        log_path(args.ticket, new_run=True)
    ensure_env(args.ticket, langgraph=not args.dry_run, github=False)
    issues = resolve_bundle(args.ticket, list(getattr(args, "include_ticket", []) or []))
    primary = issues[0]
    identifiers = [issue["identifier"] for issue in issues]
    is_bundle = len(issues) > 1
    if args.body_file:
        body = read_body(args)
    elif is_bundle:
        body = BUNDLE_DISPATCH_TEMPLATE.format(
            repo=args.repo,
            primary=primary["identifier"],
            included=", ".join(identifiers[1:]),
            members=", ".join(identifiers),
            ref=args.ref,
            scope=(
                args.scope or f"execute {', '.join(identifiers)} exactly as one ticketed bundle"
            ).removesuffix("."),
            boundaries=(
                args.boundaries
                or f"no changes beyond the combined stated scope of {', '.join(identifiers)}"
            ).removesuffix("."),
            verify=(
                args.verify
                or "focused tests plus the repository's own lint and typecheck gates; name the exact commands in the plan"
            ).removesuffix("."),
            closing_lines="\n".join(f"Closes {identifier}" for identifier in identifiers),
        )
    else:
        body = DISPATCH_TEMPLATE.format(
            repo=args.repo,
            ticket=primary["identifier"],
            ref=args.ref,
            scope=(
                args.scope or f"execute {primary['identifier']} exactly as ticketed"
            ).removesuffix("."),
            boundaries=(
                args.boundaries or f"no changes beyond {primary['identifier']}'s stated scope"
            ).removesuffix("."),
            verify=(
                args.verify
                or "focused tests plus the repository's own lint and typecheck gates; name the exact commands in the plan"
            ).removesuffix("."),
        )
    guard_body_hygiene(body)
    if args.body_file:
        guard_start_repo_directive(body, args.repo)
    guard_placeholders(args.ticket, body, False if is_bundle else args.force)
    if is_bundle:
        guard_bundle_membership(body, identifiers)
    thread_id = import_wave_module().derive_linear_thread_id(primary["id"])
    result = {
        "identifier": primary["identifier"],
        "issue_id": primary["id"],
        "issue_url": primary["url"],
        "issue_state": primary.get("state"),
        "thread_id": thread_id,
        "repo": args.repo,
        "dispatched": not args.dry_run,
    }
    if is_bundle:
        result["bundle"] = identifiers
    if args.dry_run:
        result["body"] = body
        print(json.dumps(result, indent=2))
        return 0
    for issue in issues:
        guard_terminal_issue(args.ticket, issue, args.force)
    require_webhook_coverage(primary)
    write_bundle_manifest(args.ticket, issues)
    result["handoff"] = _post_with_handoff("start", args.ticket, primary["id"], body, thread_id)
    handoff = result["handoff"]
    suffix = f"; bundle={','.join(identifiers)}" if is_bundle else ""
    dogfood(
        args.ticket,
        "cmd",
        f"dispatched {primary['identifier']} to {args.repo} ({primary['url']}){suffix}; handoff "
        f"status={handoff.get('thread_status')} runs={len(handoff.get('run_ids') or [])}",
    )
    print(json.dumps(result))
    return 0


def _spawn_monitor(args: argparse.Namespace, issue_id: str) -> tuple[subprocess.Popen, deque]:
    command = [
        *resolve_monitor_python(),
        str(wave_scripts_dir() / "wave-monitor"),
        "watch",
        "--issue-id",
        issue_id,
        "--repo",
        args.repo,
        "--interval",
        str(args.interval),
        "--known-ids-file",
        str(known_ids_path(args.ticket)),
    ]
    if not args.follow:
        command.append("--until-wake")
    ready_file = getattr(args, "ready_file", None)
    if ready_file:
        command.extend(["--ready-file", str(ready_file)])
    if args.pr_number:
        command.extend(["--pr-number", str(args.pr_number)])
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )
    stderr_tail: deque = deque(maxlen=40)
    threading.Thread(
        target=lambda: [stderr_tail.append(line) for line in process.stderr], daemon=True
    ).start()
    return process, stderr_tail


def _emit_wake(ticket: str, wake: dict, source: str) -> None:
    dogfood(
        ticket, "wake", f"{wake.get('wake_node')} via {source}: {wake.get('summary', '')[:200]}"
    )
    print(json.dumps(wake, sort_keys=True, default=str), flush=True)


def watch_timeout_min(args: argparse.Namespace) -> float:
    return args.timeout_min if args.timeout_min is not None else PHASE_TIMEOUT_MINUTES[args.phase]


def _recover_langgraph_endpoint(ticket: str, issue: dict, issue_id: str) -> bool | None:
    failed_url = os.environ["LANGGRAPH_URL"]
    if probe_langgraph(failed_url):
        return None
    replacement = resolve_langgraph_endpoint(exclude={failed_url})
    if replacement is None:
        wake = {
            "wake_node": "endpoint_unavailable",
            "summary": (
                f"LangGraph endpoint {failed_url} failed; no supported healthy "
                "replacement was found"
            ),
            "evidence": {
                "failed_endpoint": failed_url,
                "replacement_endpoint": None,
                "issue_id": issue_id,
                "identifier": issue["identifier"],
            },
        }
        _emit_wake(ticket, wake, "wrapper-endpoint-check")
        return False
    replacement_url, provenance = replacement
    os.environ["LANGGRAPH_URL"] = replacement_url
    wake = {
        "wake_node": "endpoint_failover",
        "summary": (
            f"LangGraph endpoint {failed_url} failed; continuing on "
            f"{replacement_url} ({provenance})"
        ),
        "evidence": {
            "failed_endpoint": failed_url,
            "replacement_endpoint": replacement_url,
            "replacement_provenance": provenance,
            "issue_id": issue_id,
            "identifier": issue["identifier"],
        },
    }
    _emit_wake(ticket, wake, "wrapper-endpoint-check")
    return True


def _cmd_watch(args: argparse.Namespace) -> int:
    """Exit-on-first-wake watch. Healthy monitors are silent; wakes are one JSON line."""
    ensure_env(args.ticket, langgraph=True, github=True)
    issue = resolve_issue(args.ticket)
    bundle_identifiers(
        args.ticket,
        str(issue.get("identifier") or args.ticket),
        primary_issue_id=str(issue["id"]),
    )
    issue_id = issue["id"]
    timeout_min = watch_timeout_min(args)
    watermark = known_ids_path(args.ticket)
    dogfood(
        args.ticket,
        "cmd",
        f"watch started (phase {args.phase}, interval {args.interval}s, timeout {timeout_min}m, "
        f"watermark {watermark})",
    )
    process, stderr_tail = _spawn_monitor(args, issue_id)
    ready_file = getattr(args, "ready_file", None)
    managed_state_file = getattr(args, "managed_state_file", None)
    if ready_file and managed_state_file:
        readiness_deadline = time.monotonic() + args.interval + 5
        ready_path = Path(ready_file)
        while time.monotonic() < readiness_deadline:
            if ready_path.exists():
                state = _read_json_object(Path(managed_state_file)) or {}
                state.update({"status": "ready", "ready_at": now_iso()})
                _atomic_write_json(Path(managed_state_file), state)
                break
            if process.poll() is not None:
                raise RunError("wave-monitor exited before completing its live baseline")
            time.sleep(0.1)
        else:
            process.terminate()
            raise RunError(
                "wave-monitor did not verify a live baseline before the readiness deadline"
            )
    deadline = time.monotonic() + timeout_min * 60
    restarts = 0
    buffered = ""
    while True:
        if time.monotonic() >= deadline:
            process.terminate()
            wake = {
                "wake_node": "watch_timeout",
                "summary": (f"no {args.phase} wake within {timeout_min} minutes; monitor stopped"),
                "evidence": {
                    "issue_id": issue_id,
                    "identifier": issue["identifier"],
                    "phase": args.phase,
                    "timeout_min": timeout_min,
                },
            }
            _emit_wake(args.ticket, wake, "wrapper-timeout")
            return WAKE_TIMEOUT_EXIT

        ready, _, _ = select.select([process.stdout], [], [], 5)
        if ready:
            line = process.stdout.readline()
            if line:
                buffered = line.strip()
                try:
                    wake = json.loads(buffered)
                except json.JSONDecodeError:
                    continue
                if isinstance(wake, dict) and wake.get("wake_node"):
                    summary = str(wake.get("summary") or "")
                    if (
                        wake["wake_node"] == "unhandled_condition"
                        and summary.startswith("wave monitor poll failed:")
                        and "LANGGRAPH_URL request" in summary
                    ):
                        recovered = _recover_langgraph_endpoint(args.ticket, issue, issue_id)
                        if recovered is not None:
                            process.terminate()
                            if not recovered:
                                return CHILD_FAILURE_EXIT
                            process, stderr_tail = _spawn_monitor(args, issue_id)
                            continue
                    _emit_wake(args.ticket, wake, "wave-monitor")
                    if not args.follow:
                        process.terminate()
                        return 0

        if process.poll() is not None:
            tail = "".join(list(stderr_tail)[-8:]).strip()
            recovered = _recover_langgraph_endpoint(args.ticket, issue, issue_id)
            if recovered is False:
                return CHILD_FAILURE_EXIT
            if recovered is True:
                process, stderr_tail = _spawn_monitor(args, issue_id)
                continue
            dogfood(
                args.ticket,
                "error",
                f"wave-monitor exited rc={process.returncode} unexpectedly; stderr tail: {tail[:400]}",
            )
            restarts += 1
            if restarts > args.max_restarts:
                print(
                    json.dumps(
                        {
                            "wake_node": "unhandled_condition",
                            "summary": f"wave-monitor died {restarts} times; last rc={process.returncode}",
                            "evidence": {"stderr_tail": tail},
                        }
                    )
                )
                return CHILD_FAILURE_EXIT
            process, stderr_tail = _spawn_monitor(args, issue_id)
            continue


def _managed_watch_payload(args: argparse.Namespace, status: str) -> dict:
    return {
        "version": 1,
        "status": status,
        "ticket": args.ticket.upper(),
        "repo": args.repo,
        "phase": args.phase,
        "interval_seconds": float(args.interval),
        "timeout_minutes": watch_timeout_min(args),
        "pid": os.getpid(),
        "token": args.managed_token,
        "updated_at": now_iso(),
    }


def cmd_watch(args: argparse.Namespace) -> int:
    state_file = getattr(args, "managed_state_file", None)
    if not state_file:
        return _cmd_watch(args)
    if not getattr(args, "managed_token", None) or not getattr(args, "ready_file", None):
        raise RunError("managed watch needs state, token, and ready-file arguments")
    state_path = Path(state_file)
    lock_path = _watch_lock_path(state_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunError(f"a watch already owns {lock_path}") from exc
        _atomic_write_json(state_path, _managed_watch_payload(args, "starting"))
        try:
            return _cmd_watch(args)
        finally:
            state = _read_json_object(state_path) or _managed_watch_payload(args, "exited")
            if state.get("token") == args.managed_token:
                state.update({"status": "exited", "updated_at": now_iso()})
                _atomic_write_json(state_path, state)


def cmd_plan(args: argparse.Namespace) -> int:
    ensure_env(args.ticket, langgraph=False, github=False)
    issue = resolve_issue(args.ticket)
    bundle_identifiers(
        args.ticket,
        str(issue.get("identifier") or args.ticket),
        primary_issue_id=str(issue["id"]),
    )
    snapshot = linear_snapshot(issue["id"])
    wave = import_wave_module()
    comments = sorted(
        snapshot["comments"],
        key=lambda comment: (str(comment.get("createdAt") or ""), str(comment.get("id") or "")),
    )
    dispatch = re.compile(r"^@openswe\s+repo\s+[^\s/]+/[^\s/]+\b", re.IGNORECASE)
    start = 0
    for index, comment in enumerate(comments):
        if dispatch.match(str(comment.get("body") or "")):
            start = index + 1
    plans = [
        comment
        for comment in comments[start:]
        if wave.is_plan_comment(str(comment.get("body") or ""))
    ]
    if args.last is not None:
        plans = plans[-args.last :]
    for comment in plans:
        user = (comment.get("user") or {}).get("name")
        print(f"----- {user} at {comment.get('createdAt')} -----")
        print(comment.get("body") or "")
    if not plans:
        print("(no plan comments yet)")
    return 0


def _post_prepared(
    args: argparse.Namespace, action: str, body: str, issue: dict | None = None
) -> int:
    issue = issue or resolve_issue(args.ticket)
    guard_body_hygiene(body)
    guard_placeholders(args.ticket, body, getattr(args, "force", False))
    thread_id = import_wave_module().derive_linear_thread_id(issue["id"])
    watch_context = post_watch_context(args.ticket, action, getattr(args, "repo", None))
    final = _post_with_handoff(action, args.ticket, issue["id"], body, thread_id)
    watch = ensure_post_watch(watch_context)
    dogfood(
        args.ticket,
        "cmd",
        f"{action} posted on {issue['identifier']}; handoff "
        f"status={final.get('thread_status')} runs={len(final.get('run_ids') or [])}: {body[:160]}",
    )
    print(
        json.dumps(
            {
                "identifier": issue["identifier"],
                "posted": action,
                "handoff": final,
                "watch": watch,
            }
        )
    )
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    ensure_env(args.ticket, langgraph=True, github=False)
    if not args.adjudicated:
        raise RunError(
            "Refusing to approve without adjudication. Read "
            "../openswe-wave/references/adjudication-checklist.md, "
            "record your rulings in the approval body, then re-run with --adjudicated."
        )
    body = read_body(args)
    guard_body_hygiene(body)
    # Everything that can reject this approval runs BEFORE the plan-record write.
    # The write is not undone by `reject`, so a body refused after it would leave
    # the plan approved with no dispatching comment — and the next comment on the
    # issue, of any kind, would then run against an approved plan.
    issue = resolve_issue(args.ticket)
    identifiers = bundle_identifiers(
        args.ticket,
        str(issue.get("identifier") or args.ticket),
        primary_issue_id=str(issue["id"]),
    )
    guard_placeholders(
        args.ticket, body, False if len(identifiers) > 1 else getattr(args, "force", False)
    )
    if len(identifiers) > 1:
        guard_bundle_membership(body, identifiers)
    thread_id = import_wave_module().derive_linear_thread_id(issue["id"])
    watch_context = post_watch_context(args.ticket, "approval", getattr(args, "repo", None))
    state = set_plan_status(thread_id, "approved", plan_mode=False)
    dogfood(
        args.ticket,
        "cmd",
        f"plan record {state.get('previous')!r} -> 'approved' on thread {thread_id}"
        + ("" if state.get("metadata_ok") else " (thread metadata mirror failed)"),
    )
    plan_context = {
        "previous": state.get("previous"),
        "status": "approved",
        "rollback": "not automatic",
    }
    final = _post_with_handoff(
        "approval",
        args.ticket,
        issue["id"],
        body,
        thread_id,
        plan_context=plan_context,
    )
    watch = ensure_post_watch(watch_context)
    dogfood(
        args.ticket,
        "cmd",
        f"approval posted on {issue['identifier']}; handoff "
        f"status={final.get('thread_status')} runs={len(final.get('run_ids') or [])}: {body[:160]}",
    )
    print(
        json.dumps(
            {
                "identifier": issue["identifier"],
                "posted": "approval",
                "handoff": final,
                "watch": watch,
            }
        )
    )
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    ensure_env(args.ticket, langgraph=True, github=False)
    body = read_body(args)
    guard_body_hygiene(body)
    issue = resolve_issue(args.ticket)
    identifiers = bundle_identifiers(
        args.ticket,
        str(issue.get("identifier") or args.ticket),
        primary_issue_id=str(issue["id"]),
    )
    guard_placeholders(
        args.ticket, body, False if len(identifiers) > 1 else getattr(args, "force", False)
    )
    thread_id = import_wave_module().derive_linear_thread_id(issue["id"])
    watch_context = post_watch_context(args.ticket, "rejection", getattr(args, "repo", None))
    # Send the record back to revising, as the dashboard's reject endpoint does.
    # Without this a rejection posted after an approval would leave the plan
    # approved, and the resulting run would arm auto-merge on a rejected plan.
    state = set_plan_status(thread_id, "revising", plan_mode=True)
    dogfood(
        args.ticket,
        "cmd",
        f"plan record {state.get('previous')!r} -> 'revising' on thread {thread_id}",
    )
    plan_context = {
        "previous": state.get("previous"),
        "status": "revising",
        "rollback": "not automatic",
    }
    final = _post_with_handoff(
        "rejection",
        args.ticket,
        issue["id"],
        body,
        thread_id,
        plan_context=plan_context,
    )
    watch = ensure_post_watch(watch_context)
    dogfood(
        args.ticket,
        "cmd",
        f"rejection posted on {issue['identifier']}; handoff "
        f"status={final.get('thread_status')} runs={len(final.get('run_ids') or [])}: {body[:160]}",
    )
    print(
        json.dumps(
            {
                "identifier": issue["identifier"],
                "posted": "rejection",
                "handoff": final,
                "watch": watch,
            }
        )
    )
    return 0


def cmd_comment(args: argparse.Namespace) -> int:
    ensure_env(args.ticket, langgraph=True, github=False)
    return _post_prepared(args, "comment", read_body(args))


def cmd_nudge(args: argparse.Namespace) -> int:
    ensure_env(args.ticket, langgraph=True, github=False)
    issue = resolve_issue(args.ticket)
    body = NUDGE_TEMPLATE.format(ticket=issue["identifier"], minutes=args.minutes)
    return _post_prepared(args, "nudge", body)


def cmd_log(args: argparse.Namespace) -> int:
    tag = "ISSUE" if args.issue else "note"
    dogfood(args.ticket, tag, args.text)
    print(str(log_path(args.ticket)))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    handoffs = ensure_handoffs()
    candidates = sorted(handoffs.glob(f"{args.ticket.upper()}-*-run.md"))
    if not candidates:
        raise RunError(f"No dogfood log found for {args.ticket} under {handoffs}")
    path = candidates[-1]
    manifest = load_bundle_manifest(args.ticket, path)
    bundle_manifest = manifest if manifest is not None and len(manifest["members"]) > 1 else None
    entries = [line for line in path.read_text().splitlines() if line.startswith("- ")]
    issues = [line for line in entries if "[ISSUE]" in line]
    wakes = [line for line in entries if "[wake]" in line]
    dispatches = [
        match
        for line in entries
        if (
            match := re.search(
                r"\[cmd\] dispatched \S+ to ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) ", line
            )
        )
    ]
    pr_wakes = [match for line in wakes if (match := re.search(r"\bPR #(\d+)\b", line))]
    terminal_wakes = [
        match for line in wakes if (match := re.search(r"\[wake\] (terminal_[a-z_]+)\b", line))
    ]
    repo = dispatches[-1].group(1) if dispatches else None
    pr_number = int(pr_wakes[-1].group(1)) if pr_wakes else None
    terminal_state = terminal_wakes[-1].group(1) if terminal_wakes else None
    pr_reference = f"{repo}#{pr_number}" if repo and pr_number else None
    pr_url = None
    merge_sha = None
    pr_body = None
    pr_state = None
    pr_lookup_error = None
    should_query = bool(
        pr_reference and (bundle_manifest is not None or terminal_state == "terminal_merged")
    )
    if should_query:
        fields = "url,state,body,mergeCommit" if bundle_manifest else "url,mergeCommit"
        try:
            result = subprocess.run(
                ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", fields],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            result = None
            pr_lookup_error = str(exc)
        if result is not None and result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                payload = {}
                pr_lookup_error = f"invalid GitHub JSON: {exc.msg}"
            pr_url = payload.get("url")
            merge_commit = payload.get("mergeCommit") or {}
            merge_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
            pr_body = payload.get("body")
            pr_state = str(payload.get("state") or "").upper() or None
        elif result is not None:
            pr_lookup_error = (
                result.stderr or result.stdout
            ).strip() or f"gh exited {result.returncode}"
    print(f"log: {path}")
    print(f"terminal state: {terminal_state or '(none recorded)'}")
    if pr_url or pr_reference:
        print(f"PR: {pr_url or pr_reference}")
    if merge_sha:
        print(f"merge SHA: {merge_sha}")
    complete = True
    if bundle_manifest is not None:
        identifiers = [member["identifier"] for member in bundle_manifest["members"]]
        members_by_identifier = {
            member["identifier"]: member for member in bundle_manifest["members"]
        }
        member_results = []
        for identifier in identifiers:
            try:
                issue = resolve_issue(identifier)
                canonical_identifier = str(issue.get("identifier") or "").strip().upper()
                canonical_id = str(issue.get("id") or "").strip()
                expected = members_by_identifier[identifier]
                identity_ok = (
                    canonical_identifier == identifier and canonical_id == expected["issue_id"]
                )
                state = issue.get("state") or {}
                state_type = str(state.get("type") or "").lower()
                member_results.append(
                    {
                        "identifier": canonical_identifier or identifier,
                        "issue_id": canonical_id or "unresolved",
                        "state": state.get("name") or state_type or "unknown",
                        "state_type": state_type or "unknown",
                        "tracker_terminal": state_type in {"completed", "canceled"},
                        "identity_ok": identity_ok,
                        "closing_line": has_closing_line(pr_body, identifier),
                    }
                )
            except RunError as exc:
                member_results.append(
                    {
                        "identifier": identifier,
                        "issue_id": "unresolved",
                        "state": "unresolved",
                        "state_type": "unresolved",
                        "tracker_terminal": False,
                        "identity_ok": False,
                        "closing_line": has_closing_line(pr_body, identifier),
                        "error": str(exc),
                    }
                )
        pr_terminal = pr_state in {"MERGED", "CLOSED"}
        complete = (
            pr_lookup_error is None
            and pr_terminal
            and all(
                item["closing_line"] and item["tracker_terminal"] and item["identity_ok"]
                for item in member_results
            )
        )
        print(f"shared PR live state: {pr_state or 'unavailable'}")
        print(f"bundle members ({len(member_results)}):")
        for item in member_results:
            closing = "present" if item["closing_line"] else "MISSING"
            tracker = "terminal" if item["tracker_terminal"] else "NONTERMINAL/UNRESOLVED"
            identity = "canonical" if item["identity_ok"] else "IDENTITY MISMATCH"
            print(
                f"  {item['identifier']} ({item['issue_id']}): Closes line {closing}; "
                f"Linear {item['state']} ({item['state_type']}) {tracker}; {identity}"
            )
            if item.get("error"):
                print(f"    error: {item['error']}")
        if pr_lookup_error:
            print(f"  PR evidence failed: {pr_lookup_error}")
        if not pr_terminal:
            print(f"  shared PR terminal state unresolved: {pr_state or 'unavailable'}")
        print(f"bundle completion: {'complete' if complete else 'INCOMPLETE'}")
    print(f"wakes ({len(wakes)}):")
    for line in wakes:
        print(f"  {line}")
    print(f"dogfood issues ({len(issues)}):")
    for line in issues:
        print(f"  {line}")
    if not issues:
        print("  (none recorded — say so explicitly in the chat summary)")
    print("Reminder: end the run by summarizing these issues in chat output.")
    return 0 if complete else 2


# --------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openswe-run", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("env", help="preflight report; rc 0 when ready").set_defaults(func=cmd_env)

    start = sub.add_parser("start", help="dispatch one ticket via @openswe Linear comment")
    start.add_argument("--ticket", required=True)
    start.add_argument(
        "--include-ticket",
        action="append",
        default=[],
        help="included Linear ticket; repeat for one atomic bundle",
    )
    start.add_argument("--repo", required=True, help="owner/repo the run should target")
    start.add_argument("--ref", default="main")
    start.add_argument("--scope")
    start.add_argument("--boundaries")
    start.add_argument("--verify")
    start.add_argument("--body-file", help="full custom dispatch body ('-' for stdin)")
    start.add_argument("--dry-run", action="store_true")
    start.add_argument("--force", action="store_true")
    start.set_defaults(func=cmd_start)

    watch = sub.add_parser("watch", help="block until one wake; prints a single JSON line")
    watch.add_argument("--ticket", required=True)
    watch.add_argument("--repo", required=True)
    watch.add_argument("--pr-number", type=int)
    watch.add_argument("--phase", choices=sorted(PHASE_TIMEOUT_MINUTES), default="plan")
    watch.add_argument("--interval", type=float, default=WATCH_INTERVAL_SECONDS)
    watch.add_argument("--timeout-min", type=float)
    watch.add_argument("--heartbeat-min", type=float, default=10)
    watch.add_argument("--max-restarts", type=int, default=2)
    watch.add_argument("--managed-state-file", help=argparse.SUPPRESS)
    watch.add_argument("--managed-token", help=argparse.SUPPRESS)
    watch.add_argument("--ready-file", help=argparse.SUPPRESS)
    watch.add_argument("--follow", action="store_true", help="stream wakes instead of exiting")
    watch.set_defaults(func=cmd_watch)

    plan = sub.add_parser("plan", help="print durable plan comments from the current dispatch")
    plan.add_argument("--ticket", required=True)
    plan.add_argument("--last", type=int)
    plan.set_defaults(func=cmd_plan)

    approve = sub.add_parser("approve", help="post plan approval (requires --adjudicated)")
    approve.add_argument("--ticket", required=True)
    approve.add_argument("--repo")
    approve.add_argument("--body-file", required=True)
    approve.add_argument("--adjudicated", action="store_true")
    approve.add_argument("--force", action="store_true")
    approve.set_defaults(func=cmd_approve)

    reject = sub.add_parser("reject", help="post plan rejection with corrections")
    reject.add_argument("--ticket", required=True)
    reject.add_argument("--repo")
    reject.add_argument("--body-file", required=True)
    reject.add_argument("--force", action="store_true")
    reject.set_defaults(func=cmd_reject)

    comment = sub.add_parser("comment", help="post a mid-run message to the agent")
    comment.add_argument("--ticket", required=True)
    comment.add_argument("--repo")
    comment.add_argument("--body-file", required=True)
    comment.add_argument("--force", action="store_true")
    comment.set_defaults(func=cmd_comment)

    nudge = sub.add_parser("nudge", help="post the one allowed stall nudge")
    nudge.add_argument("--ticket", required=True)
    nudge.add_argument("--repo")
    nudge.add_argument("--minutes", type=int, default=30)
    nudge.set_defaults(func=cmd_nudge)

    log = sub.add_parser("log", help="append a note or [ISSUE] to the dogfood log")
    log.add_argument("--ticket", required=True)
    log.add_argument("--issue", action="store_true", help="tag as a dogfood ISSUE finding")
    log.add_argument("text")
    log.set_defaults(func=cmd_log)

    report = sub.add_parser("report", help="print wakes + dogfood issues for the chat summary")
    report.add_argument("--ticket", required=True)
    report.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RunError as exc:
        print(f"openswe-run: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
