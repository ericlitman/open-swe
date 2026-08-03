from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agent.dashboard import routes
from agent.dashboard.options import default_model_pair, provider_fallback_pair
from agent.dashboard.team_settings import (
    get_team_default_grouping_model,
    get_team_default_model,
    get_team_default_model_pair,
    get_team_default_subagent_model,
    get_team_model_resolution_diagnostics,
    get_team_settings,
)

_VALID_ANTHROPIC = ("anthropic:claude-opus-4-8", "high")
_VALID_OPENAI = ("openai:gpt-5.6-sol", "low")
_STALE_ANTHROPIC = ("anthropic:claude-opus-4-1", "xhigh")


def _fallback_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "agent.dashboard.team_settings"
        and hasattr(record, "model_resolution_fallback")
    ]


async def _resolve_surface(surface: str) -> None:
    if surface == "agent":
        await get_team_default_model("agent")
    elif surface == "agent_subagent":
        await get_team_default_subagent_model("agent")
    elif surface == "reviewer":
        await get_team_default_model("reviewer")
    elif surface == "reviewer_subagent":
        await get_team_default_subagent_model("reviewer")
    elif surface == "grouping":
        await get_team_default_grouping_model()
    else:
        await get_team_default_model("chat")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "setting"),
    [
        ("agent", "default_agent_model"),
        ("agent_subagent", "default_agent_subagent_model"),
        ("reviewer", "default_reviewer_model"),
        ("reviewer_subagent", "default_reviewer_subagent_model"),
        ("grouping", "default_reviewer_subagent_model"),
        ("chat", "default_agent_model"),
    ],
)
async def test_product_default_resolution_warns_once_per_surface(
    surface: str,
    setting: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="agent.dashboard.team_settings")
    with patch(
        "agent.dashboard.team_settings.get_team_settings",
        new_callable=AsyncMock,
        return_value={},
    ):
        await _resolve_surface(surface)

    records = _fallback_records(caplog)
    assert len(records) == 1
    assert records[0].getMessage() == (
        f"model resolution fell back to product default for {surface} "
        f"({setting} unset or invalid); resolved openai:gpt-5.5/medium"
    )
    assert records[0].model_resolution_fallback == {  # type: ignore[attr-defined]
        "surface": surface,
        "setting": setting,
        "resolved_model": "openai:gpt-5.5",
        "resolved_effort": "medium",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        {},
        {
            "default_agent_model": None,
            "default_agent_reasoning_effort": None,
        },
        {
            "default_agent_reasoning_effort": "high",
        },
    ],
)
async def test_stored_unset_agent_pair_warns_through_real_settings_read(
    stored: dict[str, object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="agent.dashboard.team_settings")
    with patch(
        "agent.dashboard.team_settings._get_stored_team_settings",
        new_callable=AsyncMock,
        return_value=stored,
    ):
        assert await get_team_default_model("agent") == default_model_pair()

    records = _fallback_records(caplog)
    assert len(records) == 1
    assert records[0].model_resolution_fallback == {  # type: ignore[attr-defined]
        "surface": "agent",
        "setting": "default_agent_model",
        "resolved_model": "openai:gpt-5.5",
        "resolved_effort": "medium",
    }


@pytest.mark.asyncio
async def test_stored_missing_effort_uses_provider_fallback_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="agent.dashboard.team_settings")
    stored = {"default_agent_model": _VALID_ANTHROPIC[0]}
    expected = provider_fallback_pair(_VALID_ANTHROPIC[0], None)
    assert expected is not None
    with patch(
        "agent.dashboard.team_settings._get_stored_team_settings",
        new_callable=AsyncMock,
        return_value=stored,
    ):
        assert await get_team_default_model("agent") == expected

    assert _fallback_records(caplog) == []


@pytest.mark.asyncio
async def test_diagnostics_preserve_unset_pairs_without_changing_settings_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="agent.dashboard.team_settings")
    with patch(
        "agent.dashboard.team_settings._get_stored_team_settings",
        new_callable=AsyncMock,
        return_value={},
    ):
        settings = await get_team_settings()
        diagnostics = await get_team_model_resolution_diagnostics()

    assert (
        settings["default_agent_model"],
        settings["default_agent_reasoning_effort"],
    ) == default_model_pair()
    assert [entry["fallback"] for entry in diagnostics] == ["product_default"] * 6
    assert _fallback_records(caplog) == []


@pytest.mark.asyncio
async def test_model_pair_warns_for_each_independent_product_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="agent.dashboard.team_settings")
    with patch(
        "agent.dashboard.team_settings.get_team_settings",
        new_callable=AsyncMock,
        return_value={},
    ):
        await get_team_default_model_pair("agent")

    payloads = [
        record.model_resolution_fallback  # type: ignore[attr-defined]
        for record in _fallback_records(caplog)
    ]
    assert [payload["surface"] for payload in payloads] == ["agent", "agent_subagent"]


@pytest.mark.asyncio
async def test_supported_pair_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="agent.dashboard.team_settings")
    with patch(
        "agent.dashboard.team_settings.get_team_settings",
        new_callable=AsyncMock,
        return_value={
            "default_agent_model": _VALID_ANTHROPIC[0],
            "default_agent_reasoning_effort": _VALID_ANTHROPIC[1],
        },
    ):
        assert await get_team_default_model("agent") == _VALID_ANTHROPIC

    assert _fallback_records(caplog) == []


