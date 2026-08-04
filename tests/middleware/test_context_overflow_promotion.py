from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import openai
import pytest
from deepagents.middleware import summarization as summarization_module
from deepagents.middleware.summarization import SummarizationMiddleware
from fireworks import BadRequestError as FireworksBadRequestError
from langchain.agents.middleware import AgentState
from langchain.agents.middleware.types import ExtendedModelResponse, ModelRequest, ModelResponse
from langchain_anthropic.chat_models import AnthropicContextOverflowError
from langchain_core.exceptions import ContextOverflowError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_fireworks.chat_models import FireworksContextOverflowError
from langchain_openai.chat_models.base import OpenAIContextOverflowError

from agent.middleware.context_overflow import ContextOverflowPromotionMiddleware

INCIDENT_MESSAGE = "Your input exceeds the context window of this model"


def _openai_bad_request(
    *, message: str = INCIDENT_MESSAGE, code: str | None = "context_too_large"
) -> openai.BadRequestError:
    error: dict[str, object] = {"message": message, "type": "invalid_request_error"}
    if code is not None:
        error["code"] = code
    body = {"error": error}
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
        json=body,
    )
    return openai.BadRequestError(message, response=response, body=body)


def _request(messages: list[AnyMessage] | None = None) -> ModelRequest[None]:
    request_messages = messages or [HumanMessage(content="hello")]
    return ModelRequest(
        model=cast(BaseChatModel, MagicMock(spec=BaseChatModel)),
        messages=request_messages,
        state=cast(AgentState[Any], {"messages": request_messages}),
    )


@pytest.mark.asyncio
async def test_promotes_exact_incident_error_with_original_cause() -> None:
    middleware = ContextOverflowPromotionMiddleware()
    original = _openai_bad_request()

    async def handler(_request: ModelRequest[None]) -> ModelResponse[Any]:
        raise original

    with pytest.raises(ContextOverflowError) as raised:
        await middleware.awrap_model_call(_request(), handler)

    assert isinstance(raised.value, OpenAIContextOverflowError)
    assert raised.value.__cause__ is original


@pytest.mark.asyncio
async def test_promotes_case_insensitive_message_without_code() -> None:
    middleware = ContextOverflowPromotionMiddleware()
    original = _openai_bad_request(message=INCIDENT_MESSAGE.upper(), code=None)

    async def handler(_request: ModelRequest[None]) -> ModelResponse[Any]:
        raise original

    with pytest.raises(ContextOverflowError) as raised:
        await middleware.awrap_model_call(_request(), handler)

    assert raised.value.__cause__ is original


@pytest.mark.asyncio
async def test_promotes_nested_fireworks_context_code() -> None:
    middleware = ContextOverflowPromotionMiddleware()
    body = {"error": {"details": [{"reason": "context_too_large"}]}}
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://api.fireworks.ai/inference/v1/chat/completions"),
        json=body,
    )
    original = FireworksBadRequestError("input rejected", response=response, body=body)

    async def handler(_request: ModelRequest[None]) -> ModelResponse[Any]:
        raise original

    with pytest.raises(ContextOverflowError) as raised:
        await middleware.awrap_model_call(_request(), handler)

    assert isinstance(raised.value, FireworksContextOverflowError)
    assert raised.value.__cause__ is original


@pytest.mark.asyncio
async def test_promotes_anthropic_context_code() -> None:
    middleware = ContextOverflowPromotionMiddleware()
    body = {"error": {"code": "context_too_large", "message": INCIDENT_MESSAGE}}
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        json=body,
    )
    original = anthropic.BadRequestError("input rejected", response=response, body=body)

    async def handler(_request: ModelRequest[None]) -> ModelResponse[Any]:
        raise original

    with pytest.raises(ContextOverflowError) as raised:
        await middleware.awrap_model_call(_request(), handler)

    assert isinstance(raised.value, AnthropicContextOverflowError)
    assert raised.value.__cause__ is original


@pytest.mark.asyncio
async def test_non_overflow_bad_request_propagates_unchanged() -> None:
    middleware = ContextOverflowPromotionMiddleware()
    original = _openai_bad_request(message="Invalid tool schema", code="invalid_request")

    async def handler(_request: ModelRequest[None]) -> ModelResponse[Any]:
        raise original

    with pytest.raises(openai.BadRequestError) as raised:
        await middleware.awrap_model_call(_request(), handler)

    assert raised.value is original


@pytest.mark.asyncio
async def test_existing_context_overflow_error_propagates_unchanged() -> None:
    middleware = ContextOverflowPromotionMiddleware()
    original_bad_request = _openai_bad_request()
    original = OpenAIContextOverflowError(
        message=original_bad_request.message,
        response=original_bad_request.response,
        body=original_bad_request.body,
    )

    async def handler(_request: ModelRequest[None]) -> ModelResponse[Any]:
        raise original

    with pytest.raises(ContextOverflowError) as raised:
        await middleware.awrap_model_call(_request(), handler)

    assert raised.value is original
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
async def test_summarization_fallback_observes_promoted_error() -> None:
    messages: list[AnyMessage] = [
        HumanMessage(content="first"),
        AIMessage(content="second"),
        HumanMessage(content="third"),
        AIMessage(content="fourth"),
    ]
    request = _request(messages)
    promotion = ContextOverflowPromotionMiddleware()
    summarization = SummarizationMiddleware(
        model=request.model,
        backend=MagicMock(),
        trigger=None,
        keep=("messages", 1),
    )
    original = _openai_bad_request()
    calls = 0

    async def provider_handler(_request: ModelRequest[None]) -> ModelResponse[Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise original
        return ModelResponse(result=[AIMessage(content="recovered")])

    async def promotion_handler(model_request: ModelRequest[None]) -> ModelResponse[Any]:
        return cast(
            ModelResponse[Any],
            await promotion.awrap_model_call(model_request, provider_handler),
        )

    with (
        patch.object(summarization, "_count_tokens", return_value=1),
        patch.object(summarization, "_truncate_args", return_value=(messages, False)),
        patch.object(summarization, "_should_summarize", return_value=False),
        patch.object(summarization, "_determine_cutoff_index", return_value=2),
        patch.object(
            summarization,
            "_partition_messages",
            return_value=(messages[:2], messages[2:]),
        ),
        patch.object(summarization, "_get_backend", return_value=MagicMock()),
        patch.object(
            summarization,
            "_aoffload_inline_media",
            new=AsyncMock(return_value=(messages[:2], 0)),
        ),
        patch.object(
            summarization,
            "_aoffload_to_backend",
            new=AsyncMock(return_value="/conversation_history/test.md"),
        ),
        patch.object(
            summarization,
            "_acreate_summary",
            new=AsyncMock(return_value="summary"),
        ),
        patch.object(
            summarization_module,
            "_aclip_overflow_tail",
            new=AsyncMock(return_value=(messages[2:], [])),
        ),
    ):
        result = await summarization.awrap_model_call(request, promotion_handler)

    assert isinstance(result, ExtendedModelResponse)
    assert calls == 2
    assert result.model_response.result[0].content == "recovered"
