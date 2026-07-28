from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command
from langgraph_sdk import get_client

logger = logging.getLogger(__name__)
_GITHUB_COMMENT_COMMAND = re.compile(r"(?:^|\s)gh\s+(?:issue|pr)\s+comment(?:\s|$)")
_SOURCES_WITH_COMPLETION_REPLY = {"github", "github_issue", "linear", "slack"}


def _tool_name(request: ToolCallRequest) -> str | None:
    tool_call = getattr(request, "tool_call", None)
    if isinstance(tool_call, Mapping):
        name = tool_call.get("name")
        return name if isinstance(name, str) else None
    return None


def _tool_args(request: ToolCallRequest) -> dict[str, Any]:
    tool_call = getattr(request, "tool_call", None)
    args = tool_call.get("args") if isinstance(tool_call, Mapping) else None
    return dict(args) if isinstance(args, Mapping) else {}


def _payload(result: ToolMessage | Command[Any]) -> dict[str, Any]:
    if not isinstance(result, ToolMessage):
        return {}
    content = result.content
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        return {}
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _successful(result: ToolMessage | Command[Any]) -> bool:
    if not isinstance(result, ToolMessage) or result.status == "error":
        return False
    payload = _payload(result)
    return not payload or payload.get("success") is True


class SourceCompletionGuardMiddleware(AgentMiddleware):
    state_schema = AgentState

    def __init__(self, *, thread_id: str, source: str) -> None:
        super().__init__()
        self._thread_id = thread_id
        self._source = source
        self._pr: dict[str, Any] | None = None
        self._replied = False

    def _record_pr_result(self, result: ToolMessage | Command[Any]) -> None:
        payload = _payload(result)
        number = payload.get("number")
        if not isinstance(number, int):
            return
        if payload.get("success") is True or payload.get("pr_exists") is True:
            self._pr = {"pr_number": number, "pr_url": payload.get("url")}
            self._replied = False

    def _is_source_reply(
        self, request: ToolCallRequest, result: ToolMessage | Command[Any]
    ) -> bool:
        if not _successful(result):
            return False
        name = _tool_name(request)
        if self._source == "linear":
            return name == "linear_comment"
        if self._source == "slack":
            return name == "slack_thread_reply"
        if self._source in {"github", "github_issue"} and name == "execute":
            command = _tool_args(request).get("command")
            return isinstance(command, str) and bool(_GITHUB_COMMENT_COMMAND.search(command))
        return False

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        result = await handler(request)
        if _tool_name(request) == "open_pull_request":
            self._record_pr_result(result)
        elif self._pr is not None and self._is_source_reply(request, result):
            self._replied = True
        return result

    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        if (
            self._pr is None
            or self._replied
            or not self._thread_id
            or self._source not in _SOURCES_WITH_COMPLETION_REPLY
        ):
            return None
        metadata = {
            "source_completion_reply_missing": {
                "source": self._source,
                **self._pr,
            }
        }
        try:
            await get_client().threads.update(thread_id=self._thread_id, metadata=metadata)
        except Exception:
            logger.warning(
                "Failed to record missing source completion reply for thread %s",
                self._thread_id,
                exc_info=True,
            )
            return None
        logger.warning(
            "Run opened PR #%s but did not post a %s completion reply",
            self._pr["pr_number"],
            self._source,
        )
        return None
