from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.dashboard import team_settings as ts
from agent.dashboard.options import (
    FABLE_MODEL_IDS,
    SUPPORTED_MODEL_IDS,
    fable_disabled_fallback,
    model_default_effort,
)

_FABLE_MODEL = "anthropic:claude-fable-5"
_NON_DEFAULT_MODEL = "openai:gpt-5.6-sol"
_NON_DEFAULT_EFFORT = "high"
_RETIRED_MODEL = "anthropic:claude-opus-4-1"

_MODEL_PAIR_ROLES = (
    "agent",
    "agent subagent",
    "reviewer",
    "reviewer subagent",
    "review diff grouping",
    "review chat",
)

_DEFAULTED_EFFORT_MODEL_FIELDS = {
    "default_agent_reasoning_effort": "default_agent_model",
    "default_agent_subagent_reasoning_effort": "default_agent_subagent_model",
    "default_reviewer_reasoning_effort": "default_reviewer_model",
    "default_reviewer_subagent_reasoning_effort": "default_reviewer_subagent_model",
}

_NULLABLE_FIELD_VALUES: dict[str, object] = {
    "gateway_enabled": True,
    "autofix_enabled": True,
    "autofix_severity_threshold": "high",
    "require_plan_approval": True,
    "auto_merge_mode": ts.AUTO_MERGE_ALWAYS,
    "review_tracing_project": "stored-traces",
    "org_guidelines": "Stored guidelines",
    "default_agent_model": _NON_DEFAULT_MODEL,
    "default_agent_reasoning_effort": _NON_DEFAULT_EFFORT,
    "default_agent_subagent_model": _NON_DEFAULT_MODEL,
    "default_agent_subagent_reasoning_effort": _NON_DEFAULT_EFFORT,
    "default_repo": "stored/repository",
    "default_reviewer_model": _NON_DEFAULT_MODEL,
    "default_reviewer_reasoning_effort": _NON_DEFAULT_EFFORT,
    "default_reviewer_subagent_model": _NON_DEFAULT_MODEL,
    "default_reviewer_subagent_reasoning_effort": _NON_DEFAULT_EFFORT,
    "default_grouping_model": _NON_DEFAULT_MODEL,
    "default_grouping_reasoning_effort": _NON_DEFAULT_EFFORT,
    "default_chat_model": _NON_DEFAULT_MODEL,
    "default_chat_reasoning_effort": _NON_DEFAULT_EFFORT,
    "plan_profile": "stored-plan-profile",
    "review_profile": "stored-review-profile",
    "reviewer_routing": "reviewer_adversarial",
}


async def _upsert_with_stored(
    update: ts.TeamSettingsUpdate, stored: dict[str, object]
) -> dict[str, object]:
    client = MagicMock()
    put_item = AsyncMock()
    client.store.put_item = put_item
    with (
        patch(
            "agent.dashboard.team_settings._get_stored_team_settings",
            new_callable=AsyncMock,
            return_value=stored,
        ),
        patch("agent.dashboard.team_settings._client", return_value=client),
    ):
        value = await ts.upsert_team_settings(update)

    put_item.assert_awaited_once_with(ts.TEAM_SETTINGS_NAMESPACE, ts.TEAM_SETTINGS_KEY, value)
    return value


def _assert_model_pairs_valid(value: dict[str, object]) -> None:
    for (model_field, effort_field), role in zip(
        ts._MODEL_PAIR_FIELDS, _MODEL_PAIR_ROLES, strict=True
    ):
        model, effort = value[model_field], value[effort_field]
        assert isinstance(model, str | None), model_field
        assert isinstance(effort, str | None), effort_field
        ts._validate_model_effort_pair(model, effort, role)


