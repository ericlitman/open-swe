from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent.utils import thread_ops


class _StubThreads:
    def __init__(
        self,
        thread: dict[str, Any] | None = None,
        *,
        missing: bool = False,
        call_order: list[str] | None = None,
    ) -> None:
        self.thread = thread
        self.missing = missing
        self.call_order = call_order
        self.calls: list[tuple[str, Any]] = []

    async def get(self, thread_id: str) -> dict[str, Any] | None:
        if self.missing:
            raise RuntimeError("missing")
        return self.thread

    async def delete(self, thread_id: str) -> None:
        if self.call_order is not None:
            self.call_order.append("delete")
        self.calls.append(("delete", thread_id))

    async def create(self, *, thread_id: str, metadata: dict[str, Any]) -> None:
        if self.call_order is not None:
            self.call_order.append("create")
        self.calls.append(("create", {"thread_id": thread_id, "metadata": metadata}))


class _StubRuns:
    def __init__(
        self,
        runs: list[dict[str, Any]] | None = None,
        *,
        cancel_error: Exception | None = None,
        call_order: list[str] | None = None,
    ) -> None:
        self._runs = runs or []
        self.cancel_error = cancel_error
        self.call_order = call_order
        self.calls: list[tuple[str, Any]] = []

    async def list(self, thread_id: str, *, limit: int) -> list[dict[str, Any]]:
        if self.call_order is not None:
            self.call_order.append("list")
        self.calls.append(("list", {"thread_id": thread_id, "limit": limit}))
        return self._runs[:limit]

    async def cancel(self, thread_id: str, run_id: str, *, wait: bool = False) -> None:
        if self.call_order is not None:
            self.call_order.append("cancel")
        self.calls.append(("cancel", (thread_id, run_id, wait)))
        if self.cancel_error is not None:
            raise self.cancel_error


class _StubClient:
    def __init__(self, threads: _StubThreads, runs: _StubRuns | None = None) -> None:
        self.threads = threads
        self.runs = runs or _StubRuns()


@pytest.mark.asyncio
async def test_reset_thread_preserves_metadata_and_drops_failure_tracking() -> None:
    metadata = {
        "source": "slack",
        "source_context": {"slack_thread": {"channel_id": "C1"}},
        "plan": {"status": "approved"},
        "failure_reply_posted": True,
        "failure_reply_posted_run_id": "run-2",
        "failure_reply_posted_run_ids": ["run-1", "run-2"],
        "failure_streak": 2,
        "failure_streak_last_run_id": "run-2",
        "failure_streak_last_run_created_at": "2026-01-01T00:00:05Z",
        "latest_run_status": "running",
    }
    threads = _StubThreads({"thread_id": "tid", "metadata": metadata})

    result = await thread_ops.reset_thread_preserving_metadata("tid", client=_StubClient(threads))

    preserved = {
        "source": "slack",
        "source_context": {"slack_thread": {"channel_id": "C1"}},
        "plan": {"status": "approved"},
        "failure_reply_posted": True,
        "failure_reply_posted_run_id": "run-2",
        "failure_reply_posted_run_ids": ["run-1", "run-2"],
    }
    assert result == {
        "thread_id": "tid",
        "preserved_keys": [
            "failure_reply_posted",
            "failure_reply_posted_run_id",
            "failure_reply_posted_run_ids",
            "plan",
            "source",
            "source_context",
        ],
        "dropped_keys": [
            "failure_streak",
            "failure_streak_last_run_created_at",
            "failure_streak_last_run_id",
            "latest_run_status",
        ],
    }
    assert threads.calls == [
        ("delete", "tid"),
        ("create", {"thread_id": "tid", "metadata": preserved}),
    ]


