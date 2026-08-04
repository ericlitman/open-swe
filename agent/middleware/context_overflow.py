from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import anthropic
import openai
from fireworks import BadRequestError as FireworksBadRequestError
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_anthropic.chat_models import AnthropicContextOverflowError
from langchain_core.exceptions import ContextOverflowError
from langchain_fireworks.chat_models import FireworksContextOverflowError
from langchain_openai.chat_models.base import OpenAIContextOverflowError


def _contains_context_too_large(value: object) -> bool:
    if value == "context_too_large":
        return True
    if isinstance(value, dict):
        return any(_contains_context_too_large(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_context_too_large(item) for item in value)
    return False


def _is_context_overflow(exc: BaseException) -> bool:
    return _contains_context_too_large(getattr(exc, "body", None)) or (
        "exceeds the context window" in str(exc).lower()
    )


class ContextOverflowPromotionMiddleware(AgentMiddleware):
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> Any:
        try:
            return await handler(request)
        except ContextOverflowError:
            raise
        except openai.BadRequestError as exc:
            if not _is_context_overflow(exc):
                raise
            raise OpenAIContextOverflowError(
                message=exc.message, response=exc.response, body=exc.body
            ) from exc
        except anthropic.BadRequestError as exc:
            if not _is_context_overflow(exc):
                raise
            raise AnthropicContextOverflowError(
                message=exc.message, response=exc.response, body=exc.body
            ) from exc
        except FireworksBadRequestError as exc:
            if not _is_context_overflow(exc):
                raise
            raise FireworksContextOverflowError(
                str(exc), response=exc.response, body=exc.body
            ) from exc
