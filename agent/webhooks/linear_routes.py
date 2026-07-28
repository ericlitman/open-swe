"""Linear webhook HTTP routes."""

from fastapi import APIRouter

from . import common
from . import linear as service

router = APIRouter()


@router.post("/webhooks/linear")
async def linear_webhook(  # noqa: PLR0911, PLR0912, PLR0915
    request: common.Request, background_tasks: common.BackgroundTasks
) -> dict[str, str]:
    """Handle Linear webhooks.

    Triggers a new LangGraph run when an issue gets the 'open-swe' label added.
    """
    common.logger.info("Received Linear webhook")
    body = await request.body()

    signature = request.headers.get("Linear-Signature", "")
    if not common.verify_linear_signature(body, signature, common.LINEAR_WEBHOOK_SECRET):
        common.logger.warning("Invalid webhook signature")
        raise common.HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = common.json.loads(body)
    except common.json.JSONDecodeError:
        common.logger.exception("Failed to parse webhook JSON")
        return {"status": "error", "message": "Invalid JSON"}

    if payload.get("type") != "Comment":
        common.logger.debug("Ignoring webhook: not a Comment event")
        return {"status": "ignored", "reason": "Not a Comment event"}

    action = payload.get("action")
    if action != "create":
        common.logger.debug("Ignoring webhook: action is %s, not create", action)
        return {
            "status": "ignored",
            "reason": f"Comment action is '{action}', only processing 'create'",
        }

    data = payload.get("data", {})

    if data.get("botActor"):
        common.logger.debug("Ignoring webhook: comment is from a bot")
        return {"status": "ignored", "reason": "Comment is from a bot"}

    comment_body = data.get("body", "")
    bot_message_prefixes = [
        "🔐 **GitHub Authentication Required**",
        "✅ **Pull Request Created**",
        "✅ **Pull Request Updated**",
        "**Pull Request Created**",
        "**Pull Request Updated**",
        "🤖 **Agent Response**",
        "❌ **Agent Error**",
    ]
    for prefix in bot_message_prefixes:
        if comment_body.startswith(prefix):
            common.logger.debug("Ignoring webhook: comment is our own bot message")
            return {"status": "ignored", "reason": "Comment is our own bot message"}
    mention = common.classify_comment_mention(comment_body, ("@openswe",))
    mention_ignore_reasons = {
        "no_mention": "Comment doesn't mention @openswe",
        "mid_line": "@openswe mention is not at start of line",
        "inline_code": "@openswe mention is inside inline code",
        "fenced_code": "@openswe mention is inside a fenced code block",
    }
    if mention.disposition != "accepted":
        reason = mention_ignore_reasons[mention.disposition]
        common.logger.debug("Ignoring webhook: %s", reason)
        return {"status": "ignored", "reason": reason}

    issue = data.get("issue", {})
    if not issue:
        common.logger.debug("Ignoring webhook: no issue data in comment")
        return {"status": "ignored", "reason": "No issue data in comment"}

    issue_id = issue.get("id", "")
    repo_config = common.extract_adjacent_repo_directive(comment_body, mention)

    if repo_config:
        common.logger.debug(
            "Using repo from adjacent comment directive: %s/%s",
            repo_config["owner"],
            repo_config["name"],
        )
    else:
        try:
            repo_config = await service.get_linear_thread_repo_config(issue_id)
        except service.LinearThreadRepoError:
            await service.post_linear_routing_failure(
                issue_id,
                data.get("id", ""),
                "Couldn't safely read a repository from the existing thread. Retry or specify it "
                "as `repo owner/name` immediately after the agent mention.",
            )
            return {"status": "ignored", "reason": "Failed to access thread repository metadata"}

    if not repo_config:
        await service.post_linear_routing_failure(
            issue_id,
            data.get("id", ""),
            "Couldn't determine the target repository. Specify it as `repo owner/name` "
            "immediately after the agent mention.",
        )
        return {"status": "ignored", "reason": "No repository directive or thread metadata"}

    if not common._is_repo_allowed(repo_config):
        common.logger.warning(
            "Rejecting Linear webhook: repo '%s/%s' not in allowlist",
            repo_config.get("owner"),
            repo_config.get("name"),
        )
        await service.post_linear_routing_failure(
            issue_id,
            data.get("id", ""),
            f"The target repository `{repo_config['owner']}/{repo_config['name']}` is not enabled. "
            "Specify an allowed repository as `repo owner/name` immediately after the agent mention.",
        )
        return {"status": "ignored", "reason": "Repository not in allowlist"}

    try:
        await service.persist_linear_thread_repo_config(issue_id, repo_config)
    except service.LinearThreadRepoError:
        await service.post_linear_routing_failure(
            issue_id,
            data.get("id", ""),
            "Couldn't save the target repository due to a temporary service error. Please retry.",
        )
        return {"status": "ignored", "reason": "Failed to persist thread repository metadata"}

    repo_owner = repo_config["owner"]
    repo_name = repo_config["name"]

    issue["triggering_comment"] = comment_body
    issue["triggering_comment_id"] = data.get("id", "")
    comment_user = data.get("user", {})
    if comment_user:
        issue["comment_author"] = comment_user

    common.logger.info(
        "Accepted webhook for issue '%s' (%s), scheduling background task",
        issue.get("title"),
        issue.get("id"),
    )
    background_tasks.add_task(service.process_linear_issue, issue, repo_config)

    return {
        "status": "accepted",
        "message": f"Processing issue '{issue.get('title')}' for repo {repo_owner}/{repo_name}",
    }


@router.get("/webhooks/linear")
async def linear_webhook_verify() -> dict[str, str]:
    """Verify endpoint for Linear webhook setup."""
    return {"status": "ok", "message": "Linear webhook endpoint is active"}