@pytest.mark.asyncio
async def test_partial_auto_merge_update_preserves_every_other_stored_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEFAULT_REPO_OWNER", raising=False)
    monkeypatch.delenv("DEFAULT_REPO_NAME", raising=False)
    defaults = ts._default_settings()
    stored = {
        **defaults,
        "review_draft_prs": True,
        "pr_summaries": False,
        "review_trace_links": False,
        "gateway_enabled": True,
        "autofix_enabled": True,
        "autofix_severity_threshold": "critical",
        "require_plan_approval": True,
        "auto_merge_mode": ts.AUTO_MERGE_ALWAYS,
        "fable_enabled": True,
        "review_tracing_project": "production-reviewer",
        "org_guidelines": "Never erase production settings.",
        "default_agent_model": _NON_DEFAULT_MODEL,
        "default_agent_reasoning_effort": _NON_DEFAULT_EFFORT,
        "default_agent_subagent_model": _NON_DEFAULT_MODEL,
        "default_agent_subagent_reasoning_effort": _NON_DEFAULT_EFFORT,
        "default_repo": "open-swe/production",
        "default_reviewer_model": _NON_DEFAULT_MODEL,
        "default_reviewer_reasoning_effort": _NON_DEFAULT_EFFORT,
        "default_reviewer_subagent_model": _NON_DEFAULT_MODEL,
        "default_reviewer_subagent_reasoning_effort": _NON_DEFAULT_EFFORT,
        "default_grouping_model": _NON_DEFAULT_MODEL,
        "default_grouping_reasoning_effort": _NON_DEFAULT_EFFORT,
        "default_chat_model": _NON_DEFAULT_MODEL,
        "default_chat_reasoning_effort": _NON_DEFAULT_EFFORT,
        "plan_profile": "production-plan",
        "review_profile": "production-review",
        "reviewer_routing": "reviewer_adversarial",
        "updated_at": "2026-07-27T00:00:00+00:00",
    }
    for field_name, default_value in defaults.items():
        assert stored[field_name] != default_value, field_name

    value = await _upsert_with_stored(
        ts.TeamSettingsUpdate(auto_merge_mode=ts.AUTO_MERGE_NEVER), stored
    )

    assert set(value) == set(stored)
    for field_name, stored_value in stored.items():
        if field_name == "auto_merge_mode":
            assert value[field_name] == ts.AUTO_MERGE_NEVER
        elif field_name == "updated_at":
            assert value[field_name] != stored_value
        else:
            assert value[field_name] == stored_value, field_name


@pytest.mark.parametrize("field_name", _NULLABLE_FIELD_VALUES)
@pytest.mark.asyncio
async def test_explicit_null_clears_nullable_field(field_name: str) -> None:
    defaults = ts._default_settings()
    stored = {
        **defaults,
        field_name: _NULLABLE_FIELD_VALUES[field_name],
    }
    update = ts.TeamSettingsUpdate.model_validate({field_name: None})

    assert field_name in (update.supplied_fields or frozenset())
    value = await _upsert_with_stored(update, stored)

    model_field = _DEFAULTED_EFFORT_MODEL_FIELDS.get(field_name)
    if model_field is None:
        assert value[field_name] is None
    else:
        fallback_model = defaults[model_field]
        assert isinstance(fallback_model, str)
        assert value[field_name] == model_default_effort(fallback_model)


@pytest.mark.asyncio
async def test_clearing_model_also_clears_stored_effort_and_keeps_pairs_valid() -> None:
    stored = {
        **ts._default_settings(),
        "default_agent_model": _NON_DEFAULT_MODEL,
        "default_agent_reasoning_effort": _NON_DEFAULT_EFFORT,
    }
    update = ts.TeamSettingsUpdate.model_validate({"default_agent_model": None})

    value = await _upsert_with_stored(update, stored)

    assert value["default_agent_model"] is None
    assert value["default_agent_reasoning_effort"] is None
    _assert_model_pairs_valid(value)


@pytest.mark.parametrize(("model_field", "effort_field"), ts._MODEL_PAIR_FIELDS)
@pytest.mark.asyncio
async def test_clearing_effort_backfills_model_default_effort(
    model_field: str, effort_field: str
) -> None:
    stored = {
        **ts._default_settings(),
        model_field: _NON_DEFAULT_MODEL,
        effort_field: _NON_DEFAULT_EFFORT,
    }
    update = ts.TeamSettingsUpdate.model_validate({effort_field: None})

    value = await _upsert_with_stored(update, stored)

    assert value[model_field] == _NON_DEFAULT_MODEL
    assert value[effort_field] == model_default_effort(_NON_DEFAULT_MODEL)