@pytest.mark.asyncio
async def test_provider_fallback_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="agent.dashboard.team_settings")
    expected = provider_fallback_pair(*_STALE_ANTHROPIC)
    assert expected is not None
    with patch(
        "agent.dashboard.team_settings.get_team_settings",
        new_callable=AsyncMock,
        return_value={
            "default_agent_model": _STALE_ANTHROPIC[0],
            "default_agent_reasoning_effort": _STALE_ANTHROPIC[1],
        },
    ):
        assert await get_team_default_model("agent") == expected

    assert _fallback_records(caplog) == []


@pytest.mark.asyncio
async def test_chat_inherits_configured_agent_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="agent.dashboard.team_settings")
    with patch(
        "agent.dashboard.team_settings.get_team_settings",
        new_callable=AsyncMock,
        return_value={
            "default_chat_model": None,
            "default_chat_reasoning_effort": None,
            "default_agent_model": _VALID_ANTHROPIC[0],
            "default_agent_reasoning_effort": _VALID_ANTHROPIC[1],
        },
    ):
        assert await get_team_default_model("chat") == _VALID_ANTHROPIC

    assert _fallback_records(caplog) == []


@pytest.mark.asyncio
async def test_chat_inherits_missing_agent_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="agent.dashboard.team_settings")
    with patch(
        "agent.dashboard.team_settings.get_team_settings",
        new_callable=AsyncMock,
        return_value={"default_chat_model": None, "default_chat_reasoning_effort": None},
    ):
        assert await get_team_default_model("chat") == default_model_pair()

    records = _fallback_records(caplog)
    assert len(records) == 1
    assert records[0].model_resolution_fallback["surface"] == "chat"  # type: ignore[attr-defined]
    setting = records[0].model_resolution_fallback["setting"]  # type: ignore[attr-defined]
    assert setting == "default_agent_model"


@pytest.mark.asyncio
async def test_grouping_inherits_configured_reviewer_subagent_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="agent.dashboard.team_settings")
    with patch(
        "agent.dashboard.team_settings.get_team_settings",
        new_callable=AsyncMock,
        return_value={
            "default_grouping_model": None,
            "default_grouping_reasoning_effort": None,
            "default_reviewer_subagent_model": _VALID_OPENAI[0],
            "default_reviewer_subagent_reasoning_effort": _VALID_OPENAI[1],
        },
    ):
        assert await get_team_default_grouping_model() == _VALID_OPENAI

    assert _fallback_records(caplog) == []


@pytest.mark.asyncio
async def test_grouping_inherits_missing_reviewer_subagent_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="agent.dashboard.team_settings")
    with patch(
        "agent.dashboard.team_settings.get_team_settings",
        new_callable=AsyncMock,
        return_value={"default_grouping_model": None},
    ):
        assert await get_team_default_grouping_model() == default_model_pair()

    records = _fallback_records(caplog)
    assert len(records) == 1
    assert records[0].model_resolution_fallback["surface"] == "grouping"  # type: ignore[attr-defined]
    setting = records[0].model_resolution_fallback["setting"]  # type: ignore[attr-defined]
    assert setting == "default_reviewer_subagent_model"


@pytest.mark.asyncio
async def test_model_resolution_diagnostics_reports_all_tiers_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="agent.dashboard.team_settings")
    settings: dict[str, Any] = {
        "default_agent_model": _VALID_ANTHROPIC[0],
        "default_agent_reasoning_effort": _VALID_ANTHROPIC[1],
        "default_agent_subagent_model": _STALE_ANTHROPIC[0],
        "default_agent_subagent_reasoning_effort": _STALE_ANTHROPIC[1],
        "default_reviewer_model": "mystery:model",
        "default_reviewer_reasoning_effort": "high",
        "default_reviewer_subagent_model": _VALID_OPENAI[0],
        "default_reviewer_subagent_reasoning_effort": _VALID_OPENAI[1],
        "default_grouping_model": None,
        "default_grouping_reasoning_effort": None,
        "default_chat_model": None,
        "default_chat_reasoning_effort": None,
    }
    with patch(
        "agent.dashboard.team_settings.get_team_settings",
        new_callable=AsyncMock,
        return_value=settings,
    ) as get_settings:
        diagnostics = await get_team_model_resolution_diagnostics()

    get_settings.assert_awaited_once_with(preserve_unset_model_pairs=True)
    assert [entry["surface"] for entry in diagnostics] == [
        "agent",
        "agent_subagent",
        "reviewer",
        "reviewer_subagent",
        "grouping",
        "chat",
    ]
    assert [entry["fallback"] for entry in diagnostics] == [
        "none",
        "provider",
        "product_default",
        "none",
        "none",
        "none",
    ]
    assert diagnostics[2] == {
        "surface": "reviewer",
        "setting": "default_reviewer_model",
        "fallback": "product_default",
        "resolved_model": "openai:gpt-5.5",
        "resolved_effort": "medium",
    }
    assert diagnostics[4]["setting"] == "default_reviewer_subagent_model"
    assert diagnostics[5]["setting"] == "default_agent_model"
    assert _fallback_records(caplog) == []


@pytest.mark.asyncio
async def test_model_resolution_endpoint_returns_separate_diagnostics_resource() -> None:
    surfaces = [{"surface": "agent", "fallback": "none"}]
    with patch(
        "agent.dashboard.routes.get_team_model_resolution_diagnostics",
        new_callable=AsyncMock,
        return_value=surfaces,
    ) as get_diagnostics:
        response = await routes.api_get_team_settings_model_resolution(session={"sub": "octocat"})

    get_diagnostics.assert_awaited_once_with()
    assert response == {"surfaces": surfaces}
