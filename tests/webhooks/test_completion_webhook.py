from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import completion, dispatch
from agent.api import health


class _FakeThreads:
    def __init__(self, metadata: dict[str, Any]) -> None:
        self._metadata = metadata
        self.updates: list[dict[str, Any]] = []

    async def get(self, thread_id: str) -> dict[str, Any]:
        return {"thread_id": thread_id, "metadata": self._metadata}

    async def update(self, *, thread_id: str, metadata: dict[str, Any]) -> None:
        self.updates.append(metadata)


class _FakeClient:
    def __init__(self, metadata: dict[str, Any]) -> None:
        self.threads = _FakeThreads(metadata)


class _JoinRuns:
    def __init__(self, payload: Any = None) -> None:
        self.join = AsyncMock(return_value=payload)


class _JoinClient(_FakeClient):
    def __init__(self, metadata: dict[str, Any], payload: Any = None) -> None:
        super().__init__(metadata)
        self.runs = _JoinRuns(payload)


class _GetRunRuns:
    def __init__(self, run: dict[str, Any]) -> None:
        self.get = AsyncMock(return_value=run)


class _GetRunClient(_FakeClient):
    def __init__(self, metadata: dict[str, Any], run: dict[str, Any]) -> None:
        super().__init__(metadata)
        self.runs = _GetRunRuns(run)


class _ListRuns:
    def __init__(self, runs: list[dict[str, Any]]) -> None:
        self.list = AsyncMock(return_value=runs)


class _ListClient(_FakeClient):
    def __init__(self, metadata: dict[str, Any], runs: list[dict[str, Any]]) -> None:
        super().__init__(metadata)
        self.runs = _ListRuns(runs)


def _slack_metadata() -> dict[str, Any]:
    return {
        "source": "slack",
        "source_context": {"slack_thread": {"channel_id": "C1", "thread_ts": "123.45"}},
    }


@pytest.mark.asyncio
async def test_error_status_posts_slack_failure_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(_slack_metadata())
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "post_slack_thread_reply", reply)
    monkeypatch.setattr(
        completion, "dashboard_thread_url", lambda thread_id: f"https://ui/{thread_id}"
    )

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "error"}
    )

    assert result["status"] == "ok"
    reply.assert_awaited_once()
    await_args = reply.await_args
    assert await_args is not None
    args = await_args.args
    assert args[0] == "C1"
    assert args[1] == "123.45"
    assert "<https://ui/t1|Open SWE Web>" in args[2]
    assert client.threads.updates == [
        {
            "failure_reply_posted_run_id": "run-1",
            "failure_reply_posted_run_ids": ["run-1"],
            "failure_streak": 1,
            "failure_streak_last_run_id": "run-1",
            "failure_streak_last_run_created_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_error_status_posts_terminal_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _JoinClient(
        _slack_metadata(),
        {
            "__error__": {
                "error": "BadRequestError",
                "message": "Error code: 400 - context_too_large",
            }
        },
    )
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "post_slack_thread_reply", reply)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "error"}
    )

    assert result["status"] == "ok"
    client.runs.join.assert_awaited_once_with("t1", "run-1")
    await_args = reply.await_args
    assert await_args is not None
    reply_text = await_args.args[2]
    assert "Cause: BadRequestError: Error code: 400 - context_too_large" in reply_text


@pytest.mark.asyncio
async def test_join_failure_keeps_generic_failure_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _JoinClient(_slack_metadata())
    client.runs.join.side_effect = RuntimeError("join failed")
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    monkeypatch.setattr(
        completion, "dashboard_thread_url", lambda thread_id: f"https://ui/{thread_id}"
    )
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "post_slack_thread_reply", reply)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "error"}
    )

    assert result["status"] == "ok"
    await_args = reply.await_args
    assert await_args is not None
    reply_text = await_args.args[2]
    assert reply_text == (
        "⚠️ I wasn't able to finish that — the run hit an unexpected error. "
        "Send another message and I'll pick it back up. "
        "You can view the error in <https://ui/t1|Open SWE Web>."
    )
    assert "Cause:" not in reply_text


