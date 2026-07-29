"""Tests for Linear mention routing and visible failures."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from agent.webhooks import linear as linear_service
from agent.webhooks.linear_routes import linear_webhook


class _NotFoundError(RuntimeError):
    status_code = 404


def _payload(body: str = "@openswe please continue") -> dict:
    return {
        "type": "Comment",
        "action": "create",
        "data": {
            "id": "comment-123",
            "body": body,
            "issue": {"id": "issue-456", "title": "Test issue"},
            "user": {"id": "user-1", "name": "Test User", "email": "test@example.com"},
        },
    }


def _full_issue() -> dict:
    return {
        "id": "issue-456",
        "title": "Test issue",
        "identifier": "TEST-1",
        "url": "https://linear.app/test/issue/TEST-1",
        "team": {"id": "team-1", "name": "Test Team", "key": "TEST"},
        "project": None,
        "comments": {"nodes": []},
    }


async def _invoke(payload: dict) -> tuple[dict[str, str], MagicMock]:
    request = AsyncMock()
    request.body.return_value = json.dumps(payload).encode()
    request.headers = {"Linear-Signature": "valid"}
    background_tasks = MagicMock()
    result = await linear_webhook(request, background_tasks)
    return result, background_tasks


@pytest.mark.asyncio
async def test_explicit_comment_repo_skips_thread_inheritance() -> None:
    thread_repo = AsyncMock(return_value={"owner": "stored", "name": "repo"})
    persist_repo = AsyncMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch(
            "agent.webhooks.common.fetch_linear_issue_details",
            new_callable=AsyncMock,
            return_value=_full_issue(),
        ),
        patch("agent.webhooks.linear.get_linear_thread_repo_config", thread_repo),
        patch("agent.webhooks.linear.persist_linear_thread_repo_config", persist_repo),
        patch("agent.webhooks.common._is_repo_allowed", return_value=True),
    ):
        result, background_tasks = await _invoke(
            _payload("@openswe repo explicit-owner/explicit-repo")
        )

    assert result["status"] == "accepted"
    thread_repo.assert_not_awaited()
    persist_repo.assert_awaited_once_with(
        "issue-456", {"owner": "explicit-owner", "name": "explicit-repo"}
    )
    assert background_tasks.add_task.call_args.args[2] == {
        "owner": "explicit-owner",
        "name": "explicit-repo",
    }


@pytest.mark.asyncio
async def test_existing_thread_repo_precedes_profile_and_team_defaults() -> None:
    profile_repo = AsyncMock(return_value={"owner": "profile", "name": "repo"})
    persist_repo = AsyncMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch(
            "agent.webhooks.common.fetch_linear_issue_details",
            new_callable=AsyncMock,
            return_value=_full_issue(),
        ),
        patch(
            "agent.webhooks.linear.get_linear_thread_repo_config",
            new_callable=AsyncMock,
            return_value={"owner": "stored", "name": "repo"},
        ),
        patch("agent.webhooks.common.get_profile_default_repo", profile_repo),
        patch("agent.webhooks.linear.persist_linear_thread_repo_config", persist_repo),
        patch("agent.webhooks.common._is_repo_allowed", return_value=True),
    ):
        result, background_tasks = await _invoke(_payload())

    assert result["status"] == "accepted"
    profile_repo.assert_not_awaited()
    persist_repo.assert_awaited_once_with("issue-456", {"owner": "stored", "name": "repo"})
    assert background_tasks.add_task.call_args.args[2] == {
        "owner": "stored",
        "name": "repo",
    }


@pytest.mark.asyncio
async def test_missing_thread_does_not_use_profile_or_team_fallbacks() -> None:
    profile_repo = AsyncMock(return_value={"owner": "profile", "name": "repo"})
    team_default = AsyncMock(return_value={"owner": "team", "name": "repo"})
    post_failure = AsyncMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch(
            "agent.webhooks.linear.get_linear_thread_repo_config",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("agent.webhooks.common.get_profile_default_repo", profile_repo),
        patch("agent.webhooks.common.get_team_default_repo", team_default),
        patch("agent.webhooks.linear.post_linear_routing_failure", post_failure),
    ):
        result, background_tasks = await _invoke(_payload())

    assert result == {
        "status": "ignored",
        "reason": "No repository directive or thread metadata",
    }
    profile_repo.assert_not_awaited()
    team_default.assert_not_awaited()
    background_tasks.add_task.assert_not_called()
    post_failure.assert_awaited_once_with(
        "issue-456",
        "comment-123",
        "Couldn't determine the target repository. Specify it as `repo owner/name` "
        "immediately after the agent mention.",
    )


@pytest.mark.asyncio
async def test_unroutable_mention_posts_visible_reason_and_returns_200() -> None:
    post_failure = AsyncMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch(
            "agent.webhooks.common.fetch_linear_issue_details",
            new_callable=AsyncMock,
            return_value=_full_issue(),
        ),
        patch(
            "agent.webhooks.linear.get_linear_thread_repo_config",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agent.webhooks.common.resolve_login_from_email_async",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agent.webhooks.common.get_profile_default_repo",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("agent.webhooks.common.get_repo_config_from_team_mapping", return_value=None),
        patch(
            "agent.webhooks.common.get_team_default_repo",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("agent.webhooks.linear.post_linear_routing_failure", post_failure),
    ):
        result, background_tasks = await _invoke(_payload())

    assert result == {
        "status": "ignored",
        "reason": "No repository directive or thread metadata",
    }
    background_tasks.add_task.assert_not_called()
    post_failure.assert_awaited_once_with(
        "issue-456",
        "comment-123",
        "Couldn't determine the target repository. Specify it as `repo owner/name` "
        "immediately after the agent mention.",
    )


@pytest.mark.asyncio
async def test_allowlist_rejection_posts_visible_reason_and_returns_200() -> None:
    post_failure = AsyncMock()
    persist_repo = AsyncMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch(
            "agent.webhooks.common.fetch_linear_issue_details",
            new_callable=AsyncMock,
            return_value=_full_issue(),
        ),
        patch("agent.webhooks.linear.persist_linear_thread_repo_config", persist_repo),
        patch("agent.webhooks.common._is_repo_allowed", return_value=False),
        patch("agent.webhooks.linear.post_linear_routing_failure", post_failure),
    ):
        result, background_tasks = await _invoke(_payload("@openswe repo blocked/repo"))

    assert result == {"status": "ignored", "reason": "Repository not in allowlist"}
    background_tasks.add_task.assert_not_called()
    persist_repo.assert_not_awaited()
    post_failure.assert_awaited_once_with(
        "issue-456",
        "comment-123",
        "The target repository `blocked/repo` is not enabled. "
        "Specify an allowed repository as `repo owner/name` immediately after the agent mention.",
    )


@pytest.mark.asyncio
async def test_explicit_repo_persist_failure_does_not_schedule_run() -> None:
    post_failure = AsyncMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch(
            "agent.webhooks.common.fetch_linear_issue_details",
            new_callable=AsyncMock,
            return_value=_full_issue(),
        ),
        patch("agent.webhooks.common._is_repo_allowed", return_value=True),
        patch(
            "agent.webhooks.linear.persist_linear_thread_repo_config",
            new_callable=AsyncMock,
            side_effect=linear_service.LinearThreadRepoError("thread-1"),
        ),
        patch("agent.webhooks.linear.post_linear_routing_failure", post_failure),
    ):
        result, background_tasks = await _invoke(_payload("@openswe repo explicit/repo"))

    assert result == {
        "status": "ignored",
        "reason": "Failed to persist thread repository metadata",
    }
    background_tasks.add_task.assert_not_called()
    post_failure.assert_awaited_once_with(
        "issue-456",
        "comment-123",
        "Couldn't save the target repository due to a temporary service error. Please retry.",
    )


@pytest.mark.asyncio
async def test_bot_actor_comment_is_ignored_without_dispatch() -> None:
    payload = _payload()
    payload["data"]["botActor"] = {"id": "bot-1", "name": "Open SWE"}
    with patch("agent.webhooks.common.verify_linear_signature", return_value=True):
        result, background_tasks = await _invoke(payload)

    assert result == {"status": "ignored", "reason": "Comment is from a bot"}
    background_tasks.add_task.assert_not_called()


@pytest.mark.asyncio
async def test_non_mention_comment_remains_silent() -> None:
    post_failure = AsyncMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch("agent.webhooks.linear.post_linear_routing_failure", post_failure),
    ):
        result, background_tasks = await _invoke(_payload("please continue"))

    assert result == {"status": "ignored", "reason": "Comment doesn't mention @openswe"}
    background_tasks.add_task.assert_not_called()
    post_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_thread_repo_helper_extracts_persisted_metadata() -> None:
    client = SimpleNamespace(
        threads=SimpleNamespace(
            get=AsyncMock(return_value={"metadata": {"repo": {"owner": "stored", "name": "repo"}}})
        )
    )
    with (
        patch.object(
            linear_service.common, "generate_thread_id_from_issue", return_value="thread-1"
        ),
        patch.object(linear_service.common, "get_client", return_value=client),
    ):
        repo_config = await linear_service.get_linear_thread_repo_config("issue-456")

    assert repo_config == {"owner": "stored", "name": "repo"}
    client.threads.get.assert_awaited_once_with("thread-1")


@pytest.mark.asyncio
async def test_thread_repo_helper_rejects_missing_repo_metadata() -> None:
    client = SimpleNamespace(threads=SimpleNamespace(get=AsyncMock(return_value={"metadata": {}})))
    with (
        patch.object(
            linear_service.common, "generate_thread_id_from_issue", return_value="thread-1"
        ),
        patch.object(linear_service.common, "get_client", return_value=client),
        pytest.raises(linear_service.LinearThreadRepoError),
    ):
        await linear_service.get_linear_thread_repo_config("issue-456")


@pytest.mark.asyncio
async def test_thread_repo_helper_returns_none_only_for_not_found() -> None:
    not_found = _NotFoundError("not found")
    client = SimpleNamespace(threads=SimpleNamespace(get=AsyncMock(side_effect=not_found)))
    with (
        patch.object(
            linear_service.common, "generate_thread_id_from_issue", return_value="thread-1"
        ),
        patch.object(linear_service.common, "get_client", return_value=client),
    ):
        repo_config = await linear_service.get_linear_thread_repo_config("issue-456")

    assert repo_config is None


@pytest.mark.asyncio
async def test_thread_repo_helper_raises_on_lookup_error() -> None:
    client = SimpleNamespace(
        threads=SimpleNamespace(get=AsyncMock(side_effect=RuntimeError("unavailable")))
    )
    with (
        patch.object(
            linear_service.common, "generate_thread_id_from_issue", return_value="thread-1"
        ),
        patch.object(linear_service.common, "get_client", return_value=client),
        pytest.raises(linear_service.LinearThreadRepoError),
    ):
        await linear_service.get_linear_thread_repo_config("issue-456")


@pytest.mark.asyncio
async def test_thread_repo_lookup_error_does_not_use_fallbacks() -> None:
    profile_repo = AsyncMock(return_value={"owner": "profile", "name": "repo"})
    post_failure = AsyncMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch(
            "agent.webhooks.common.fetch_linear_issue_details",
            new_callable=AsyncMock,
            return_value=_full_issue(),
        ),
        patch(
            "agent.webhooks.linear.get_linear_thread_repo_config",
            new_callable=AsyncMock,
            side_effect=linear_service.LinearThreadRepoError("thread-1"),
        ),
        patch("agent.webhooks.common.get_profile_default_repo", profile_repo),
        patch("agent.webhooks.linear.post_linear_routing_failure", post_failure),
    ):
        result, background_tasks = await _invoke(_payload())

    assert result == {
        "status": "ignored",
        "reason": "Failed to access thread repository metadata",
    }
    profile_repo.assert_not_awaited()
    background_tasks.add_task.assert_not_called()
    post_failure.assert_awaited_once_with(
        "issue-456",
        "comment-123",
        "Couldn't safely read a repository from the existing thread. Retry or specify it "
        "as `repo owner/name` immediately after the agent mention.",
    )


@pytest.mark.asyncio
async def test_explicit_repo_persistence_updates_existing_thread() -> None:
    client = SimpleNamespace(threads=SimpleNamespace(update=AsyncMock(), create=AsyncMock()))
    with (
        patch.object(
            linear_service.common, "generate_thread_id_from_issue", return_value="thread-1"
        ),
        patch.object(linear_service.common, "get_client", return_value=client),
    ):
        await linear_service.persist_linear_thread_repo_config(
            "issue-456", {"owner": "explicit", "name": "repo"}
        )

    client.threads.update.assert_awaited_once_with(
        thread_id="thread-1",
        metadata={
            "repo": {"owner": "explicit", "name": "repo"},
            "repo_owner": "explicit",
            "repo_name": "repo",
        },
    )
    client.threads.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_repo_persistence_creates_missing_thread() -> None:
    not_found = _NotFoundError("not found")
    client = SimpleNamespace(
        threads=SimpleNamespace(
            update=AsyncMock(side_effect=[not_found, None]),
            create=AsyncMock(),
        )
    )
    repo_config = {"owner": "explicit", "name": "repo"}
    with (
        patch.object(
            linear_service.common, "generate_thread_id_from_issue", return_value="thread-1"
        ),
        patch.object(linear_service.common, "get_client", return_value=client),
    ):
        await linear_service.persist_linear_thread_repo_config("issue-456", repo_config)

    metadata = {
        "repo": repo_config,
        "repo_owner": "explicit",
        "repo_name": "repo",
    }
    client.threads.create.assert_awaited_once_with(
        thread_id="thread-1",
        if_exists="do_nothing",
        metadata=metadata,
    )
    assert client.threads.update.await_count == 2
    assert client.threads.update.await_args_list[-1].kwargs == {
        "thread_id": "thread-1",
        "metadata": metadata,
    }


@pytest.mark.asyncio
async def test_routing_failure_reply_is_guarded_and_mention_free() -> None:
    comment = AsyncMock(return_value=True)
    with patch.object(linear_service, "comment_on_linear_issue", comment):
        await linear_service.post_linear_routing_failure(
            "issue-456",
            "comment-123",
            "Couldn't determine the target repository. Specify it as `repo owner/name` "
            "immediately after the agent mention.",
        )

    comment.assert_awaited_once()
    issue_id, body = comment.call_args.args
    assert issue_id == "issue-456"
    assert body.startswith("❌ **Agent Error**")
    assert "@openswe" not in body.lower()
    assert comment.call_args.kwargs == {"parent_id": "comment-123"}


@pytest.mark.parametrize(
    ("body", "uses_explicit_repo"),
    [
        ("@openswe repo owner/name — Execute TEST-1 only.", True),
        (
            "@openswe repo owner/name — Execute one ticket bundle with primary TEST-1 "
            "and included tickets TEST-2.",
            True,
        ),
        (
            "@openswe Combined plan approved for TEST-1, TEST-2. Proceed with the one "
            "atomic bundle only.",
            False,
        ),
        ("@openswe Plan approved. Proceed with TEST-1 implementation only.", False),
        (
            "@openswe Plan not approved for TEST-1. Revise the plan and repost for review.",
            False,
        ),
        ("@openswe Status check on TEST-1: no visible progress for 30 minutes.", False),
        ("@openswe Review findings on PR 1 acknowledged for TEST-1.", False),
    ],
)
@pytest.mark.asyncio
async def test_shipped_template_shapes_dispatch(body: str, uses_explicit_repo: bool) -> None:
    thread_repo = AsyncMock(return_value={"owner": "stored", "name": "repo"})
    persist_repo = AsyncMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch("agent.webhooks.linear.get_linear_thread_repo_config", thread_repo),
        patch("agent.webhooks.linear.persist_linear_thread_repo_config", persist_repo),
        patch("agent.webhooks.common._is_repo_allowed", return_value=True),
    ):
        result, background_tasks = await _invoke(_payload(body))

    assert result["status"] == "accepted"
    expected_repo = (
        {"owner": "owner", "name": "name"}
        if uses_explicit_repo
        else {"owner": "stored", "name": "repo"}
    )
    if uses_explicit_repo:
        thread_repo.assert_not_awaited()
    else:
        thread_repo.assert_awaited_once_with("issue-456")
    persist_repo.assert_awaited_once_with("issue-456", expected_repo)
    assert background_tasks.add_task.call_args.args[2] == expected_repo


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("Please ask @openswe to continue", "@openswe mention is not at start of line"),
        ("Discuss `@openswe` safely", "@openswe mention is inside inline code"),
        (
            "```markdown\n@openswe repo owner/name — Execute TEST-1 only.\n```",
            "@openswe mention is inside a fenced code block",
        ),
    ],
)
@pytest.mark.asyncio
async def test_non_dispatch_mentions_have_distinct_reasons(
    body: str, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="agent.webhooks.common")
    with patch("agent.webhooks.common.verify_linear_signature", return_value=True):
        result, background_tasks = await _invoke(_payload(body))

    assert result == {"status": "ignored", "reason": reason}
    assert reason in caplog.text
    background_tasks.add_task.assert_not_called()


@pytest.mark.asyncio
async def test_mid_body_repo_prose_uses_persisted_thread_repo() -> None:
    persist_repo = AsyncMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch(
            "agent.webhooks.linear.get_linear_thread_repo_config",
            new_callable=AsyncMock,
            return_value={"owner": "stored", "name": "repo"},
        ),
        patch("agent.webhooks.linear.persist_linear_thread_repo_config", persist_repo),
        patch("agent.webhooks.common._is_repo_allowed", return_value=True),
    ):
        result, background_tasks = await _invoke(
            _payload("@openswe Plan approved.\nThe repo owner/name example is only prose.")
        )

    assert result["status"] == "accepted"
    expected_repo = {"owner": "stored", "name": "repo"}
    persist_repo.assert_awaited_once_with("issue-456", expected_repo)
    assert background_tasks.add_task.call_args.args[2] == expected_repo


_DELIVERY_ID = "4bc3ee15-6e86-4e33-9a5a-136342a6f43a"
_APP_USER_ID = "dc637f5e-8932-4c0d-bcbf-933f4092525c"


def _agent_session_payload(action: str = "created") -> dict:
    payload = {
        "type": "AgentSessionEvent",
        "action": action,
        "oauthClientId": "client-id",
        "organizationId": "org-1",
        "appUserId": _APP_USER_ID,
        "agentSession": {
            "id": "session-1",
            "organizationId": "org-1",
            "appUserId": _APP_USER_ID,
            "issueId": "issue-456",
            "comment": {"id": "comment-123", "body": "@openswe please help"},
            "creatorId": "user-1",
            "creator": {"id": "user-1", "name": "Test User"},
            "issue": {
                "id": "issue-456",
                "url": "https://linear.app/test/issue/TEST-1",
            },
        },
    }
    if action == "prompted":
        payload["agentActivity"] = {
            "id": "prompt-1",
            "agentSessionId": "session-1",
            "userId": "user-1",
            "user": {"id": "user-1", "name": "Test User"},
            "content": {"type": "prompt", "body": "Please continue"},
        }
    return payload


async def _invoke_agent_session(payload: dict, delivery_id: str = _DELIVERY_ID):
    request = AsyncMock()
    request.body.return_value = json.dumps(payload).encode()
    request.headers = {
        "Linear-Signature": "valid",
        "Linear-Delivery": delivery_id,
    }
    background_tasks = MagicMock()
    result = await linear_webhook(request, background_tasks)
    return result, background_tasks


@pytest.mark.parametrize("action", ["created", "prompted"])
async def test_agent_session_event_is_acknowledged_without_dispatch(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_CLIENT_ID", "client-id")
    monkeypatch.setenv("LINEAR_APP_USER_ID", _APP_USER_ID)
    acknowledge = AsyncMock()
    mention_classifier = MagicMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch("agent.webhooks.common.classify_comment_mention", mention_classifier),
        patch("agent.webhooks.linear.create_linear_agent_activity", acknowledge),
        patch(
            "agent.webhooks.linear.common.generate_thread_id_from_issue",
            return_value="thread-1",
        ),
        patch(
            "agent.webhooks.linear.common.dashboard_thread_url",
            return_value="https://openswe.example/agents/thread-1",
        ),
    ):
        result, background_tasks = await _invoke_agent_session(_agent_session_payload(action))

    assert result == {"status": "accepted", "message": "Agent session acknowledged"}
    background_tasks.add_task.assert_not_called()
    mention_classifier.assert_not_called()
    acknowledge.assert_awaited_once()
    assert acknowledge.call_args.args[:2] == ("session-1", _DELIVERY_ID)
    content = acknowledge.call_args.args[2]
    assert content["type"] == "thought"
    assert "https://linear.app/test/issue/TEST-1" in content["body"]
    assert "https://openswe.example/agents/thread-1" in content["body"]


async def test_duplicate_agent_session_delivery_reuses_activity_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_CLIENT_ID", "client-id")
    monkeypatch.setenv("LINEAR_APP_USER_ID", _APP_USER_ID)
    acknowledge = AsyncMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch("agent.webhooks.linear.create_linear_agent_activity", acknowledge),
    ):
        first, first_tasks = await _invoke_agent_session(_agent_session_payload())
        second, second_tasks = await _invoke_agent_session(_agent_session_payload())

    assert first["status"] == second["status"] == "accepted"
    assert [call.args[1] for call in acknowledge.await_args_list] == [_DELIVERY_ID, _DELIVERY_ID]
    first_tasks.add_task.assert_not_called()
    second_tasks.add_task.assert_not_called()


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda payload: payload.update(oauthClientId="other-client"),
            "Agent session does not belong to this app",
        ),
        (
            lambda payload: payload["agentSession"].update(appUserId="other-app"),
            "Agent session does not belong to this app",
        ),
        (
            lambda payload: (
                payload.update(appUserId="other-app"),
                payload["agentSession"].update(appUserId="other-app"),
            ),
            "Agent session does not belong to this app",
        ),
        (
            lambda payload: payload["agentSession"].update(issueId="other-issue"),
            "Malformed AgentSessionEvent payload",
        ),
        (
            lambda payload: payload["agentActivity"].update(agentSessionId="other-session"),
            "Agent activity does not belong to this session",
        ),
    ],
)
async def test_agent_session_wrong_app_or_session_is_ignored(
    mutate, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_CLIENT_ID", "client-id")
    monkeypatch.setenv("LINEAR_APP_USER_ID", _APP_USER_ID)
    payload = _agent_session_payload("prompted")
    mutate(payload)
    acknowledge = AsyncMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch("agent.webhooks.linear.create_linear_agent_activity", acknowledge),
    ):
        result, background_tasks = await _invoke_agent_session(payload)

    assert result == {"status": "ignored", "reason": reason}
    acknowledge.assert_not_awaited()
    background_tasks.add_task.assert_not_called()


async def test_agent_session_malformed_payload_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_CLIENT_ID", "client-id")
    monkeypatch.setenv("LINEAR_APP_USER_ID", _APP_USER_ID)
    acknowledge = AsyncMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch("agent.webhooks.linear.create_linear_agent_activity", acknowledge),
    ):
        result, background_tasks = await _invoke_agent_session(
            _agent_session_payload(), delivery_id="not-a-uuid"
        )

    assert result == {"status": "ignored", "reason": "Malformed AgentSessionEvent payload"}
    acknowledge.assert_not_awaited()
    background_tasks.add_task.assert_not_called()


@pytest.mark.parametrize("action", ["created", "prompted"])
async def test_agent_session_app_sender_is_ignored(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_CLIENT_ID", "client-id")
    monkeypatch.setenv("LINEAR_APP_USER_ID", _APP_USER_ID)
    payload = _agent_session_payload(action)
    if action == "created":
        payload["agentSession"]["creatorId"] = _APP_USER_ID
        payload["agentSession"]["creator"]["id"] = _APP_USER_ID
    else:
        payload["agentActivity"]["userId"] = _APP_USER_ID
        payload["agentActivity"]["user"]["id"] = _APP_USER_ID
    acknowledge = AsyncMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch("agent.webhooks.linear.create_linear_agent_activity", acknowledge),
    ):
        result, background_tasks = await _invoke_agent_session(payload)

    assert result == {"status": "ignored", "reason": "Agent session sender is not a human user"}
    acknowledge.assert_not_awaited()
    background_tasks.add_task.assert_not_called()


async def test_agent_session_signature_failure_is_rejected() -> None:
    request = AsyncMock()
    request.body.return_value = json.dumps(_agent_session_payload()).encode()
    request.headers = {
        "Linear-Signature": "invalid",
        "Linear-Delivery": _DELIVERY_ID,
    }
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=False),
        pytest.raises(HTTPException) as exc_info,
    ):
        await linear_webhook(request, MagicMock())

    assert exc_info.value.status_code == 401


async def test_agent_session_acknowledgement_failure_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_CLIENT_ID", "client-id")
    monkeypatch.setenv("LINEAR_APP_USER_ID", _APP_USER_ID)
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch(
            "agent.webhooks.linear.create_linear_agent_activity",
            new_callable=AsyncMock,
            side_effect=RuntimeError("activity failed"),
        ),
        pytest.raises(RuntimeError, match="activity failed"),
    ):
        await _invoke_agent_session(_agent_session_payload())


async def test_agent_session_without_comment_is_not_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_CLIENT_ID", "client-id")
    monkeypatch.setenv("LINEAR_APP_USER_ID", _APP_USER_ID)
    payload = _agent_session_payload()
    payload["agentSession"].pop("comment")
    acknowledge = AsyncMock()
    with (
        patch("agent.webhooks.common.verify_linear_signature", return_value=True),
        patch("agent.webhooks.linear.create_linear_agent_activity", acknowledge),
    ):
        result, background_tasks = await _invoke_agent_session(payload)

    assert result == {"status": "ignored", "reason": "Malformed AgentSessionEvent payload"}
    acknowledge.assert_not_awaited()
    background_tasks.add_task.assert_not_called()
