"""Run-completion webhook handler — guarantees every run ends with a signal.

The platform POSTs a run-completion payload to ``/webhooks/run-complete`` (wired
as the ``webhook`` on every dispatched run, see ``agent.dispatch``). Every terminal callback also releases any review check deferred behind that
PR-linked implementation run. When a run ends in a failure state (``error`` /
``timeout``) we post a short failure reply to the originating channel, so a run
that died on a server recycle or hit a limit never leaves the user in silence.

This decouples "the user gets an answer" from "the agent remembered to reply."
The reply is idempotent per run when the webhook includes a run id. Older or
manual payloads without a run id fall back to legacy thread-level idempotence so
missing ids degrade dedupe instead of silencing failure replies.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from typing import Any

from .review.findings import REVIEWER_THREAD_KIND, open_surfaced_finding_count
from .review.publish import settle_review_check_run
from .utils.dashboard_links import dashboard_thread_url
from .utils.github_app import get_github_app_installation_token
from .utils.github_checks import (
    fetch_pull_request_head_sha,
    fetch_review_check_run_status,
    incomplete_review_check_result,
    review_check_conclusion,
)
from .utils.github_comments import post_github_comment
from .utils.linear import comment_on_linear_issue
from .utils.slack import post_slack_thread_reply
from .utils.thread_ids import generate_reviewer_thread_id
from .utils.thread_ops import langgraph_client

logger = logging.getLogger(__name__)

# Run statuses that mean the user will otherwise get nothing back. "interrupted"
# is intentionally excluded: with multitask_strategy="interrupt", a normal
# follow-up halts the prior run (status "interrupted") while its replacement
# carries on — that's healthy, not a failure worth a "couldn't finish" reply.
_TERMINAL_FAILURE_STATUSES = frozenset({"error", "timeout"})
_TERMINAL_RUN_STATUSES = frozenset({"success", "error", "timeout", "interrupted"})
_REVIEW_CHECK_DEFERRED_RESULT_KEY = "review_check_deferred_result"
_FAILURE_REPLY_FLAG = "failure_reply_posted"
_FAILURE_REPLY_RUN_ID = "failure_reply_posted_run_id"
_FAILURE_REPLY_RUN_IDS = "failure_reply_posted_run_ids"
_MAX_FAILURE_REPLY_RUN_IDS = 20

# Shared-secret bearer token proving a /webhooks/run-complete call came from our
# own dispatch (which appends ?token= when this is set) rather than from an
# attacker hitting the public route. Fail closed when unset: the route rejects
# every call, so completion replies stay off until the secret is configured.
RUN_COMPLETE_WEBHOOK_SECRET = os.environ.get("RUN_COMPLETE_WEBHOOK_SECRET")
if not RUN_COMPLETE_WEBHOOK_SECRET:
    logger.warning(
        "RUN_COMPLETE_WEBHOOK_SECRET is not set; /webhooks/run-complete is fail-closed "
        "(all calls rejected) and run-failure replies are disabled. Set it to enable them."
    )


def verify_run_complete_token(token: str | None) -> bool:
    """Return whether a run-completion webhook token is acceptable.

    Fail closed: with no secret configured, reject every call rather than accept
    unauthenticated requests on a publicly reachable route.
    """
    secret = RUN_COMPLETE_WEBHOOK_SECRET
    if not secret:
        return False
    return token is not None and hmac.compare_digest(token, secret)


def _failure_text(status: str, dashboard_url: str | None = None, cause: str | None = None) -> str:
    if status == "timeout":
        reason = "timed out"
    elif status == "interrupted":
        reason = "was interrupted before it could finish"
    else:
        reason = "hit an unexpected error"
    text = (
        f"⚠️ I wasn't able to finish that — the run {reason}. "
        "Send another message and I'll pick it back up."
    )
    if cause:
        text += f" Cause: {cause}."
    if dashboard_url:
        text += f" You can view the error in <{dashboard_url}|Open SWE Web>."
    return text


def _dead_thread_text(
    status: str, streak: int, dashboard_url: str | None = None, cause: str | None = None
) -> str:
    reason = "timed out" if status == "timeout" else "hit an unexpected error"
    text = (
        f"🛑 This thread has failed {streak} consecutive runs and may be unrecoverable — "
        f"the latest run {reason}. Reset the thread before sending another message to "
        "re-dispatch."
    )
    if cause:
        text += f" Cause: {cause}."
    if dashboard_url:
        text += f" You can view the error in <{dashboard_url}|Open SWE Web>."
    return text


async def _review_check_id_for_run(client: Any, thread_id: str, run_id: str | None) -> int | None:
    """Return the full-review check owned by a completed run."""
    if run_id is None:
        return None
    try:
        run = await client.runs.get(thread_id, run_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "run-complete: could not load completed run %s/%s",
            thread_id,
            run_id,
            exc_info=True,
        )
        return None
    run_metadata = run.get("metadata") if isinstance(run, dict) else None
    check_run_id = (
        run_metadata.get("review_check_run_id") if isinstance(run_metadata, dict) else None
    )
    return check_run_id if isinstance(check_run_id, int) else None


async def _settle_failed_reviewer_check(
    thread_id: str, metadata: dict[str, Any], *, owned_check_run_id: int | None
) -> None:
    """Best-effort cleanup for a full-review check owned by a terminal run."""
    if metadata.get("kind") != REVIEWER_THREAD_KIND:
        return
    if not isinstance(owned_check_run_id, int):
        return
    pr = metadata.get("pr")
    if not isinstance(pr, dict):
        return
    owner = pr.get("owner")
    repo = pr.get("name")
    if not isinstance(owner, str) or not owner or not isinstance(repo, str) or not repo:
        return
    try:
        token = await get_github_app_installation_token(target_repo=f"{owner}/{repo}")
        if not token:
            logger.warning("run-complete: no GitHub token to settle review check for %s", thread_id)
            return
        check_status = await fetch_review_check_run_status(
            owner=owner,
            repo=repo,
            check_run_id=owned_check_run_id,
            token=token,
        )
        if check_status != "in_progress":
            return
        pending = (
            metadata.get("review_check_pending_result")
            if metadata.get("review_check_run_id") == owned_check_run_id
            else None
        )
        if isinstance(pending, dict) and pending.get("conclusion") in {
            "success",
            "neutral",
            "failure",
        }:
            conclusion = pending["conclusion"]
            title = str(pending.get("title") or "Review completed")
            summary = str(pending.get("summary") or "")
        else:
            conclusion, title, summary = incomplete_review_check_result()
        await settle_review_check_run(
            thread_id=thread_id,
            owner=owner,
            repo=repo,
            token=token,
            conclusion=conclusion,
            title=title,
            summary=summary,
            expected_check_run_id=owned_check_run_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "run-complete: could not settle review check for %s", thread_id, exc_info=True
        )


def _run_value(run: Any, name: str) -> Any:
    return run.get(name) if isinstance(run, dict) else getattr(run, name, None)


async def _run_failure_cause(client: Any, thread_id: str, run_id: str) -> str | None:
    try:
        payload = await client.runs.join(thread_id, run_id)
        if not isinstance(payload, dict):
            return None
        err = payload.get("__error__")
        if not isinstance(err, dict):
            return None
        error = err.get("error")
        message = err.get("message")
        if not error and message is None:
            return None
        cause = f"{error}: {message}" if error else str(message)
        return re.sub(r"\s+", " ", cause).strip()[:300]
    except Exception:  # noqa: BLE001
        logger.debug(
            "run-complete: could not inspect failure cause for %s/%s",
            thread_id,
            run_id,
            exc_info=True,
        )
        return None


async def _consecutive_failures(client: Any, thread_id: str, completed_run_id: str | None) -> int:
    try:
        runs = await client.runs.list(thread_id, limit=10)
        ordered_runs = sorted(
            runs,
            key=lambda run: _run_value(run, "created_at") or "",
            reverse=True,
        )
        listed_ids = {_run_value(run, "run_id") or _run_value(run, "id") for run in ordered_runs}
        streak = 0
        for run in ordered_runs:
            listed_run_id = _run_value(run, "run_id") or _run_value(run, "id")
            if completed_run_id is not None and listed_run_id == completed_run_id:
                streak += 1
                continue

            status = _run_value(run, "status")
            status = status.lower() if isinstance(status, str) else None
            if status in _TERMINAL_FAILURE_STATUSES:
                streak += 1
            elif status == "success":
                break

        if completed_run_id is not None and completed_run_id not in listed_ids:
            streak += 1
        return streak
    except Exception:  # noqa: BLE001
        logger.debug(
            "run-complete: could not derive consecutive failures for %s/%s",
            thread_id,
            completed_run_id,
            exc_info=True,
        )
        return 1


async def _latest_run_info(client: Any, thread_id: str) -> tuple[str | None, str | None, bool]:
    try:
        runs = await client.runs.list(thread_id, limit=1)
    except Exception:  # noqa: BLE001
        logger.debug("run-complete: could not inspect runs for %s", thread_id, exc_info=True)
        return None, None, False
    latest = runs[0] if runs else None
    status = _run_value(latest, "status")
    run_id = _run_value(latest, "run_id") or _run_value(latest, "id")
    return (
        status.lower() if isinstance(status, str) else None,
        run_id if isinstance(run_id, str) and run_id else None,
        True,
    )


async def settle_deferred_review_check(
    client: Any,
    implementation_thread_id: str,
    implementation_metadata: dict[str, Any],
    *,
    completed_run_id: str | None,
    completed_status: str,
) -> bool:
    repo_config = implementation_metadata.get("repo")
    pr_number = implementation_metadata.get("pr_number")
    if not isinstance(repo_config, dict) or not isinstance(pr_number, int):
        return False
    owner = repo_config.get("owner")
    repo = repo_config.get("name")
    if not isinstance(owner, str) or not owner or not isinstance(repo, str) or not repo:
        return False

    reviewer_thread_id = generate_reviewer_thread_id(owner, repo, pr_number)
    try:
        reviewer_thread = await client.threads.get(reviewer_thread_id)
    except Exception:  # noqa: BLE001
        return False
    reviewer_metadata = (
        reviewer_thread.get("metadata") if isinstance(reviewer_thread, dict) else None
    )
    if not isinstance(reviewer_metadata, dict):
        return False
    deferred = reviewer_metadata.get(_REVIEW_CHECK_DEFERRED_RESULT_KEY)
    if (
        not isinstance(deferred, dict)
        or deferred.get("implementation_thread_id") != implementation_thread_id
    ):
        return False
    held_check_run_id = deferred.get("review_check_run_id")
    if not isinstance(held_check_run_id, int):
        return False
    current_check_run_id = reviewer_metadata.get("review_check_run_id")
    if current_check_run_id != held_check_run_id:
        return False

    latest_status, latest_run_id, latest_known = await _latest_run_info(
        client, implementation_thread_id
    )
    if latest_known and latest_status in {"pending", "running"}:
        return False
    held_run_id = deferred.get("implementation_run_id")
    if latest_known and latest_run_id and latest_status:
        effective_status = latest_status
    elif isinstance(held_run_id, str) and completed_run_id == held_run_id:
        effective_status = completed_status
    else:
        return False

    token = await get_github_app_installation_token(target_repo=f"{owner}/{repo}")
    if not token:
        return False
    live_head = await fetch_pull_request_head_sha(
        owner=owner, repo=repo, pr_number=pr_number, token=token
    )
    held_head = deferred.get("head_sha")
    if not isinstance(live_head, str) or not isinstance(held_head, str):
        return False
    if live_head != held_head:
        return True

    findings = reviewer_metadata.get("findings")
    finding_count = open_surfaced_finding_count(findings if isinstance(findings, list) else [])
    if finding_count:
        conclusion, title, summary = review_check_conclusion(finding_count)
    elif effective_status == "success":
        conclusion = deferred.get("conclusion")
        title = deferred.get("title")
        summary = deferred.get("summary")
        if conclusion not in {"success", "neutral", "failure"}:
            return False
        title = str(title or "Review completed")
        summary = str(summary or "")
    else:
        conclusion = "failure"
        title = "Implementation work did not complete"
        summary = (
            "The PR-linked Open SWE run ended without landing a new commit. "
            "Re-run the requested work before merging."
        )
    settled = await settle_review_check_run(
        thread_id=reviewer_thread_id,
        owner=owner,
        repo=repo,
        token=token,
        conclusion=conclusion,
        title=title,
        summary=summary,
        expected_check_run_id=held_check_run_id,
    )
    if not settled:
        raise RuntimeError("Deferred review check completion failed")
    return True


async def _post_failure_reply(
    thread_id: str,
    metadata: dict[str, Any],
    status: str,
    cause: str | None = None,
    streak: int = 0,
) -> bool:
    """Post a failure reply to the run's originating channel. Best-effort."""
    source = metadata.get("source")
    ctx = metadata.get("source_context")
    ctx = ctx if isinstance(ctx, dict) else {}

    def _text(dashboard_url: str | None = None) -> str:
        if streak >= 2:
            return _dead_thread_text(status, streak, dashboard_url, cause)
        return _failure_text(status, dashboard_url, cause)

    text = _text()

    slack_thread = ctx.get("slack_thread")
    if source == "slack" or isinstance(slack_thread, dict):
        if isinstance(slack_thread, dict):
            channel_id = slack_thread.get("channel_id")
            thread_ts = slack_thread.get("thread_ts")
            if channel_id and thread_ts:
                slack_text = _text(dashboard_thread_url(thread_id))
                return await post_slack_thread_reply(channel_id, thread_ts, slack_text)
        return False

    if source == "linear":
        linear_issue = ctx.get("linear_issue")
        if isinstance(linear_issue, dict):
            issue_id = linear_issue.get("id")
            if issue_id:
                return await comment_on_linear_issue(issue_id, text)
        return False

    if source in ("github", "github_issue"):
        repo_config = metadata.get("repo")
        number = ctx.get("pr_number")
        if number is None:
            github_issue = ctx.get("github_issue")
            if isinstance(github_issue, dict):
                number = github_issue.get("number")
        if isinstance(repo_config, dict) and isinstance(number, int):
            owner = repo_config.get("owner")
            repo = repo_config.get("name")
            target_repo = f"{owner}/{repo}" if owner and repo else None
            token = await get_github_app_installation_token(target_repo=target_repo)
            if token:
                return await post_github_comment(repo_config, number, text, token=token)
        return False

    logger.info("No failure-reply channel for thread %s (source=%s)", thread_id, source)
    return False