@pytest.mark.asyncio
async def test_success_status_does_not_join_run(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _JoinClient(_slack_metadata())
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "success"}
    )

    assert result["status"] == "ignored"
    client.runs.join.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_failure_cause_collapses_whitespace_and_truncates() -> None:
    client = _JoinClient(
        {},
        {
            "__error__": {
                "error": "Bad\n  Request",
                "message": "\t detail   " + "x" * 400,
            }
        },
    )

    cause = await completion._run_failure_cause(client, "t1", "run-1")

    assert cause is not None
    assert cause == ("Bad Request: detail " + "x" * 400)[:300]
    assert len(cause) == 300


@pytest.mark.asyncio
async def test_run_failure_cause_returns_none_for_empty_error_and_message() -> None:
    client = _JoinClient({}, {"__error__": {"error": None, "message": None}})

    cause = await completion._run_failure_cause(client, "t1", "run-1")

    assert cause is None


@pytest.mark.asyncio
async def test_reviewer_error_settles_tracked_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_CHECK_BLOCKING", raising=False)
    metadata = {
        "kind": "reviewer",
        "review_check_run_id": 42,
        "pr": {"owner": "acme", "name": "widgets"},
        "source": "schedule",
    }
    client = _FakeClient(metadata)
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    monkeypatch.setattr(
        completion, "get_github_app_installation_token", AsyncMock(return_value="token")
    )
    settle = AsyncMock()
    monkeypatch.setattr(completion, "settle_review_check_run", settle)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "error"}
    )

    assert result["status"] == "ignored"
    settle.assert_awaited_once_with(
        thread_id="t1",
        owner="acme",
        repo="widgets",
        token="token",
        conclusion="neutral",
        title="Review did not complete",
        summary=(
            "The Open SWE review run ended without publishing a review. "
            "Re-trigger the review by pushing a commit or re-requesting it."
        ),
    )


@pytest.mark.asyncio
async def test_reviewer_error_settles_failure_when_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_CHECK_BLOCKING", "true")
    metadata = {
        "kind": "reviewer",
        "review_check_run_id": 42,
        "pr": {"owner": "acme", "name": "widgets"},
        "source": "schedule",
    }
    client = _FakeClient(metadata)
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    monkeypatch.setattr(
        completion, "get_github_app_installation_token", AsyncMock(return_value="token")
    )
    settle = AsyncMock()
    monkeypatch.setattr(completion, "settle_review_check_run", settle)

    await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "error"}
    )

    # A crashed reviewer must not satisfy a blocking (required) check.
    settle_args = settle.await_args
    assert settle_args is not None
    assert settle_args.kwargs["conclusion"] == "failure"


@pytest.mark.asyncio
async def test_reviewer_error_preserves_pending_check_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = {
        "kind": "reviewer",
        "review_check_run_id": 42,
        "review_check_pending_result": {
            "conclusion": "success",
            "title": "Found 1 potential issue",
            "summary": "Open SWE surfaced 1 potential issue.",
        },
        "pr": {"owner": "acme", "name": "widgets"},
        "source": "schedule",
    }
    client = _FakeClient(metadata)
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    monkeypatch.setattr(
        completion, "get_github_app_installation_token", AsyncMock(return_value="token")
    )
    settle = AsyncMock()
    monkeypatch.setattr(completion, "settle_review_check_run", settle)

    await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "error"}
    )

    settle.assert_awaited_once_with(
        thread_id="t1",
        owner="acme",
        repo="widgets",
        token="token",
        conclusion="success",
        title="Found 1 potential issue",
        summary="Open SWE surfaced 1 potential issue.",
    )