@pytest.mark.asyncio
async def test_reset_thread_preserves_failure_reply_dedupe_and_drops_run_status_keys() -> None:
    metadata = {
        "source": "slack",
        "failure_reply_posted": True,
        "failure_reply_posted_run_id": "run-2",
        "failure_reply_posted_run_ids": ["run-1", "run-2"],
        "failure_streak": 2,
        "failure_streak_last_run_id": "run-2",
        "latest_run_id": "run-2",
        "latest_run_status": "running",
    }
    threads = _StubThreads({"thread_id": "tid", "metadata": metadata})

    result = await thread_ops.reset_thread_preserving_metadata("tid", client=_StubClient(threads))

    preserved = {
        "source": "slack",
        "failure_reply_posted": True,
        "failure_reply_posted_run_id": "run-2",
        "failure_reply_posted_run_ids": ["run-1", "run-2"],
    }
    assert result == {
        "thread_id": "tid",
        "preserved_keys": [
            "failure_reply_posted",
            "failure_reply_posted_run_id",
            "failure_reply_posted_run_ids",
            "source",
        ],
        "dropped_keys": [
            "failure_streak",
            "failure_streak_last_run_id",
            "latest_run_id",
            "latest_run_status",
        ],
    }
    assert threads.calls[-1] == ("create", {"thread_id": "tid", "metadata": preserved})


@pytest.mark.asyncio
async def test_reset_cancels_running_latest_run_before_delete() -> None:
    call_order: list[str] = []
    threads = _StubThreads(
        {"thread_id": "tid", "metadata": {"source": "slack"}}, call_order=call_order
    )
    runs = _StubRuns([{"run_id": "run-9", "status": "running"}], call_order=call_order)

    await thread_ops.reset_thread_preserving_metadata("tid", client=_StubClient(threads, runs))

    assert runs.calls == [
        ("list", {"thread_id": "tid", "limit": 1}),
        ("cancel", ("tid", "run-9", True)),
    ]
    assert call_order == ["list", "cancel", "delete", "create"]


@pytest.mark.asyncio
async def test_reset_continues_when_active_run_cancel_fails() -> None:
    threads = _StubThreads({"thread_id": "tid", "metadata": {"source": "slack"}})
    runs = _StubRuns(
        [{"id": "run-9", "status": "pending"}], cancel_error=RuntimeError("cancel failed")
    )

    result = await thread_ops.reset_thread_preserving_metadata(
        "tid", client=_StubClient(threads, runs)
    )

    assert result["thread_id"] == "tid"
    assert ("cancel", ("tid", "run-9", True)) in runs.calls
    assert ("delete", "tid") in threads.calls


@pytest.mark.asyncio
async def test_reset_continues_when_active_run_cancel_times_out() -> None:
    threads = _StubThreads({"thread_id": "tid", "metadata": {"source": "slack"}})
    runs = _StubRuns(
        [{"id": "run-9", "status": "pending"}],
        cancel_error=asyncio.TimeoutError(),  # noqa: UP041
    )

    result = await thread_ops.reset_thread_preserving_metadata(
        "tid", client=_StubClient(threads, runs)
    )

    assert result["thread_id"] == "tid"
    assert ("cancel", ("tid", "run-9", True)) in runs.calls
    assert ("delete", "tid") in threads.calls
    assert (
        "create",
        {"thread_id": "tid", "metadata": {"source": "slack"}},
    ) in threads.calls


@pytest.mark.asyncio
async def test_reset_does_not_cancel_finished_latest_run() -> None:
    threads = _StubThreads({"thread_id": "tid", "metadata": {"source": "slack"}})
    runs = _StubRuns([{"run_id": "run-9", "status": "success"}])

    await thread_ops.reset_thread_preserving_metadata("tid", client=_StubClient(threads, runs))

    assert runs.calls == [("list", {"thread_id": "tid", "limit": 1})]
    assert ("delete", "tid") in threads.calls


@pytest.mark.asyncio
async def test_reset_thread_raises_for_missing_thread() -> None:
    client = _StubClient(_StubThreads(missing=True))

    with pytest.raises(ValueError, match="Thread tid does not exist"):
        await thread_ops.reset_thread_preserving_metadata("tid", client=client)


@pytest.mark.asyncio
async def test_reset_thread_uses_default_client(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = _StubThreads({"thread_id": "tid", "metadata": {"source": "dashboard"}})
    client = _StubClient(threads)
    monkeypatch.setattr(thread_ops, "langgraph_client", lambda: client)

    result = await thread_ops.reset_thread_preserving_metadata("tid")

    assert result["preserved_keys"] == ["source"]
    assert threads.calls == [
        ("delete", "tid"),
        ("create", {"thread_id": "tid", "metadata": {"source": "dashboard"}}),
    ]
