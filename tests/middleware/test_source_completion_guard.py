from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from agent.middleware import source_completion_guard as guard


class _Request:
    def __init__(self, name: str, args: dict[str, Any] | None = None) -> None:
        self.tool_call = {"name": name, "args": args or {}, "id": f"call-{name}"}


class _Threads:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def update(self, *, thread_id: str, metadata: dict[str, Any]) -> None:
        self.updates.append({"thread_id": thread_id, "metadata": metadata})


class _Client:
    def __init__(self) -> None:
        self.threads = _Threads()


def _result(name: str, payload: dict[str, Any], *, error: bool = False) -> ToolMessage:
    return ToolMessage(
        content=json.dumps(payload),
        name=name,
        tool_call_id=f"call-{name}",
        status="error" if error else "success",
    )


async def _call(
    middleware: guard.SourceCompletionGuardMiddleware,
    name: str,
    payload: dict[str, Any],
    *,
    error: bool = False,
) -> None:
    async def handler(_request: ToolCallRequest) -> ToolMessage:
        return _result(name, payload, error=error)

    _ = await middleware.awrap_tool_call(
        cast(ToolCallRequest, cast(object, _Request(name))), handler
    )


@pytest.mark.asyncio
async def test_missing_linear_reply_is_recorded_after_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    monkeypatch.setattr(guard, "get_client", lambda: client)
    middleware = guard.SourceCompletionGuardMiddleware(thread_id="thread-1", source="linear")
    await _call(
        middleware,
        "open_pull_request",
        {"success": True, "number": 62, "url": "https://github.com/o/r/pull/62"},
    )

    _ = await middleware.aafter_agent(cast(AgentState, cast(object, {"messages": []})), MagicMock())

    assert client.threads.updates == [
        {
            "thread_id": "thread-1",
            "metadata": {
                "source_completion_reply_missing": {
                    "source": "linear",
                    "pr_number": 62,
                    "pr_url": "https://github.com/o/r/pull/62",
                }
            },
        }
    ]


@pytest.mark.asyncio
async def test_successful_linear_reply_after_pr_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    monkeypatch.setattr(guard, "get_client", lambda: client)
    middleware = guard.SourceCompletionGuardMiddleware(thread_id="thread-1", source="linear")
    await _call(
        middleware,
        "open_pull_request",
        {"success": True, "number": 62, "url": "https://github.com/o/r/pull/62"},
    )
    await _call(middleware, "linear_comment", {"success": True})

    _ = await middleware.aafter_agent(cast(AgentState, cast(object, {"messages": []})), MagicMock())

    assert client.threads.updates == []


@pytest.mark.asyncio
async def test_linear_reply_before_pr_does_not_satisfy_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    monkeypatch.setattr(guard, "get_client", lambda: client)
    middleware = guard.SourceCompletionGuardMiddleware(thread_id="thread-1", source="linear")
    await _call(middleware, "linear_comment", {"success": True})
    await _call(
        middleware,
        "open_pull_request",
        {"success": True, "number": 62, "url": "https://github.com/o/r/pull/62"},
    )

    _ = await middleware.aafter_agent(cast(AgentState, cast(object, {"messages": []})), MagicMock())

    assert len(client.threads.updates) == 1


@pytest.mark.asyncio
async def test_failed_linear_reply_after_pr_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    monkeypatch.setattr(guard, "get_client", lambda: client)
    middleware = guard.SourceCompletionGuardMiddleware(thread_id="thread-1", source="linear")
    await _call(
        middleware,
        "open_pull_request",
        {
            "success": False,
            "pr_exists": True,
            "number": 62,
            "url": "https://github.com/o/r/pull/62",
        },
    )
    await _call(middleware, "linear_comment", {"success": False}, error=True)

    _ = await middleware.aafter_agent(cast(AgentState, cast(object, {"messages": []})), MagicMock())

    assert len(client.threads.updates) == 1
