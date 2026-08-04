from __future__ import annotations

from typing import Any

import pytest

from agent.utils import thread_ops


class _StubThreads:
    def __init__(self, thread: dict[str, Any] | None = None, *, missing: bool = False) -> None:
        self.thread = thread
        self.missing = missing
        self.calls: list[tuple[str, Any]] = []

    async def get(self, thread_id: str) -> dict[str, Any] | None:
        if self.missing:
            raise RuntimeError("missing")
        return self.thread

    async def delete(self, thread_id: str) -> None:
        self.calls.append(("delete", thread_id))

    async def create(self, *, thread_id: str, metadata: dict[str, Any]) -> None:
        self.calls.append(("create", {"thread_id": thread_id, "metadata": metadata}))


class _StubClient:
    def __init__(self, threads: _StubThreads) -> None:
        self.threads = threads


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
    }
    threads = _StubThreads({"thread_id": "tid", "metadata": metadata})

    result = await thread_ops.reset_thread_preserving_metadata("tid", client=_StubClient(threads))

    preserved = {
        "source": "slack",
        "source_context": {"slack_thread": {"channel_id": "C1"}},
        "plan": {"status": "approved"},
    }
    assert result == {
        "thread_id": "tid",
        "preserved_keys": ["plan", "source", "source_context"],
        "dropped_keys": [
            "failure_reply_posted",
            "failure_reply_posted_run_id",
            "failure_reply_posted_run_ids",
            "failure_streak",
            "failure_streak_last_run_id",
        ],
    }
    assert threads.calls == [
        ("delete", "tid"),
        ("create", {"thread_id": "tid", "metadata": preserved}),
    ]


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