@pytest.mark.asyncio
async def test_ordinary_agent_error_does_not_settle_review_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _slack_metadata()
    metadata["review_check_run_id"] = 42
    client = _FakeClient(metadata)
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    monkeypatch.setattr(completion, "post_slack_thread_reply", AsyncMock(return_value=True))
    token = AsyncMock(return_value="token")
    settle = AsyncMock()
    monkeypatch.setattr(completion, "get_github_app_installation_token", token)
    monkeypatch.setattr(completion, "settle_review_check_run", settle)

    await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "error"}
    )

    token.assert_not_awaited()
    settle.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata, token",
    [
        ({"kind": "reviewer", "pr": {"owner": "acme", "name": "widgets"}}, "token"),
        ({"kind": "reviewer", "review_check_run_id": 42}, "token"),
        (
            {
                "kind": "reviewer",
                "review_check_run_id": 42,
                "pr": {"owner": "acme", "name": "widgets"},
            },
            None,
        ),
    ],
)
async def test_reviewer_cleanup_skips_missing_metadata_or_token(
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict[str, Any],
    token: str | None,
) -> None:
    client = _FakeClient(metadata | {"source": "schedule"})
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    monkeypatch.setattr(
        completion, "get_github_app_installation_token", AsyncMock(return_value=token)
    )
    settle = AsyncMock()
    monkeypatch.setattr(completion, "settle_review_check_run", settle)

    await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "timeout"}
    )

    settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_reviewer_cleanup_failure_does_not_block_failure_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _slack_metadata() | {
        "kind": "reviewer",
        "review_check_run_id": 42,
        "pr": {"owner": "acme", "name": "widgets"},
    }
    client = _FakeClient(metadata)
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    monkeypatch.setattr(
        completion, "get_github_app_installation_token", AsyncMock(return_value="token")
    )
    monkeypatch.setattr(
        completion, "settle_review_check_run", AsyncMock(side_effect=RuntimeError("boom"))
    )
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "post_slack_thread_reply", reply)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "error"}
    )

    assert result["status"] == "ok"
    reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_schedule_source_with_slack_context_posts_failure_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _slack_metadata()
    metadata["source"] = "schedule"
    client = _FakeClient(metadata)
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "post_slack_thread_reply", reply)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "error"}
    )

    assert result["status"] == "ok"
    reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_success_status_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(_slack_metadata())
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "post_slack_thread_reply", reply)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "success"}
    )

    assert result["status"] == "ignored"
    reply.assert_not_called()


@pytest.mark.asyncio
async def test_idempotent_when_already_replied(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = _slack_metadata()
    metadata["failure_reply_posted_run_ids"] = ["run-1"]
    client = _FakeClient(metadata)
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "post_slack_thread_reply", reply)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "timeout"}
    )

    assert result["status"] == "ignored"
    reply.assert_not_called()
    assert client.threads.updates == []


@pytest.mark.asyncio
async def test_later_failed_run_posts_even_if_prior_run_replied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _slack_metadata()
    metadata["failure_reply_posted_run_ids"] = ["run-1"]
    client = _FakeClient(metadata)
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "post_slack_thread_reply", reply)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-2", "status": "timeout"}
    )

    assert result["status"] == "ok"
    reply.assert_awaited_once()
    assert client.threads.updates == [
        {
            "failure_reply_posted_run_id": "run-2",
            "failure_reply_posted_run_ids": ["run-1", "run-2"],
            "failure_streak": 1,
            "failure_streak_last_run_id": "run-2",
            "failure_streak_last_run_created_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_consecutive_failures_escalate_on_second_distinct_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_client = _FakeClient(_slack_metadata())
    monkeypatch.setattr(completion, "langgraph_client", lambda: first_client)
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "post_slack_thread_reply", reply)

    first_result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "error"}
    )

    assert first_result["status"] == "ok"
    first_await_args = reply.await_args
    assert first_await_args is not None
    first_text = first_await_args.args[2]
    assert "consecutive runs" not in first_text

    metadata = _slack_metadata()
    metadata.update(
        {
            "failure_reply_posted_run_id": "run-1",
            "failure_reply_posted_run_ids": ["run-1"],
            "failure_streak": 1,
            "failure_streak_last_run_id": "run-1",
        }
    )
    second_client = _JoinClient(
        metadata,
        {"__error__": {"error": "BadRequestError", "message": "poisoned context"}},
    )
    monkeypatch.setattr(completion, "langgraph_client", lambda: second_client)
    reply.reset_mock()

    second_result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-2", "status": "error"}
    )

    assert second_result["status"] == "ok"
    second_await_args = reply.await_args
    assert second_await_args is not None
    second_text = second_await_args.args[2]
    assert "2 consecutive runs" in second_text
    assert "Cause: BadRequestError: poisoned context." in second_text
    assert second_client.threads.updates == [
        {
            "failure_reply_posted_run_id": "run-2",
            "failure_reply_posted_run_ids": ["run-1", "run-2"],
            "failure_streak": 2,
            "failure_streak_last_run_id": "run-2",
            "failure_streak_last_run_created_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_same_run_id_redelivery_does_not_increment_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _slack_metadata()
    metadata.update(
        {
            "failure_reply_posted_run_ids": ["run-1"],
            "failure_streak": 1,
            "failure_streak_last_run_id": "run-1",
        }
    )
    client = _FakeClient(metadata)
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "post_slack_thread_reply", reply)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "error"}
    )

    assert result == {"status": "ignored", "reason": "failure reply already posted for run"}
    reply.assert_not_called()
    assert client.threads.updates == []


