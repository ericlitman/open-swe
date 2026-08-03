"""Team-wide Open SWE Review (Bugbot) settings stored in LangGraph Store.

A single record keyed ``"default"`` keeps all instance-wide reviewer
configuration in one place. Per-repo style prompts live in
:mod:`agent.dashboard.review_styles`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from langgraph_sdk import get_client
from pydantic import BaseModel, Field, field_validator, model_validator

from ..utils.gateway import resolve_gateway_enabled
from .options import (
    FABLE_MODEL_IDS,
    SUPPORTED_MODEL_IDS,
    default_model_pair,
    gate_fable_model,
    model_default_effort,
    model_supports_effort,
    provider_fallback_pair,
)

logger = logging.getLogger(__name__)

TEAM_SETTINGS_NAMESPACE: list[str] = ["team_settings"]
TEAM_SETTINGS_KEY = "default"

# Cap the org-wide guidelines so a runaway value can't dominate the reviewer
# prompt. Generous enough for a detailed policy, small enough to stay bounded.
ORG_GUIDELINES_MAX_CHARS = 10_000
REVIEW_TRACING_PROJECT_MAX_CHARS = 256
REVIEWER_ROUTING_VALUES = frozenset({"reviewer", "reviewer_adversarial"})
AutoMergeMode = Literal["never", "on_plan_approval", "always"]
AUTO_MERGE_NEVER: AutoMergeMode = "never"
AUTO_MERGE_ON_PLAN_APPROVAL: AutoMergeMode = "on_plan_approval"
AUTO_MERGE_ALWAYS: AutoMergeMode = "always"
AUTO_MERGE_MODES = frozenset({AUTO_MERGE_NEVER, AUTO_MERGE_ON_PLAN_APPROVAL, AUTO_MERGE_ALWAYS})


class TeamSettingsUpdate(BaseModel):
    review_draft_prs: bool = False
    pr_summaries: bool = True
    review_trace_links: bool = True
    # Tri-state LLM Gateway toggle: True/False is authoritative, None inherits the
    # LANGSMITH_GATEWAY_ENABLED deployment default.
    gateway_enabled: bool | None = None
    autofix_enabled: bool | None = None
    autofix_severity_threshold: Literal["low", "medium", "high", "critical"] | None = None
    require_plan_approval: bool | None = None
    auto_merge_mode: AutoMergeMode | None = None
    fable_enabled: bool = False
    review_tracing_project: str | None = None
    org_guidelines: str | None = None
    default_agent_model: str | None = None
    default_agent_reasoning_effort: str | None = None
    default_agent_subagent_model: str | None = None
    default_agent_subagent_reasoning_effort: str | None = None
    default_repo: str | None = None
    default_reviewer_model: str | None = None
    default_reviewer_reasoning_effort: str | None = None
    default_reviewer_subagent_model: str | None = None
    default_reviewer_subagent_reasoning_effort: str | None = None
    default_grouping_model: str | None = None
    default_grouping_reasoning_effort: str | None = None
    default_chat_model: str | None = None
    default_chat_reasoning_effort: str | None = None
    plan_profile: str | None = None
    review_profile: str | None = None
    reviewer_routing: str | None = None
    supplied_fields: frozenset[str] | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="before")
    @classmethod
    def _capture_supplied_fields(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        values = dict(data)
        values["supplied_fields"] = frozenset(
            field_name for field_name in data if field_name != "supplied_fields"
        )
        return values

    @field_validator("plan_profile", "review_profile", mode="before")
    @classmethod
    def _normalize_stage_profile(cls, v: object) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("stage profile must be a string")
        return v.strip() or None

    @field_validator("reviewer_routing", mode="before")
    @classmethod
    def _normalize_reviewer_routing(cls, v: object) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("reviewer_routing must be a string")
        value = v.strip()
        if not value:
            return None
        if value not in REVIEWER_ROUTING_VALUES:
            raise ValueError(f"unsupported reviewer_routing: {value}")
        return value

    @field_validator("org_guidelines", mode="before")
    @classmethod
    def _normalize_org_guidelines(cls, v: object) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("org_guidelines must be a string")
        text = v.strip()
        if not text:
            return None
        if len(text) > ORG_GUIDELINES_MAX_CHARS:
            raise ValueError(
                f"org_guidelines must be at most {ORG_GUIDELINES_MAX_CHARS} characters"
            )
        return text

    @field_validator("review_tracing_project", mode="before")
    @classmethod
    def _normalize_review_tracing_project(cls, v: object) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("review_tracing_project must be a string")
        text = v.strip()
        if not text:
            return None
        if len(text) > REVIEW_TRACING_PROJECT_MAX_CHARS:
            raise ValueError(
                "review_tracing_project must be at most "
                f"{REVIEW_TRACING_PROJECT_MAX_CHARS} characters"
            )
        return text

    @model_validator(mode="after")
    def _validate_model_pairs(self) -> TeamSettingsUpdate:
        for model_field, effort_field in _MODEL_PAIR_FIELDS:
            model = getattr(self, model_field)
            effort = getattr(self, effort_field)
            normalized = _normalize_stale_model_pair(model, effort)
            if normalized != (model, effort):
                setattr(self, model_field, normalized[0])
                setattr(self, effort_field, normalized[1])
        _validate_model_effort_pair(
            self.default_agent_model, self.default_agent_reasoning_effort, "agent"
        )
        _validate_model_effort_pair(
            self.default_agent_subagent_model,
            self.default_agent_subagent_reasoning_effort,
            "agent subagent",
        )
        _validate_model_effort_pair(
            self.default_reviewer_model, self.default_reviewer_reasoning_effort, "reviewer"
        )
        _validate_model_effort_pair(
            self.default_reviewer_subagent_model,
            self.default_reviewer_subagent_reasoning_effort,
            "reviewer subagent",
        )
        _validate_model_effort_pair(
            self.default_grouping_model,
            self.default_grouping_reasoning_effort,
            "review diff grouping",
        )
        _validate_model_effort_pair(
            self.default_chat_model, self.default_chat_reasoning_effort, "review chat"
        )
        return self


def _validate_model_effort_pair(model: str | None, effort: str | None, role: str) -> None:
    if model is None and effort is None:
        return
    if model is None:
        raise ValueError(f"{role} reasoning effort set without a model")
    if model not in SUPPORTED_MODEL_IDS:
        raise ValueError(f"unsupported {role} model: {model}")
    if effort is None or not model_supports_effort(model, effort):
        raise ValueError(f"effort {effort!r} not supported by {role} model {model!r}")


_RETIRED_MODEL_REPLACEMENTS: dict[str, str] = {}


def _normalize_stale_model_pair(
    model: str | None, effort: str | None
) -> tuple[str | None, str | None]:
    if model is None:
        return model, effort
    return _RETIRED_MODEL_REPLACEMENTS.get(model, model), effort


_MODEL_PAIR_FIELDS: tuple[tuple[str, str], ...] = (
    ("default_agent_model", "default_agent_reasoning_effort"),
    ("default_agent_subagent_model", "default_agent_subagent_reasoning_effort"),
    ("default_reviewer_model", "default_reviewer_reasoning_effort"),
    ("default_reviewer_subagent_model", "default_reviewer_subagent_reasoning_effort"),
    ("default_grouping_model", "default_grouping_reasoning_effort"),
    ("default_chat_model", "default_chat_reasoning_effort"),
)


def _normalize_auto_merge_mode(value: object) -> AutoMergeMode:
    if value == AUTO_MERGE_ALWAYS:
        return AUTO_MERGE_ALWAYS
    if value == AUTO_MERGE_ON_PLAN_APPROVAL:
        return AUTO_MERGE_ON_PLAN_APPROVAL
    return AUTO_MERGE_NEVER


def normalize_team_settings_for_response(settings: dict[str, Any]) -> dict[str, Any]:
    value = dict(settings)
    value["auto_merge_mode"] = _normalize_auto_merge_mode(value.get("auto_merge_mode"))
    for model_field, effort_field in _MODEL_PAIR_FIELDS:
        model = value.get(model_field)
        effort = value.get(effort_field)
        if isinstance(model, str):
            value[model_field], value[effort_field] = _normalize_stale_model_pair(
                model,
                effort if isinstance(effort, str) else None,
            )
    return value


def _client():
    return get_client()


def _env_default_repo() -> str | None:
    owner = os.environ.get("DEFAULT_REPO_OWNER", "").strip()
    name = os.environ.get("DEFAULT_REPO_NAME", "").strip()
    return f"{owner}/{name}" if owner and name else None


def _parse_repo(value: object) -> dict[str, str] | None:
    if not isinstance(value, str):
        return None
    owner, sep, name = value.strip().partition("/")
    if not sep or not owner.strip() or not name.strip():
        return None
    return {"owner": owner.strip(), "name": name.strip()}


def _default_settings() -> dict[str, Any]:
    fallback_model, fallback_effort = default_model_pair()
    return {
        "review_draft_prs": False,
        "pr_summaries": True,
        "review_trace_links": True,
        "gateway_enabled": None,
        "autofix_enabled": False,
        "autofix_severity_threshold": "medium",
        "require_plan_approval": False,
        "auto_merge_mode": AUTO_MERGE_NEVER,
        "fable_enabled": False,
        "review_tracing_project": None,
        "org_guidelines": None,
        "default_agent_model": fallback_model,
        "default_agent_reasoning_effort": fallback_effort,
        "default_agent_subagent_model": fallback_model,
        "default_agent_subagent_reasoning_effort": fallback_effort,
        "default_repo": _env_default_repo(),
        "default_reviewer_model": fallback_model,
        "default_reviewer_reasoning_effort": fallback_effort,
        "default_reviewer_subagent_model": fallback_model,
        "default_reviewer_subagent_reasoning_effort": fallback_effort,
        # No hardcoded grouping default: unset means "inherit the Reviewer
        # subagent default".
        "default_grouping_model": None,
        "default_grouping_reasoning_effort": None,
        # No hardcoded chat default: unset means "inherit the Agent default".
        "default_chat_model": None,
        "default_chat_reasoning_effort": None,
        "plan_profile": None,
        "review_profile": None,
        "reviewer_routing": None,
        "updated_at": None,
    }


_PERSISTED_SETTING_FIELDS: tuple[str, ...] = tuple(
    field_name for field_name in _default_settings() if field_name != "updated_at"
)

# The merge in upsert_team_settings reads each persisted field off the update
# model, so a stored-only default would blow up every PUT. Fail at import
# instead, where it's obvious which of the two lists is missing the field.
assert set(_PERSISTED_SETTING_FIELDS) <= set(TeamSettingsUpdate.model_fields), (
    "every persisted setting needs a matching TeamSettingsUpdate field: "
    f"{sorted(set(_PERSISTED_SETTING_FIELDS) - set(TeamSettingsUpdate.model_fields))}"
)


async def _get_stored_team_settings(*, raise_on_error: bool = False) -> dict[str, Any]:
    try:
        item = await _client().store.get_item(TEAM_SETTINGS_NAMESPACE, TEAM_SETTINGS_KEY)
    except Exception as e:
        logger.debug("team settings lookup failed: %s", e)
        if raise_on_error:
            raise
        return {}
    value = item.get("value") if isinstance(item, dict) else getattr(item, "value", None)
    return value if isinstance(value, dict) else {}


async def get_team_settings(
    *,
    raise_on_error: bool = False,
    preserve_unset_model_pairs: bool = False,
) -> dict[str, Any]:
    defaults = _default_settings()
    value = (
        await _get_stored_team_settings(raise_on_error=True)
        if raise_on_error
        else await _get_stored_team_settings()
    )
    # Skip None-valued model fields so legacy records (or PUTs that cleared the
    # selection) still surface the hardcoded default instead of a null.
    overlay = {k: v for k, v in value.items() if v is not None}
    merged = {**defaults, **overlay}
    if preserve_unset_model_pairs:
        for model_field, effort_field in _MODEL_PAIR_FIELDS:
            if value.get(model_field) is None:
                merged[model_field] = None
            if value.get(effort_field) is None:
                merged[effort_field] = None
    # These current opt-in fields were in the stale-field purge from older
    # settings shapes and must survive the response.
    for stale_field in (
        "trigger_mode",
        "autofix_mode",
        "review_author_context_enabled",
    ):
        merged.pop(stale_field, None)
    return normalize_team_settings_for_response(merged)


async def upsert_team_settings(update: TeamSettingsUpdate) -> dict[str, Any]:
    """Persist a partial update: omitted fields keep their stored value, an
    explicitly supplied ``None`` clears the setting.

    This is a read-modify-write, so a swallowed store read would leave ``stored``
    empty and quietly overwrite every unsupplied field with a default — the same
    erasure this function exists to prevent. Fail the write instead.
    """
    stored = await _get_stored_team_settings(raise_on_error=True)
    defaults = _default_settings()
    supplied_fields = update.supplied_fields
    value: dict[str, Any] = {}
    for field_name in _PERSISTED_SETTING_FIELDS:
        if supplied_fields is None or field_name in supplied_fields:
            value[field_name] = getattr(update, field_name)
        else:
            value[field_name] = stored.get(field_name, defaults[field_name])
            if field_name == "auto_merge_mode":
                value[field_name] = _normalize_auto_merge_mode(value[field_name])

    # Normalize only pairs the caller touched in this update. An untouched pair
    # is preserved stored state and must round-trip byte-for-byte even when it
    # is stale or invalid (e.g. a retired model id): clearing it here would let
    # an unrelated one-field update jump that role's model cross-provider on
    # read — the OSWE-222 erasure class. _resolve_default_pair already repairs
    # stale pairs at read time.
    for model_field, effort_field in _MODEL_PAIR_FIELDS:
        if supplied_fields is not None and supplied_fields.isdisjoint((model_field, effort_field)):
            continue
        model = value[model_field]
        effort = value[effort_field]
        if not isinstance(model, str):
            value[model_field] = None
            value[effort_field] = None
        elif not isinstance(effort, str) or not model_supports_effort(model, effort):
            default_effort = model_default_effort(model)
            if default_effort is None:
                value[model_field] = None
            value[effort_field] = default_effort

    value["updated_at"] = datetime.now(UTC).isoformat()
    if not value["fable_enabled"]:
        for model_field, effort_field in _MODEL_PAIR_FIELDS:
            model = value[model_field]
            if isinstance(model, str) and model in FABLE_MODEL_IDS:
                value[model_field], value[effort_field] = gate_fable_model(
                    model, value[effort_field], fable_enabled=False
                )
    await _client().store.put_item(TEAM_SETTINGS_NAMESPACE, TEAM_SETTINGS_KEY, value)
    return value


async def get_team_default_repo() -> dict[str, str] | None:
    settings = await get_team_settings()
    return _parse_repo(settings.get("default_repo"))


async def get_team_stage_profile(stage: Literal["plan", "review"]) -> str | None:
    """Return the selected stage profile name, or None for the default."""
    settings = await get_team_settings()
    value = settings.get(f"{stage}_profile")
    return value.strip() if isinstance(value, str) and value.strip() else None


async def get_team_default_model(
    role: Literal["agent", "reviewer", "chat"],
) -> tuple[str, str]:
    """Return the team-wide default ``(model_id, reasoning_effort)`` for ``role``.

    Always returns a valid pair, resolved in order: the admin-configured pair if
    still supported; otherwise the newest supported model for the same provider
    (so a stale Anthropic/OpenAI selection stays on its provider rather than
    jumping cross-provider); otherwise the hardcoded global default from
    :func:`agent.dashboard.options.default_model_pair`.

    ``"chat"`` (the review-page PR chat) has no hardcoded default: when its
    admin setting is unset/invalid it inherits the team **agent** default.
    """
    settings = await get_team_settings(preserve_unset_model_pairs=True)
    if role == "chat":
        model = settings.get("default_chat_model")
        effort = settings.get("default_chat_reasoning_effort")
        if _is_supported_pair(model, effort):
            return _resolve_default_pair(
                model,
                effort,
                surface="chat",
                setting="default_chat_model",
            )
        # Inherit the Agent default when no chat-specific model is configured.
        model = settings.get("default_agent_model")
        effort = settings.get("default_agent_reasoning_effort")
        surface = "chat"
        setting = "default_agent_model"
    elif role == "agent":
        model = settings.get("default_agent_model")
        effort = settings.get("default_agent_reasoning_effort")
        surface = "agent"
        setting = "default_agent_model"
    else:
        model = settings.get("default_reviewer_model")
        effort = settings.get("default_reviewer_reasoning_effort")
        surface = "reviewer"
        setting = "default_reviewer_model"
    return _resolve_default_pair(model, effort, surface=surface, setting=setting)


async def get_team_default_model_pair(
    role: Literal["agent", "reviewer"],
) -> tuple[tuple[str, str], tuple[str, str]]:
    """Return default ``(main, subagent)`` model pairs for ``role`` from one store read."""
    settings = await get_team_settings(preserve_unset_model_pairs=True)
    if role == "agent":
        main = _resolve_default_pair(
            settings.get("default_agent_model"),
            settings.get("default_agent_reasoning_effort"),
            surface="agent",
            setting="default_agent_model",
        )
        subagent = _resolve_default_pair(
            settings.get("default_agent_subagent_model"),
            settings.get("default_agent_subagent_reasoning_effort"),
            surface="agent_subagent",
            setting="default_agent_subagent_model",
        )
    else:
        main = _resolve_default_pair(
            settings.get("default_reviewer_model"),
            settings.get("default_reviewer_reasoning_effort"),
            surface="reviewer",
            setting="default_reviewer_model",
        )
        subagent = _resolve_default_pair(
            settings.get("default_reviewer_subagent_model"),
            settings.get("default_reviewer_subagent_reasoning_effort"),
            surface="reviewer_subagent",
            setting="default_reviewer_subagent_model",
        )
    return main, subagent


async def get_team_default_grouping_model() -> tuple[str, str]:
    """Return the team-wide default ``(model_id, reasoning_effort)`` for the
    review diff-grouping pass.

    When no grouping-specific model is configured (or it's no longer
    supported), inherit the team **reviewer subagent** default — the grouping
    pass is a cheap, fast companion to the reviewer, so it should track that
    cheaper tier rather than the primary reviewer model.
    """
    settings = await get_team_settings(preserve_unset_model_pairs=True)
    model = settings.get("default_grouping_model")
    effort = settings.get("default_grouping_reasoning_effort")
    if _is_supported_pair(model, effort):
        return _resolve_default_pair(
            model,
            effort,
            surface="grouping",
            setting="default_grouping_model",
        )
    return _resolve_default_pair(
        settings.get("default_reviewer_subagent_model"),
        settings.get("default_reviewer_subagent_reasoning_effort"),
        surface="grouping",
        setting="default_reviewer_subagent_model",
    )


async def get_team_autofix_settings() -> tuple[bool, str]:
    """Return the review auto-fix enabled flag and severity threshold."""
    settings = await get_team_settings()
    threshold = settings.get("autofix_severity_threshold")
    if threshold not in ("low", "medium", "high", "critical"):
        threshold = "medium"
    return settings.get("autofix_enabled") is True, threshold


async def get_team_require_plan_approval() -> bool:
    """Read the plan gate policy without converting store failures to the default."""
    settings = await get_team_settings(raise_on_error=True)
    return settings.get("require_plan_approval") is True


async def get_team_auto_merge_mode() -> AutoMergeMode:
    """Read the auto-merge policy, defaulting invalid legacy values to Never."""
    settings = await get_team_settings(raise_on_error=True)
    return _normalize_auto_merge_mode(settings.get("auto_merge_mode"))


async def get_team_review_trace_links_enabled() -> bool:
    """Return whether GitHub review bodies should include a LangSmith trace link."""
    settings = await get_team_settings()
    return bool(settings.get("review_trace_links", True))


async def get_team_gateway_enabled() -> bool | None:
    """Return the stored LLM Gateway toggle (``None`` means inherit the env default)."""
    settings = await get_team_settings()
    value = settings.get("gateway_enabled")
    return value if isinstance(value, bool) else None


async def get_team_fable_enabled() -> bool:
    """Return whether Fable models are enabled for the team."""
    settings = await get_team_settings()
    value = settings.get("fable_enabled")
    return bool(value) if isinstance(value, bool) else False


async def get_effective_gateway_enabled() -> bool:
    """Resolve whether LLM Gateway routing is on: team setting, else env default."""
    return resolve_gateway_enabled(await get_team_gateway_enabled())


async def get_team_review_tracing_project() -> str | None:
    """Return the LangSmith tracing project used for PR trace resolution."""
    settings = await get_team_settings()
    value = settings.get("review_tracing_project")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


async def get_org_review_guidelines() -> str | None:
    """Return the org-wide reviewer guidelines supplement, if configured."""
    settings = await get_team_settings()
    value = settings.get("org_guidelines")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


async def get_team_default_subagent_model(
    role: Literal["agent", "reviewer"],
) -> tuple[str, str]:
    """Return the team-wide default subagent ``(model_id, reasoning_effort)`` for ``role``."""
    settings = await get_team_settings(preserve_unset_model_pairs=True)
    if role == "agent":
        model = settings.get("default_agent_subagent_model")
        effort = settings.get("default_agent_subagent_reasoning_effort")
        surface = "agent_subagent"
        setting = "default_agent_subagent_model"
    else:
        model = settings.get("default_reviewer_subagent_model")
        effort = settings.get("default_reviewer_subagent_reasoning_effort")
        surface = "reviewer_subagent"
        setting = "default_reviewer_subagent_model"
    return _resolve_default_pair(model, effort, surface=surface, setting=setting)


def _is_supported_pair(model: object, effort: object) -> bool:
    return (
        isinstance(model, str)
        and isinstance(effort, str)
        and model in SUPPORTED_MODEL_IDS
        and model_supports_effort(model, effort)
    )


def _resolve_pair_tier(
    model: object,
    effort: object,
) -> tuple[Literal["none", "provider", "product_default"], tuple[str, str]]:
    """Resolve a model pair and identify which fallback tier supplied it."""
    if _is_supported_pair(model, effort):
        assert isinstance(model, str) and isinstance(effort, str)
        return "none", (model, effort)
    provider_pair = provider_fallback_pair(model, effort)
    if provider_pair is not None:
        return "provider", provider_pair
    return "product_default", default_model_pair()


def _resolve_default_pair(
    model: object,
    effort: object,
    *,
    surface: str,
    setting: str,
) -> tuple[str, str]:
    """Resolve a production model pair and warn when the product default supplies it."""
    fallback, resolved = _resolve_pair_tier(model, effort)
    if fallback == "product_default":
        resolved_model, resolved_effort = resolved
        logger.warning(
            "model resolution fell back to product default for %s "
            "(%s unset or invalid); resolved %s/%s",
            surface,
            setting,
            resolved_model,
            resolved_effort,
            extra={
                "model_resolution_fallback": {
                    "surface": surface,
                    "setting": setting,
                    "resolved_model": resolved_model,
                    "resolved_effort": resolved_effort,
                }
            },
        )
    return resolved


def _model_resolution_diagnostic(
    *,
    surface: str,
    setting: str,
    model: object,
    effort: object,
) -> dict[str, Any]:
    fallback, (resolved_model, resolved_effort) = _resolve_pair_tier(model, effort)
    return {
        "surface": surface,
        "setting": setting,
        "fallback": fallback,
        "resolved_model": resolved_model,
        "resolved_effort": resolved_effort,
    }


async def get_team_model_resolution_diagnostics() -> list[dict[str, Any]]:
    """Report all production model resolutions without emitting fallback warnings."""
    settings = await get_team_settings(preserve_unset_model_pairs=True)
    diagnostics = [
        _model_resolution_diagnostic(
            surface="agent",
            setting="default_agent_model",
            model=settings.get("default_agent_model"),
            effort=settings.get("default_agent_reasoning_effort"),
        ),
        _model_resolution_diagnostic(
            surface="agent_subagent",
            setting="default_agent_subagent_model",
            model=settings.get("default_agent_subagent_model"),
            effort=settings.get("default_agent_subagent_reasoning_effort"),
        ),
        _model_resolution_diagnostic(
            surface="reviewer",
            setting="default_reviewer_model",
            model=settings.get("default_reviewer_model"),
            effort=settings.get("default_reviewer_reasoning_effort"),
        ),
        _model_resolution_diagnostic(
            surface="reviewer_subagent",
            setting="default_reviewer_subagent_model",
            model=settings.get("default_reviewer_subagent_model"),
            effort=settings.get("default_reviewer_subagent_reasoning_effort"),
        ),
    ]

    grouping_model = settings.get("default_grouping_model")
    grouping_effort = settings.get("default_grouping_reasoning_effort")
    if _is_supported_pair(grouping_model, grouping_effort):
        grouping_setting = "default_grouping_model"
    else:
        grouping_setting = "default_reviewer_subagent_model"
        grouping_model = settings.get("default_reviewer_subagent_model")
        grouping_effort = settings.get("default_reviewer_subagent_reasoning_effort")
    diagnostics.append(
        _model_resolution_diagnostic(
            surface="grouping",
            setting=grouping_setting,
            model=grouping_model,
            effort=grouping_effort,
        )
    )

    chat_model = settings.get("default_chat_model")
    chat_effort = settings.get("default_chat_reasoning_effort")
    if _is_supported_pair(chat_model, chat_effort):
        chat_setting = "default_chat_model"
    else:
        chat_setting = "default_agent_model"
        chat_model = settings.get("default_agent_model")
        chat_effort = settings.get("default_agent_reasoning_effort")
    diagnostics.append(
        _model_resolution_diagnostic(
            surface="chat",
            setting=chat_setting,
            model=chat_model,
            effort=chat_effort,
        )
    )
    return diagnostics