@pytest.mark.parametrize("stored_model", ["retired:model", 42])
@pytest.mark.asyncio
async def test_invalid_stored_model_pair_is_cleanly_cleared_when_pair_is_touched(
    stored_model: object,
) -> None:
    stored = {
        **ts._default_settings(),
        "default_chat_model": stored_model,
        "default_chat_reasoning_effort": _NON_DEFAULT_EFFORT,
    }
    update = ts.TeamSettingsUpdate.model_validate({"default_chat_reasoning_effort": None})

    value = await _upsert_with_stored(update, stored)

    assert value["default_chat_model"] is None
    assert value["default_chat_reasoning_effort"] is None
    _assert_model_pairs_valid(value)


@pytest.mark.asyncio
async def test_unrelated_update_preserves_stale_stored_model_pair_byte_for_byte() -> None:
    # Guard against silent rot: this test only proves scope if the id is retired.
    assert _RETIRED_MODEL not in SUPPORTED_MODEL_IDS
    stored = {
        **ts._default_settings(),
        "default_agent_model": _RETIRED_MODEL,
        "default_agent_reasoning_effort": _NON_DEFAULT_EFFORT,
    }
    update = ts.TeamSettingsUpdate.model_validate({"auto_merge_mode": ts.AUTO_MERGE_NEVER})

    value = await _upsert_with_stored(update, stored)

    assert value["default_agent_model"] == _RETIRED_MODEL
    assert value["default_agent_reasoning_effort"] == _NON_DEFAULT_EFFORT


@pytest.mark.asyncio
async def test_supplying_model_over_stale_stored_pair_normalizes_it() -> None:
    replacement_model = "anthropic:claude-sonnet-5"
    assert _RETIRED_MODEL not in SUPPORTED_MODEL_IDS
    stored = {
        **ts._default_settings(),
        "default_agent_model": _RETIRED_MODEL,
        "default_agent_reasoning_effort": _NON_DEFAULT_EFFORT,
    }
    update = ts.TeamSettingsUpdate.model_construct(
        default_agent_model=replacement_model,
        supplied_fields=frozenset({"default_agent_model"}),
    )

    value = await _upsert_with_stored(update, stored)

    assert value["default_agent_model"] == replacement_model
    assert value["default_agent_reasoning_effort"] == _NON_DEFAULT_EFFORT
    _assert_model_pairs_valid(value)


@pytest.mark.asyncio
async def test_changing_model_backfills_effort_when_stored_effort_is_unsupported() -> None:
    replacement_model = "google_genai:gemini-3.5-flash"
    stored = {
        **ts._default_settings(),
        "default_agent_model": _NON_DEFAULT_MODEL,
        "default_agent_reasoning_effort": "xhigh",
    }
    update = ts.TeamSettingsUpdate.model_construct(
        default_agent_model=replacement_model,
        supplied_fields=frozenset({"default_agent_model"}),
    )

    value = await _upsert_with_stored(update, stored)

    assert value["default_agent_model"] == replacement_model
    assert value["default_agent_reasoning_effort"] == model_default_effort(replacement_model)


@pytest.mark.asyncio
async def test_valid_supplied_model_pair_survives_normalization() -> None:
    model = "google_genai:gemini-3.5-flash"
    effort = "high"
    stored = ts._default_settings()
    update = ts.TeamSettingsUpdate.model_validate(
        {
            "default_agent_model": model,
            "default_agent_reasoning_effort": effort,
        }
    )

    value = await _upsert_with_stored(update, stored)

    assert value["default_agent_model"] == model
    assert value["default_agent_reasoning_effort"] == effort


@pytest.mark.asyncio
async def test_explicit_value_wins_over_stored_and_default() -> None:
    defaults = ts._default_settings()
    assert defaults["review_draft_prs"] is False
    stored = {**defaults, "review_draft_prs": False}

    value = await _upsert_with_stored(ts.TeamSettingsUpdate(review_draft_prs=True), stored)

    assert value["review_draft_prs"] is True


@pytest.mark.asyncio
async def test_partial_update_without_stored_record_fills_every_other_default() -> None:
    defaults = ts._default_settings()

    value = await _upsert_with_stored(
        ts.TeamSettingsUpdate(auto_merge_mode=ts.AUTO_MERGE_ALWAYS), {}
    )

    for field_name, default_value in defaults.items():
        if field_name == "auto_merge_mode":
            assert value[field_name] == ts.AUTO_MERGE_ALWAYS
        elif field_name == "updated_at":
            assert value[field_name] is not None
        else:
            assert value[field_name] == default_value, field_name


