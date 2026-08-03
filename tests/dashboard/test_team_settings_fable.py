from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.dashboard.team_settings import TeamSettingsUpdate, get_team_fable_enabled

# --- accessor: get_team_fable_enabled (async, patched store) ---


@pytest.mark.asyncio
async def test_fable_enabled_defaults_false_when_absent() -> None:
    # Legacy record with no fable_enabled key -> off.
    with patch(
        "agent.dashboard.team_settings.get_team_settings",
        new_callable=AsyncMock,
        return_value={},
    ):
        assert await get_team_fable_enabled() is False


@pytest.mark.asyncio
async def test_fable_enabled_true_when_set() -> None:
    with patch(
        "agent.dashboard.team_settings.get_team_settings",
        new_callable=AsyncMock,
        return_value={"fable_enabled": True},
    ):
        assert await get_team_fable_enabled() is True


@pytest.mark.asyncio
async def test_fable_enabled_false_for_non_bool_value() -> None:
    # Fail-closed: any non-bool (e.g. a stray string) resolves to False.
    with patch(
        "agent.dashboard.team_settings.get_team_settings",
        new_callable=AsyncMock,
        return_value={"fable_enabled": "true"},
    ):
        assert await get_team_fable_enabled() is False


# --- validation: TeamSettingsUpdate (sync) ---


def test_update_defaults_fable_disabled() -> None:
    assert TeamSettingsUpdate().fable_enabled is False


def test_update_accepts_non_fable_model_pair() -> None:
    update = TeamSettingsUpdate(
        default_agent_model="openai:gpt-5.6-sol",
        default_agent_reasoning_effort="medium",
    )
    assert update.default_agent_model == "openai:gpt-5.6-sol"
    assert update.default_agent_reasoning_effort == "medium"