@pytest.mark.asyncio
async def test_failure_increment_stores_created_at_and_success_reset_clears_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _slack_metadata()
    client = _GetRunClient(metadata, {"run_id": "run-f", "created_at": "2026-01-01T00:00:05Z"})
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    monkeypatch.setattr(completion, "post_slack_thread_reply", AsyncMock(return_value=True))

    failed_result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-f", "status": "error"}
    )

    assert failed_result["status"] == "ok"
    assert client.threads.updates == [
        {
            "failure_reply_posted_run_id": "run-f",
            "failure_reply_posted_run_ids": ["run-f"],
            "failure_streak": 1,
            "failure_streak_last_run_id": "run-f",
            "failure_streak_last_run_created_at": "2026-01-01T00:00:05Z",
        }
    ]

    metadata.update(client.threads.updates[-1])
    client.threads.updates.clear()
    client.runs.get.return_value = {
        "run_id": "run-s",
        "created_at": "2026-01-01T00:00:10Z",
    }

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-s", "status": "success"}
    )

    assert result["status"] == "ignored"
    assert client.threads.updates == [
        {
            "failure_streak": 0,
            "failure_streak_last_run_id": None,
            "failure_streak_last_run_created_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_success_older_than_counted_failure_does_not_reset_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _slack_metadata()
    metadata.update(
        {
            "failure_streak": 3,
            "failure_streak_last_run_id": "run-f",
            "failure_streak_last_run_created_at": "2026-01-01T00:00:10Z",
        }
    )
    client = _GetRunClient(metadata, {"run_id": "run-a", "created_at": "2026-01-01T00:00:05Z"})
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-a", "status": "success"}
    )

    assert result["status"] == "ignored"
    client.runs.get.assert_awaited_once_with("t1", "run-a")
    assert client.threads.updates == []


@pytest.mark.asyncio
async def test_success_newer_than_counted_failure_resets_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _slack_metadata()
    metadata.update(
        {
            "failure_streak": 3,
            "failure_streak_last_run_id": "run-f",
            "failure_streak_last_run_created_at": "2026-01-01T00:00:05Z",
        }
    )
    client = _GetRunClient(metadata, {"run_id": "run-a", "created_at": "2026-01-01T00:00:10Z"})
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-a", "status": "success"}
    )

    assert result["status"] == "ignored"
    assert client.threads.updates == [
        {
            "failure_streak": 0,
            "failure_streak_last_run_id": None,
            "failure_streak_last_run_created_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_success_reset_fails_open_when_created_at_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _slack_metadata()
    metadata.update(
        {
            "failure_streak": 3,
            "failure_streak_last_run_id": "run-f",
            "failure_streak_last_run_created_at": "2026-01-01T00:00:05Z",
        }
    )
    client = _GetRunClient(metadata, {})
    client.runs.get.side_effect = RuntimeError("run unavailable")
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-3", "status": "success"}
    )

    assert result["status"] == "ignored"
    assert client.threads.updates == [
        {
            "failure_streak": 0,
            "failure_streak_last_run_id": None,
            "failure_streak_last_run_created_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_success_with_zero_streak_does_not_write_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(_slack_metadata())
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "success"}
    )

    assert result["status"] == "ignored"
    assert client.threads.updates == []


@pytest.mark.asyncio
async def test_linear_source_comments_on_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient({"source": "linear", "source_context": {"linear_issue": {"id": "iss_1"}}})
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    comment = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "comment_on_linear_issue", comment)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "timeout"}
    )

    assert result["status"] == "ok"
    comment.assert_awaited_once()
    await_args = comment.await_args
    assert await_args is not None
    assert await_args.args[0] == "iss_1"