@pytest.mark.asyncio
async def test_supplied_fields_is_unspoofable_and_never_persisted() -> None:
    update = ts.TeamSettingsUpdate.model_validate(
        {"supplied_fields": frozenset(), "auto_merge_mode": ts.AUTO_MERGE_NEVER}
    )
    assert update.supplied_fields == frozenset({"auto_merge_mode"})
    assert "supplied_fields" not in update.model_dump()
    stored = {
        **ts._default_settings(),
        "review_draft_prs": True,
        "auto_merge_mode": ts.AUTO_MERGE_ALWAYS,
    }

    value = await _upsert_with_stored(update, stored)

    assert value["review_draft_prs"] is True
    assert value["auto_merge_mode"] == ts.AUTO_MERGE_NEVER
    assert "supplied_fields" not in value


@pytest.mark.asyncio
async def test_model_construct_without_supplied_fields_fails_toward_overwrite() -> None:
    update = ts.TeamSettingsUpdate.model_construct(auto_merge_mode=ts.AUTO_MERGE_NEVER)
    assert update.supplied_fields is None
    stored = {**ts._default_settings(), "review_draft_prs": True}

    value = await _upsert_with_stored(update, stored)

    assert value["review_draft_prs"] is False


@pytest.mark.parametrize(
    "stored",
    ({}, {"auto_merge_mode": None}, {"auto_merge_mode": "invalid"}),
)
@pytest.mark.asyncio
async def test_unsupplied_auto_merge_mode_normalizes_stored_or_default(
    stored: dict[str, object],
) -> None:
    value = await _upsert_with_stored(ts.TeamSettingsUpdate(), stored)

    assert value["auto_merge_mode"] == ts.AUTO_MERGE_NEVER


@pytest.mark.asyncio
async def test_disabling_fable_gates_merged_stored_models_for_every_role() -> None:
    stored = {**ts._default_settings(), "fable_enabled": True}
    for model_field, effort_field in ts._MODEL_PAIR_FIELDS:
        stored[model_field] = _FABLE_MODEL
        stored[effort_field] = "high"

    value = await _upsert_with_stored(ts.TeamSettingsUpdate(fable_enabled=False), stored)

    fallback = fable_disabled_fallback("high")
    assert value["fable_enabled"] is False
    for model_field, effort_field in ts._MODEL_PAIR_FIELDS:
        assert value[model_field] == fallback[0], model_field
        assert value[model_field] not in FABLE_MODEL_IDS, model_field
        assert value[effort_field] == fallback[1], effort_field


@pytest.mark.parametrize(
    ("model_field", "effort_field"),
    (
        ("default_agent_model", "default_agent_reasoning_effort"),
        ("default_chat_model", "default_chat_reasoning_effort"),
    ),
)
@pytest.mark.asyncio
async def test_supplied_fable_model_is_gated_when_effective_flag_is_disabled(
    model_field: str,
    effort_field: str,
) -> None:
    stored = {**ts._default_settings(), "fable_enabled": False}
    update = ts.TeamSettingsUpdate.model_validate({model_field: _FABLE_MODEL, effort_field: "high"})

    value = await _upsert_with_stored(update, stored)

    assert (value[model_field], value[effort_field]) == fable_disabled_fallback("high")


@pytest.mark.asyncio
async def test_fable_model_survives_when_stored_effective_flag_is_enabled() -> None:
    stored = {**ts._default_settings(), "fable_enabled": True}

    value = await _upsert_with_stored(
        ts.TeamSettingsUpdate(
            default_agent_model=_FABLE_MODEL,
            default_agent_reasoning_effort="high",
        ),
        stored,
    )

    assert value["fable_enabled"] is True
    assert value["default_agent_model"] == _FABLE_MODEL
    assert value["default_agent_reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_upsert_does_not_write_when_stored_settings_read_fails() -> None:
    client = MagicMock()
    client.store.get_item = AsyncMock(side_effect=RuntimeError("store unavailable"))
    client.store.put_item = AsyncMock()
    with patch("agent.dashboard.team_settings._client", return_value=client):
        with pytest.raises(RuntimeError, match="store unavailable"):
            await ts.upsert_team_settings(
                ts.TeamSettingsUpdate(auto_merge_mode=ts.AUTO_MERGE_ALWAYS)
            )

    client.store.put_item.assert_not_awaited()