def _posted_failure_run_ids(metadata: dict[str, Any]) -> list[str]:
    raw = metadata.get(_FAILURE_REPLY_RUN_IDS)
    ids = [item for item in raw if isinstance(item, str) and item] if isinstance(raw, list) else []
    latest = metadata.get(_FAILURE_REPLY_RUN_ID)
    if isinstance(latest, str) and latest and latest not in ids:
        ids.append(latest)
    return ids


def _failure_reply_metadata(metadata: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    if run_id is None:
        return {_FAILURE_REPLY_FLAG: True}
    ids = [item for item in _posted_failure_run_ids(metadata) if item != run_id]
    ids.append(run_id)
    return {
        _FAILURE_REPLY_RUN_ID: run_id,
        _FAILURE_REPLY_RUN_IDS: ids[-_MAX_FAILURE_REPLY_RUN_IDS:],
    }


async def handle_run_completion(payload: dict[str, Any]) -> dict[str, str]:
    """Handle a platform run-completion webhook POST.

    Release deferred review checks and post deduplicated failure replies.
    """
    status = payload.get("status")
    thread_id = payload.get("thread_id")
    raw_run_id = payload.get("run_id")
    run_id = raw_run_id if isinstance(raw_run_id, str) and raw_run_id else None
    if not isinstance(thread_id, str) or not thread_id:
        return {"status": "ignored", "reason": "missing thread_id"}
    if status not in _TERMINAL_RUN_STATUSES:
        return {"status": "ignored", "reason": f"non-terminal status: {status}"}

    client = langgraph_client()
    try:
        thread = await client.threads.get(thread_id)
    except Exception:  # noqa: BLE001
        logger.warning("run-complete: could not load thread %s", thread_id, exc_info=True)
        return {"status": "error", "reason": "thread fetch failed"}

    metadata = thread.get("metadata") if isinstance(thread, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    deferred_error: Exception | None = None
    try:
        deferred_settled = await settle_deferred_review_check(
            client,
            thread_id,
            metadata,
            completed_run_id=run_id,
            completed_status=status,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "run-complete: could not settle deferred review check for %s",
            thread_id,
            exc_info=True,
        )
        deferred_settled = False
        deferred_error = RuntimeError("Deferred review check settlement failed")
    owned_check_run_id = None
    if status == "interrupted" or status in _TERMINAL_FAILURE_STATUSES:
        owned_check_run_id = await _review_check_id_for_run(client, thread_id, run_id)
    if status == "interrupted":
        await _settle_failed_reviewer_check(
            thread_id, metadata, owned_check_run_id=owned_check_run_id
        )
    if status not in _TERMINAL_FAILURE_STATUSES:
        if deferred_error is not None:
            raise deferred_error
        return {
            "status": "ok" if deferred_settled else "ignored",
            "reason": (
                "deferred review check settled"
                if deferred_settled
                else f"no deferred review check for status: {status}"
            ),
        }
    await _settle_failed_reviewer_check(thread_id, metadata, owned_check_run_id=owned_check_run_id)
    if run_id is None:
        # Payloads without run ids fall back to the old per-thread flag; run-scoped
        # dedupe intentionally does not read it so future runs can still report.
        if metadata.get(_FAILURE_REPLY_FLAG):
            if deferred_error is not None:
                raise deferred_error
            return {"status": "ignored", "reason": "failure reply already posted"}
    elif run_id in _posted_failure_run_ids(metadata):
        if deferred_error is not None:
            raise deferred_error
        return {"status": "ignored", "reason": "failure reply already posted for run"}

    cause = await _run_failure_cause(client, thread_id, run_id) if run_id is not None else None
    streak = await _consecutive_failures(client, thread_id, run_id)

    posted = await _post_failure_reply(thread_id, metadata, status, cause, streak)
    if not posted:
        if deferred_error is not None:
            raise deferred_error
        return {"status": "ignored", "reason": "no reply posted"}

    reply_metadata = _failure_reply_metadata(metadata, run_id)
    try:
        await client.threads.update(
            thread_id=thread_id,
            metadata=reply_metadata,
        )
    except Exception:  # noqa: BLE001
        logger.warning("run-complete: could not flag thread %s", thread_id, exc_info=True)
    logger.info("Posted failure reply for thread %s (status=%s)", thread_id, status)
    if deferred_error is not None:
        raise deferred_error
    return {"status": "ok", "reason": "failure reply posted"}
