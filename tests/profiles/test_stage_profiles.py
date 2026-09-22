from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from deepagents import create_deep_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import ToolNode

from agent.middleware.exclude_tools import ExcludeToolsMiddleware
from agent.middleware.plan_mode import PlanModeMiddleware
from agent.prompt import PLAN_MODE_SECTION, construct_system_prompt
from agent.reviewer import (
    REVIEW_STAGE_TOOL_NAMES,
    REVIEWER_PROMPT_TEMPLATE,
    _reviewer_subagent,
    _reviewer_system_prompt,
)
from agent.server import PLAN_STAGE_TOOL_NAMES
from agent.utils.deferred_model import DeferredErrorModel
from agent.utils.stage_profiles import (
    DEEP_AGENT_TOOL_NAMES,
    StageProfileError,
    load_bundled_stage_profile_body,
    load_stage_profile,
    resolve_stage_profile,
)


def _write_profile(root: Path, stage: str, name: str, frontmatter: str, body: str) -> None:
    directory = root / stage
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")


def test_default_plan_profile_is_byte_identical() -> None:
    profile = load_stage_profile("plan", "default", allowed_tools=PLAN_STAGE_TOOL_NAMES)

    assert PLAN_MODE_SECTION == load_bundled_stage_profile_body("plan")
    for body in (profile.body, PLAN_MODE_SECTION):
        assert "\n### Challenge\n" in body
        assert "\n### Unverified claims\n" in body
        assert "\n### Questions\n" in body
        normalized_body = " ".join(body.split())
        assert (
            "Do not narrate successful verifications anywhere — only refutations, "
            "ambiguities, and unverifiables appear in the plan." in normalized_body
        )
        assert "Include the full plan Markdown when it fits" in normalized_body
        assert "truncate the non-Challenge plan content to fit" in normalized_body
        assert "include the Challenge section verbatim" in normalized_body
        assert "explicit exception to the general Slack terseness rule" in normalized_body
        assert "retain at least a one-line Challenge summary" in normalized_body
        assert "post a brief completion message" not in normalized_body
    assert profile.body == PLAN_MODE_SECTION
    assert construct_system_prompt(working_dir="/work", plan_mode=True) == construct_system_prompt(
        working_dir="/work",
        plan_mode=True,
        plan_profile_body=profile.body,
    )


def test_default_review_profile_is_byte_identical() -> None:
    profile = load_stage_profile("review", "default", allowed_tools=REVIEW_STAGE_TOOL_NAMES)

    assert REVIEWER_PROMPT_TEMPLATE == load_bundled_stage_profile_body("review")
    assert profile.body == REVIEWER_PROMPT_TEMPLATE
    expected = _reviewer_system_prompt(
        "/work/repo", repo_owner="acme", repo_name="repo", pr_number=7
    )
    actual = _reviewer_system_prompt(
        "/work/repo",
        repo_owner="acme",
        repo_name="repo",
        pr_number=7,
        profile_body=profile.body,
    )
    assert actual == expected


def test_deep_agent_tool_names_match_constructed_agent() -> None:
    graph = create_deep_agent(
        model=DeferredErrorModel(error_message="unused"),
        system_prompt="",
        tools=[],
    )
    tool_node = cast(ToolNode, graph.nodes["tools"].bound)

    assert frozenset(tool_node.tools_by_name) == DEEP_AGENT_TOOL_NAMES


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("model: openai:gpt-6-sol\nBody.\n", "missing opening frontmatter delimiter"),
        ("---\nmodel: openai:gpt-6-sol\n", "unterminated frontmatter block"),
        ("---\nmodel: [\n---\nBody.\n", "invalid YAML"),
        ("---\n- model\n---\nBody.\n", "frontmatter must be a mapping"),
    ],
)
def test_stage_profile_reports_shared_frontmatter_errors(
    tmp_path: Path,
    content: str,
    expected: str,
) -> None:
    path = tmp_path / "plan" / "broken.md"
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")

    with pytest.raises(StageProfileError, match=expected):
        load_stage_profile("plan", "broken", allowed_tools=PLAN_STAGE_TOOL_NAMES, root=tmp_path)


def test_non_default_profile_changes_assembled_prompt(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "plan",
        "concise-v2",
        "tools:\n  - read_file\n  - save_plan",
        "CUSTOM PLAN PROFILE at {plan_url}",
    )
    profile = load_stage_profile(
        "plan", "concise-v2", allowed_tools=PLAN_STAGE_TOOL_NAMES, root=tmp_path
    )

    prompt = construct_system_prompt(
        working_dir="/work",
        plan_mode=True,
        plan_url="https://plans.example/7",
        plan_profile_body=profile.body,
    )

    assert "CUSTOM PLAN PROFILE at https://plans.example/7" in prompt
    assert "Plan Mode (ACTIVE)" not in prompt


def test_profile_honors_model_effort_and_tool_restriction(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "review",
        "focused",
        "model: openai:gpt-6-sol\nreasoning_effort: medium\ntools:\n  - read_file\n  - add_finding",
        "Review {repo_owner}/{repo_name}#{pr_number}.",
    )

    profile = load_stage_profile(
        "review", "focused", allowed_tools=REVIEW_STAGE_TOOL_NAMES, root=tmp_path
    )

    assert profile.model == "openai:gpt-6-sol"
    assert profile.reasoning_effort == "medium"
    assert profile.tools == ("read_file", "add_finding")