@pytest.mark.asyncio
async def test_missing_thread_id_is_ignored() -> None:
    result = await completion.handle_run_completion({"run_id": "run-1", "status": "error"})
    assert result["status"] == "ignored"


@pytest.mark.asyncio
async def test_missing_run_id_falls_back_to_thread_level_dedupe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(_slack_metadata())
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "post_slack_thread_reply", reply)

    result = await completion.handle_run_completion({"thread_id": "t1", "status": "error"})

    assert result["status"] == "ok"
    reply.assert_awaited_once()
    assert client.threads.updates == [{"failure_reply_posted": True}]


@pytest.mark.asyncio
async def test_missing_run_id_respects_thread_level_dedupe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _slack_metadata()
    metadata["failure_reply_posted"] = True
    client = _FakeClient(metadata)
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "post_slack_thread_reply", reply)

    result = await completion.handle_run_completion({"thread_id": "t1", "status": "error"})

    assert result["status"] == "ignored"
    reply.assert_not_called()
    assert client.threads.updates == []


@pytest.mark.asyncio
async def test_no_reply_channel_does_not_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient({"source": "schedule"})
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "error"}
    )

    assert result["status"] == "ignored"
    assert client.threads.updates == []


@pytest.mark.asyncio
async def test_interrupted_status_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    # Follow-ups use multitask_strategy="interrupt", so an interrupted run is a
    # healthy hand-off, not a failure to report.
    client = _FakeClient(_slack_metadata())
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "post_slack_thread_reply", reply)

    result = await completion.handle_run_completion(
        {"thread_id": "t1", "run_id": "run-1", "status": "interrupted"}
    )

    assert result["status"] == "ignored"
    reply.assert_not_called()
    assert client.threads.updates == []


def test_verify_run_complete_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # No secret configured: fail closed (reject everything).
    monkeypatch.setattr(completion, "RUN_COMPLETE_WEBHOOK_SECRET", None)
    assert completion.verify_run_complete_token(None) is False
    assert completion.verify_run_complete_token("whatever") is False

    # Secret configured: require an exact match.
    monkeypatch.setattr(completion, "RUN_COMPLETE_WEBHOOK_SECRET", "s3cret")
    assert completion.verify_run_complete_token("s3cret") is True
    assert completion.verify_run_complete_token("wrong") is False
    assert completion.verify_run_complete_token(None) is False


def test_run_complete_route_decodes_encoded_token(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = 's3cret& with "quote"'
    monkeypatch.setattr(completion, "RUN_COMPLETE_WEBHOOK_SECRET", secret)
    handle_completion = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(health, "handle_run_completion", handle_completion)
    app = FastAPI()
    app.include_router(health.router)
    webhook_url = dispatch._resolve_completion_webhook_url(
        "https://example.test/webhooks/run-complete", secret
    )
    assert webhook_url is not None

    response = TestClient(app).post(webhook_url, json={"status": "success"})

    assert response.status_code == 200
    assert completion.verify_run_complete_token(secret) is True
    handle_completion.assert_awaited_once_with({"status": "success"})


class _DeferredThreads:
    def __init__(self, records: dict[str, dict[str, Any]]) -> None:
        self.records = records
        self.updates: list[tuple[str, dict[str, Any]]] = []

    async def get(self, thread_id: str) -> dict[str, Any]:
        return self.records[thread_id]

    async def update(self, *, thread_id: str, metadata: dict[str, Any]) -> None:
        self.updates.append((thread_id, metadata))


class _DeferredRuns:
    def __init__(self, runs: list[dict[str, Any]]) -> None:
        self._runs = runs

    async def list(self, thread_id: str, limit: int = 1) -> list[dict[str, Any]]:
        return self._runs[:limit]


class _DeferredClient:
    def __init__(self, records: dict[str, dict[str, Any]], runs: list[dict[str, Any]]) -> None:
        self.threads = _DeferredThreads(records)
        self.runs = _DeferredRuns(runs)


def _deferred_client(
    *, findings: list[dict[str, Any]], latest_status: str, latest_run_id: str = "run-fix"
) -> tuple[_DeferredClient, str]:
    from agent.utils.thread_ids import generate_reviewer_thread_id

    reviewer_thread_id = generate_reviewer_thread_id("acme", "widgets", 77)
    deferred = {
        "implementation_thread_id": "implementation-thread",
        "implementation_run_id": "run-fix",
        "review_check_run_id": 42,
        "head_sha": "held-head",
        "pr_number": 77,
        "conclusion": "success",
        "title": "No issues found",
        "summary": "Open SWE reviewed this pull request and found no issues.",
    }
    records = {
        "implementation-thread": {
            "thread_id": "implementation-thread",
            "metadata": {
                "repo": {"owner": "acme", "name": "widgets"},
                "pr_number": 77,
            },
        },
        reviewer_thread_id: {
            "thread_id": reviewer_thread_id,
            "metadata": {
                "kind": "reviewer",
                "review_check_run_id": 42,
                "review_check_deferred_result": deferred,
                "pr": {"owner": "acme", "name": "widgets", "number": 77},
                "findings": findings,
            },
        },
    }
    return _DeferredClient(
        records, [{"run_id": latest_run_id, "status": latest_status}]
    ), reviewer_thread_id


@pytest.mark.asyncio
async def test_successful_decline_only_run_releases_deferred_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, reviewer_thread_id = _deferred_client(
        findings=[{"id": "f_declined", "status": "dismissed", "surface": {"state": "resolved"}}],
        latest_status="success",
    )
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    monkeypatch.setattr(
        completion, "get_github_app_installation_token", AsyncMock(return_value="token")
    )
    monkeypatch.setattr(
        completion, "fetch_pull_request_head_sha", AsyncMock(return_value="held-head")
    )
    settle = AsyncMock()
    monkeypatch.setattr(completion, "settle_review_check_run", settle)

    result = await completion.handle_run_completion(
        {"thread_id": "implementation-thread", "run_id": "run-fix", "status": "success"}
    )

    assert result == {"status": "ok", "reason": "deferred review check settled"}
    settle.assert_awaited_once_with(
        thread_id=reviewer_thread_id,
        owner="acme",
        repo="widgets",
        token="token",
        conclusion="success",
        title="No issues found",
        summary="Open SWE reviewed this pull request and found no issues.",
        expected_check_run_id=42,
    )


@pytest.mark.asyncio
async def test_unchanged_head_with_open_fix_fails_deferred_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_CHECK_BLOCKING", "true")
    client, reviewer_thread_id = _deferred_client(
        findings=[
            {
                "id": "f_fix_now",
                "status": "open",
                "surface": {"state": "surfaced", "surfaced_at_sha": "held-head"},
            }
        ],
        latest_status="success",
    )
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    monkeypatch.setattr(
        completion, "get_github_app_installation_token", AsyncMock(return_value="token")
    )
    monkeypatch.setattr(
        completion, "fetch_pull_request_head_sha", AsyncMock(return_value="held-head")
    )
    settle = AsyncMock()
    monkeypatch.setattr(completion, "settle_review_check_run", settle)

    await completion.handle_run_completion(
        {"thread_id": "implementation-thread", "run_id": "run-fix", "status": "success"}
    )

    settle_args = settle.await_args
    assert settle_args is not None
    assert settle_args.kwargs["thread_id"] == reviewer_thread_id
    assert settle_args.kwargs["conclusion"] == "failure"
    assert settle_args.kwargs["title"] == "Found 1 potential issue"


@pytest.mark.asyncio
async def test_interrupted_run_does_not_release_hold_while_replacement_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _reviewer_thread_id = _deferred_client(
        findings=[], latest_status="running", latest_run_id="replacement-run"
    )
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    token = AsyncMock(return_value="token")
    settle = AsyncMock()
    monkeypatch.setattr(completion, "get_github_app_installation_token", token)
    monkeypatch.setattr(completion, "settle_review_check_run", settle)

    result = await completion.handle_run_completion(
        {"thread_id": "implementation-thread", "run_id": "run-fix", "status": "interrupted"}
    )

    assert result["status"] == "ignored"
    token.assert_not_awaited()
    settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_head_supersedes_deferred_check_without_settling_current_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, reviewer_thread_id = _deferred_client(findings=[], latest_status="success")
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    monkeypatch.setattr(
        completion, "get_github_app_installation_token", AsyncMock(return_value="token")
    )
    monkeypatch.setattr(
        completion, "fetch_pull_request_head_sha", AsyncMock(return_value="new-head")
    )
    settle = AsyncMock()
    monkeypatch.setattr(completion, "settle_review_check_run", settle)

    result = await completion.handle_run_completion(
        {"thread_id": "implementation-thread", "run_id": "run-fix", "status": "success"}
    )

    assert result["status"] == "ok"
    settle.assert_not_awaited()
    assert client.threads.updates == []


@pytest.mark.asyncio
async def test_replaced_review_check_does_not_clear_newer_deferred_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, reviewer_thread_id = _deferred_client(findings=[], latest_status="success")
    client.threads.records[reviewer_thread_id]["metadata"]["review_check_run_id"] = 43
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    token = AsyncMock(return_value="token")
    settle = AsyncMock()
    monkeypatch.setattr(completion, "get_github_app_installation_token", token)
    monkeypatch.setattr(completion, "settle_review_check_run", settle)

    result = await completion.handle_run_completion(
        {"thread_id": "implementation-thread", "run_id": "run-fix", "status": "success"}
    )

    assert result["status"] == "ignored"
    token.assert_not_awaited()
    settle.assert_not_awaited()
    assert client.threads.updates == []


@pytest.mark.asyncio
async def test_deferred_settlement_failure_does_not_block_failure_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(_slack_metadata())
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    monkeypatch.setattr(
        completion,
        "settle_deferred_review_check",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(completion, "post_slack_thread_reply", reply)

    with pytest.raises(RuntimeError, match="Deferred review check settlement failed"):
        await completion.handle_run_completion(
            {"thread_id": "t1", "run_id": "run-1", "status": "error"}
        )

    reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_completion_does_not_settle_replacement_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, reviewer_thread_id = _deferred_client(findings=[], latest_status="success")
    client.threads.records[reviewer_thread_id]["metadata"]["review_check_run_id"] = 99
    monkeypatch.setattr(completion, "langgraph_client", lambda: client)
    token = AsyncMock(return_value="token")
    settle = AsyncMock()
    monkeypatch.setattr(completion, "get_github_app_installation_token", token)
    monkeypatch.setattr(completion, "settle_review_check_run", settle)

    result = await completion.handle_run_completion(
        {"thread_id": "implementation-thread", "run_id": "run-fix", "status": "success"}
    )

    assert result["status"] == "ignored"
    token.assert_not_awaited()
    settle.assert_not_awaited()
    assert client.threads.updates == []