def test_profile_rejects_additive_tool(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "plan",
        "additive",
        "tools:\n  - open_pull_request",
        "Plan safely.",
    )

    with pytest.raises(StageProfileError, match="profiles may only restrict tools"):
        load_stage_profile("plan", "additive", allowed_tools=PLAN_STAGE_TOOL_NAMES, root=tmp_path)


@pytest.mark.parametrize(
    "body",
    ["Review {repo_owner:{missing}}.", "Review {repo_owner!x}."],
)
def test_profile_rejects_unrenderable_template(body: str, tmp_path: Path) -> None:
    _write_profile(tmp_path, "review", "broken-format", "{}", body)

    with pytest.raises(StageProfileError, match="cannot be rendered safely"):
        load_stage_profile(
            "review", "broken-format", allowed_tools=REVIEW_STAGE_TOOL_NAMES, root=tmp_path
        )


def test_profile_rejects_unknown_capability_fields(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "review",
        "capabilities",
        "mcp_servers:\n  - internal\nsubagents:\n  - audit",
        "Review carefully.",
    )

    with pytest.raises(StageProfileError) as exc_info:
        load_stage_profile(
            "review", "capabilities", allowed_tools=REVIEW_STAGE_TOOL_NAMES, root=tmp_path
        )

    assert "unknown frontmatter key 'mcp_servers'" in str(exc_info.value)
    assert "unknown frontmatter key 'subagents'" in str(exc_info.value)
    assert "cannot declare capabilities" in str(exc_info.value)


def test_invalid_selection_falls_back_to_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_profile(tmp_path, "plan", "default", "{}", PLAN_MODE_SECTION)
    _write_profile(tmp_path, "plan", "broken", "tools:\n  - open_pull_request", "Broken.")

    profile = resolve_stage_profile(
        "plan",
        "broken",
        allowed_tools=PLAN_STAGE_TOOL_NAMES,
        fallback_body=PLAN_MODE_SECTION,
        root=tmp_path,
    )

    assert profile.name == "default"
    assert profile.body == PLAN_MODE_SECTION
    assert "falling back to default" in caplog.text


class _Request:
    def __init__(
        self,
        tools: list[dict[str, str]],
        state: dict[str, Any] | None = None,
        *,
        model: object | None = None,
        system_message: SystemMessage | None = None,
    ) -> None:
        self.tools = tools
        self.state = state or {}
        self.model = model
        self.system_message = system_message

    def override(self, **kwargs: Any) -> _Request:
        return _Request(
            kwargs.get("tools", self.tools),
            self.state,
            model=kwargs.get("model", self.model),
            system_message=kwargs.get("system_message", self.system_message),
        )


def test_plan_profile_tool_restriction_is_applied_at_model_request() -> None:
    middleware = PlanModeMiddleware(
        excluded=frozenset(), allowed=frozenset({"read_file", "save_plan"}), initial=True
    )
    request = _Request([{"name": "read_file"}, {"name": "save_plan"}, {"name": "web_search"}])

    filtered = cast(_Request, middleware._filter(cast(Any, request)))

    assert [tool["name"] for tool in filtered.tools] == ["read_file", "save_plan"]


def test_review_profile_tool_restriction_is_applied_at_model_request() -> None:
    middleware = ExcludeToolsMiddleware(allowed=frozenset({"read_file", "add_finding"}))
    request = _Request([{"name": "read_file"}, {"name": "add_finding"}, {"name": "execute"}])

    filtered = cast(_Request, middleware._filter(cast(Any, request)))

    assert [tool["name"] for tool in filtered.tools] == ["read_file", "add_finding"]


@pytest.mark.asyncio
async def test_plan_profile_applies_after_mid_run_enter_plan_mode() -> None:
    base_model = cast(BaseChatModel, MagicMock(name="base_model"))
    plan_model = cast(BaseChatModel, MagicMock(name="plan_model"))
    middleware = PlanModeMiddleware(
        excluded=frozenset(),
        model=plan_model,
        base_model=base_model,
        prompt="CUSTOM PLAN PROFILE",
        initial=False,
    )
    captured: list[_Request] = []

    async def handler(request: Any) -> Any:
        captured.append(cast(_Request, request))
        return MagicMock()

    before_entry = _Request(
        [{"name": "read_file"}],
        {"plan_mode": False},
        model=base_model,
        system_message=SystemMessage(content="BASE PROMPT"),
    )
    after_entry = _Request(
        [{"name": "read_file"}],
        {"plan_mode": True},
        model=base_model,
        system_message=SystemMessage(content="BASE PROMPT"),
    )

    await middleware.awrap_model_call(cast(Any, before_entry), handler)
    await middleware.awrap_model_call(cast(Any, after_entry), handler)

    assert captured[0].model is base_model
    assert captured[0].system_message is not None
    assert captured[0].system_message.text == "BASE PROMPT"
    assert captured[1].model is plan_model
    assert captured[1].system_message is not None
    assert captured[1].system_message.text == "CUSTOM PLAN PROFILE\n\nBASE PROMPT"


def test_reviewer_subagent_uses_profile_tool_restriction() -> None:
    subagent = _reviewer_subagent(
        cast(BaseChatModel, MagicMock()), allowed_tools=frozenset({"read_file"})
    )
    middleware = subagent.get("middleware", [])
    assert middleware
    restriction = cast(ExcludeToolsMiddleware, middleware[0])
    request = _Request([{"name": "read_file"}, {"name": "execute"}])

    filtered = cast(_Request, restriction._filter(cast(Any, request)))

    assert [tool["name"] for tool in filtered.tools] == ["read_file"]
