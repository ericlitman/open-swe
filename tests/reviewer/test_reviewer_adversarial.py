from __future__ import annotations

import asyncio
import importlib
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain.agents.middleware import AgentState
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.constants import CONFIG_KEY_CHECKPOINTER, Send
from langgraph.graph.state import RunnableConfig
from langgraph.runtime import Runtime
from pydantic import Field

from agent.middleware import ExcludeToolsMiddleware
from agent.review.trace_context import PRTraceContext
from agent.reviewer import (
    REVIEW_STAGE_TOOL_NAMES,
    REVIEWER_EVAL_PROMPT_SUFFIX,
    REVIEWER_PROMPT_TEMPLATE,
    ReviewContextBundle,
    _fetch_existing_threads_block,
    _format_parent_review_context,
    _repo_checkout_note,
)
from agent.reviewer_adversarial import (
    RESERVED_SUBAGENT_TOOLS,
    PrepareAdversarialReviewerRunMiddleware,
    _active_finder_names,
    _bounded_agent,
    _finder_payload,
    _finder_prompt,
    _judgment_context,
    _prepare_context,
    _render_parent_prompt,
    _run_stage,
    get_reviewer_adversarial_agent,
)
from agent.utils.agent_definitions import (
    build_subagents,
    list_agent_definitions,
    load_agent_definition,
)
from agent.utils.stage_profiles import StageProfile, load_stage_profile


def test_registered_in_langgraph_json_and_importable() -> None:
    config = json.loads(Path("langgraph.json").read_text(encoding="utf-8"))
    assert config["graphs"]["reviewer_adversarial"] == (
        "agent.graphs.reviewer_adversarial:traced_reviewer_adversarial"
    )

    module = importlib.import_module("agent.graphs.reviewer_adversarial")
    assert hasattr(module, "traced_reviewer_adversarial")
    assert hasattr(module, "get_reviewer_adversarial_agent")


def test_shipped_definition_shape() -> None:
    definition = load_agent_definition("reviewer-adversarial")
    assert definition.description
    assert definition.tools == (
        "fetch_review_diff",
        "add_finding",
        "update_finding",
        "list_findings",
        "publish_review",
        "resolve_finding_thread",
        "reply_to_finding_thread",
        "web_search",
        "fetch_url",
        "http_request",
    )
    assert tuple(subagent.name for subagent in definition.subagents) == (
        "adjudicator",
        "conventions",
        "correctness",
        "security",
    )
    assert all(subagent.tools == () for subagent in definition.subagents)
    prompt_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("agent/reviewer-adversarial").rglob("*.md")
    )
    assert "zero-findings re-walk" not in prompt_text
    assert "same-file independence" not in prompt_text
    assert "top changed" not in prompt_text

    model = cast(BaseChatModel, MagicMock())
    specs = build_subagents(
        definition,
        model=model,
        reserved_tools=RESERVED_SUBAGENT_TOOLS,
    )
    assert all(spec.get("tools") == [] for spec in specs)


def test_conventions_roster_filter_and_finder_context_isolation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="agent.reviewer_adversarial")
    finder_names = ["conventions", "correctness", "security"]
    assert (
        _active_finder_names(finder_names, agents_md_content="ROOT RULE", scoped_agents_md={})
        == finder_names
    )
    assert (
        _active_finder_names(
            finder_names, agents_md_content=None, scoped_agents_md={"src/AGENTS.md": "SCOPED"}
        )
        == finder_names
    )
    assert _active_finder_names(finder_names, agents_md_content=None, scoped_agents_md={}) == [
        "correctness",
        "security",
    ]
    assert "Skipping conventions finder" in caplog.text

    state = {
        "diff_path": "/tmp/review.diff",
        "working_dir": "/workspace/repo",
        "stage_context": "context",
        "agents_md_content": "ROOT RULE",
        "scoped_agents_md": {"src/AGENTS.md": "SCOPED RULE"},
        "skill_sources": ["/workspace/.open-swe/trusted-skills/.agents/skills/"],
    }
    correctness = _finder_payload("correctness", state)
    conventions = _finder_payload("conventions", state)
    assert "agents_md_content" not in correctness
    assert "scoped_agents_md" not in correctness
    assert correctness["skill_sources"] == state["skill_sources"]
    assert conventions["agents_md_content"] == "ROOT RULE"
    assert conventions["scoped_agents_md"] == {"src/AGENTS.md": "SCOPED RULE"}
    assert conventions["skill_sources"] == state["skill_sources"]


def test_finder_prompt_preserves_empty_identity_and_distributes_paths_only() -> None:
    base_state = {
        "finder_name": "correctness",
        "diff_path": "/tmp/review.diff",
        "working_dir": "/workspace/repo",
        "stage_context": "context",
    }
    expected = (
        "Review the complete materialized diff at /tmp/review.diff against "
        "the checkout at /workspace/repo. Review context: context. "
        "Return only structured candidate defects."
    )
    assert _finder_prompt(base_state) == expected

    skill_root = "/workspace/.open-swe/trusted-skills/.agents/skills/"
    skill_prompt = _finder_prompt({**base_state, "skill_sources": [skill_root]})
    assert skill_root in skill_prompt
    assert "Grep these roots for the SKILL.md matching" in skill_prompt
    assert "ROOT RULE" not in skill_prompt

    conventions_prompt = _finder_prompt(
        {
            **base_state,
            "finder_name": "conventions",
            "skill_sources": [skill_root],
            "agents_md_content": "ROOT RULE",
            "scoped_agents_md": {
                "src/pkg/AGENTS.md": "DEEP RULE",
                "src/AGENTS.md": "SHALLOW RULE",
            },
        }
    )
    assert skill_root in conventions_prompt
    assert "ROOT RULE" in conventions_prompt
    assert conventions_prompt.index("src/AGENTS.md") < conventions_prompt.index("src/pkg/AGENTS.md")
    assert conventions_prompt.index("SHALLOW RULE") < conventions_prompt.index("DEEP RULE")


@pytest.mark.asyncio
async def test_graph_dispatches_conventions_only_with_instructions_and_isolates_prompts() -> None:
    from agent.review.adversarial import FinderOutput

    stages = [object() for _ in range(6)]
    stage_iter = iter(stages)
    prompts: list[tuple[object, str]] = []

    async def run_stage(graph: object, prompt: str, *_args: object, **_kwargs: object) -> Any:
        prompts.append((graph, prompt))
        if graph in {stages[1], stages[2], stages[3]}:
            return FinderOutput(candidates=[])
        raise AssertionError("unexpected judgment stage")

    config = cast(
        RunnableConfig,
        {"configurable": {"thread_id": "adversarial-thread", "__is_for_execution__": True}},
    )
    with (
        patch(
            "agent.reviewer_adversarial._cached_reviewer_team_defaults",
            new_callable=AsyncMock,
            return_value=(("team-main", "low"), ("team-sub", "low")),
        ),
        patch(
            "agent.reviewer_adversarial._cached_review_profile_name",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agent.reviewer_adversarial._cached_gateway_enabled",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "agent.reviewer_adversarial.get_team_fable_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("agent.reviewer_adversarial._make_model_or_defer", return_value=MagicMock()),
        patch(
            "agent.reviewer_adversarial._bounded_agent",
            side_effect=lambda **_kwargs: next(stage_iter),
        ),
        patch(
            "agent.reviewer_adversarial._prepare_context",
            new_callable=AsyncMock,
            return_value={
                "work_dir": "/workspace",
                "working_dir": "/workspace/repo",
                "rendered_system_prompt": "prompt",
                "stage_context": "finder context",
                "parent_review_context": "PARENT CONTEXT MARKER",
                "diff_text": "",
                "diff_line_set": None,
                "pr_title": "title",
                "diff_path": "/tmp/review.diff",
                "agents_md_content": "ROOT RULE",
                "scoped_agents_md": {"src/AGENTS.md": "SCOPED RULE"},
                "skill_sources": ["/workspace/.open-swe/trusted-skills/.agents/skills/"],
            },
        ),
        patch("agent.reviewer_adversarial._run_stage", side_effect=run_stage),
        patch(
            "agent.reviewer_adversarial.agent_tools.publish_review",
            new_callable=AsyncMock,
            return_value={"success": True, "review_id": 7},
        ),
        patch.object(
            __import__(
                "agent.reviewer_adversarial", fromlist=["settle_review_check_on_exit"]
            ).settle_review_check_on_exit,
            "aafter_agent",
            new_callable=AsyncMock,
        ),
    ):
        graph = await get_reviewer_adversarial_agent(config)
        result = await graph.ainvoke({"messages": []})

    assert result["finders_expected"] == ["conventions", "correctness", "security"]
    by_graph = dict(prompts)
    assert set(by_graph) == {stages[1], stages[2], stages[3]}
    assert "ROOT RULE" in by_graph[stages[1]]
    assert "SCOPED RULE" in by_graph[stages[1]]
    assert "SKILL.md matching" in by_graph[stages[1]]
    for finder in (stages[2], stages[3]):
        assert "ROOT RULE" not in by_graph[finder]
        assert "SCOPED RULE" not in by_graph[finder]
        assert "SKILL.md matching" in by_graph[finder]


def test_discovery_finds_exactly_the_shipped_definition() -> None:
    assert list_agent_definitions() == ("reviewer-adversarial",)


@pytest.mark.asyncio
async def test_config_isolation() -> None:
    callback = object()
    configurable_value = object()
    callbacks = [callback]
    config = cast(
        RunnableConfig,
        {
            "configurable": {
                "thread_id": None,
                "custom_key": configurable_value,
            },
            "callbacks": callbacks,
            "recursion_limit": 25,
        },
    )
    fake = MagicMock()
    fake.with_config = MagicMock(return_value=fake)

    with patch("agent.reviewer_adversarial.create_deep_agent", return_value=fake):
        await get_reviewer_adversarial_agent(config)

        bound = cast(RunnableConfig, fake.with_config.call_args.args[0])
        bound_configurable = cast(dict[str, object], bound.get("configurable"))
        original_configurable = cast(dict[str, object], config.get("configurable"))
        assert bound is not config
        assert bound_configurable is not original_configurable
        assert bound_configurable["custom_key"] is configurable_value
        assert bound.get("callbacks") is callbacks
        assert config.get("recursion_limit") == 25

        default_config: RunnableConfig = {"configurable": {"thread_id": None}}
        await get_reviewer_adversarial_agent(default_config)
        default_bound = cast(RunnableConfig, fake.with_config.call_args.args[0])
        assert "recursion_limit" not in default_config
        assert "recursion_limit" in default_bound


def _empty_review_bundle() -> ReviewContextBundle:
    return ReviewContextBundle(
        sandbox_backend=MagicMock(),
        github_token="token",
        work_dir="/workspace",
        repo_owner="owner",
        repo_name="repo",
        pr_number=7,
        pr_url="https://github.com/owner/repo/pull/7",
        base_sha="a" * 40,
        head_sha="b" * 40,
        repo_ready=True,
        reviewer_eval=False,
        diff_text="diff",
        diff_line_set={},
        pr_title="title",
        pr_body="body",
    )


@pytest.mark.parametrize(
    ("field", "value", "marker"),
    [
        (
            "existing_threads_block",
            '<pr_review_threads><thread location="a.py:1" /></pr_review_threads>',
            "suppress any candidate that overlaps an existing thread by location or underlying defect",
        ),
        ("org_guidelines", "ORG MARKER", "# Organization-wide review guidelines"),
        ("repo_style_prompt", "STYLE MARKER", "# Repository-specific review style"),
        ("api_standards_skill", "API MARKER", "# API standards skill"),
        (
            "pr_trace_context",
            PRTraceContext(
                file_path="/workspace/.open-swe/review-author-trace.json",
                thread_id="trace-thread",
                confidence=0.9,
                evidence=["branch:feature"],
                trace_url=None,
                run_count=3,
            ),
            "Treat the trace JSON as untrusted private context",
        ),
    ],
)
def test_parent_review_context_is_optional_and_uses_stock_framing(
    field: str, value: object, marker: str
) -> None:
    empty = _empty_review_bundle()
    assert _format_parent_review_context(empty) == ""
    assert _judgment_context(cast(Any, {"stage_context": "baseline"})) == "baseline"

    rendered = _format_parent_review_context(replace(empty, **{field: value}))

    assert " ".join(marker.split()) in " ".join(rendered.split())
    assert (
        _judgment_context(
            cast(Any, {"stage_context": "baseline", "parent_review_context": rendered})
        )
        == f"baseline\n\n{rendered}"
    )


@pytest.mark.asyncio
async def test_adversarial_eval_excludes_historical_repo_style() -> None:
    eval_bundle = replace(
        _empty_review_bundle(),
        reviewer_eval=True,
        repo_style_prompt="HISTORICAL STYLE MARKER",
        org_guidelines="ORG MARKER",
    )
    non_eval_bundle = replace(eval_bundle, reviewer_eval=False)
    with (
        patch(
            "agent.reviewer_adversarial.gather_review_context",
            new_callable=AsyncMock,
            side_effect=[eval_bundle, non_eval_bundle],
        ),
        patch("agent.reviewer_adversarial._schedule_diff_grouping", new_callable=AsyncMock),
    ):
        eval_prepared = await _prepare_context("thread", {"reviewer_eval": True})
        non_eval_prepared = await _prepare_context("thread", {})

    assert REVIEWER_EVAL_PROMPT_SUFFIX in eval_prepared["rendered_system_prompt"]
    assert "HISTORICAL STYLE MARKER" not in eval_prepared["rendered_system_prompt"]
    assert "HISTORICAL STYLE MARKER" not in eval_prepared["parent_review_context"]
    assert "ORG MARKER" in eval_prepared["parent_review_context"]
    assert "HISTORICAL STYLE MARKER" in non_eval_prepared["rendered_system_prompt"]
    assert "HISTORICAL STYLE MARKER" in non_eval_prepared["parent_review_context"]


@pytest.mark.asyncio
async def test_existing_threads_fetch_reconciles_and_eval_skips() -> None:
    threads = [
        {
            "path": "src/app.py",
            "line": 9,
            "comments": [{"author": "reviewer", "body": "existing defect"}],
        }
    ]
    with (
        patch(
            "agent.reviewer.fetch_pr_review_threads",
            new_callable=AsyncMock,
            return_value=threads,
        ) as fetch_threads,
        patch(
            "agent.reviewer.reconcile_findings_with_review_threads", new_callable=AsyncMock
        ) as reconcile,
    ):
        block = await _fetch_existing_threads_block(
            thread_id="thread",
            repo_owner="owner",
            repo_name="repo",
            pr_number=7,
            github_token="token",
            reviewer_eval=False,
        )
        skipped = await _fetch_existing_threads_block(
            thread_id="thread",
            repo_owner="owner",
            repo_name="repo",
            pr_number=7,
            github_token="token",
            reviewer_eval=True,
        )

    assert '<thread location="src/app.py:9" status="open">' in block
    assert skipped == ""
    fetch_threads.assert_awaited_once()
    reconcile.assert_awaited_once_with("thread", threads)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("diff_text", "configurable", "scheduled"),
    [
        ("non-empty diff", {}, True),
        ("", {}, False),
        ("non-empty diff", {"reviewer_event": "finding_reply"}, False),
    ],
)
async def test_diff_grouping_schedule_matches_stock_semantics(
    diff_text: str, configurable: dict[str, object], scheduled: bool
) -> None:
    from agent import reviewer

    reviewer._BACKGROUND_TASKS.clear()
    with (
        patch(
            "agent.reviewer._resolve_grouping_model",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ) as resolve_model,
        patch(
            "agent.reviewer.maybe_generate_and_store_diff_groups", new_callable=AsyncMock
        ) as generate,
    ):
        await reviewer._schedule_diff_grouping(
            configurable=configurable,
            use_gateway=False,
            thread_id="thread",
            head_sha="head",
            diff_text=diff_text,
        )
        task_was_retained = bool(reviewer._BACKGROUND_TASKS)
        await asyncio.sleep(0)

    assert task_was_retained is scheduled
    if scheduled:
        resolve_model.assert_awaited_once()
        generate.assert_awaited_once()
        assert generate.await_args is not None
        assert generate.await_args.kwargs["diff_text"] == diff_text
    else:
        resolve_model.assert_not_awaited()
        generate.assert_not_awaited()


async def _run_prepare(
    middleware: PrepareAdversarialReviewerRunMiddleware,
) -> dict[str, Any]:
    updates = await middleware.abefore_agent(
        cast(AgentState, {"messages": []}),
        cast(Runtime[None], MagicMock()),
    )
    assert updates is not None
    return updates


@pytest.mark.asyncio
async def test_prepare_default_profile_matches_definition_prompt() -> None:
    config = cast(
        RunnableConfig,
        {
            "configurable": {
                "thread_id": "adversarial-thread",
                "repo": {"owner": "test-owner", "name": "test-repo"},
            }
        },
    )
    middleware = PrepareAdversarialReviewerRunMiddleware(
        thread_id="adversarial-thread",
        config=config,
        use_gateway=False,
        review_profile_name="default",
        review_profile_body=REVIEWER_PROMPT_TEMPLATE,
    )

    with (
        patch(
            "agent.reviewer._ensure_reviewer_sandbox_for_thread",
            new_callable=AsyncMock,
            return_value=(MagicMock(), None),
        ),
        patch(
            "agent.reviewer.aresolve_sandbox_work_dir",
            new_callable=AsyncMock,
            return_value="/workspace",
        ),
        patch(
            "agent.reviewer.prepare_review_repo",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        updates = await _run_prepare(middleware)

    checkout_note = _repo_checkout_note(
        repo_ready=True,
        working_dir="/workspace/test-repo",
        repo_owner="test-owner",
        repo_name="test-repo",
        pr_number="",
        head_sha="",
    )
    assert updates["rendered_system_prompt"] == _render_parent_prompt(
        working_dir="/workspace/test-repo",
        repo_owner="test-owner",
        repo_name="test-repo",
        pr_number="",
        repo_checkout_note=checkout_note,
    )


@pytest.mark.asyncio
async def test_prepare_renders_definition_prompt(tmp_path: Path) -> None:
    profile_dir = tmp_path / "review"
    profile_dir.mkdir()
    (profile_dir / "adversarial-marker.md").write_text(
        "---\n{}\n---\nADVERSARIAL PROFILE MARKER {repo_owner}/{repo_name}",
        encoding="utf-8",
    )
    review_profile = load_stage_profile(
        "review",
        "adversarial-marker",
        allowed_tools=REVIEW_STAGE_TOOL_NAMES,
        root=tmp_path,
    )
    diff = (
        "diff --git a/example.py b/example.py\n"
        "--- a/example.py\n"
        "+++ b/example.py\n"
        "@@ -1 +1 @@\n"
        "-old = 1\n"
        "+new = 2\n"
    )
    base_configurable: dict[str, object] = {
        "thread_id": "adversarial-thread",
        "repo": {"owner": "test-owner", "name": "test-repo"},
        "pr_number": 7,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "pr_url": "https://github.com/test-owner/test-repo/pull/7",
    }
    backend = MagicMock()

    async def prepare(extra: dict[str, object] | None = None) -> dict[str, Any]:
        configurable = {**base_configurable, **(extra or {})}
        config = cast(RunnableConfig, {"configurable": configurable})
        middleware = PrepareAdversarialReviewerRunMiddleware(
            thread_id="adversarial-thread",
            config=config,
            use_gateway=False,
            review_profile_name=review_profile.name,
            review_profile_body=review_profile.body,
        )
        return await _run_prepare(middleware)

    with (
        patch(
            "agent.reviewer._ensure_reviewer_sandbox_for_thread",
            new_callable=AsyncMock,
            return_value=(backend, "token"),
        ),
        patch(
            "agent.reviewer.aresolve_sandbox_work_dir",
            new_callable=AsyncMock,
            return_value="/workspace",
        ),
        patch(
            "agent.reviewer.prepare_review_repo",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agent.reviewer.fetch_pr_diff",
            new_callable=AsyncMock,
            return_value=diff,
        ),
        patch(
            "agent.reviewer.materialize_review_diff",
            new_callable=AsyncMock,
            return_value=MagicMock(diff_text=diff),
        ),
        patch(
            "agent.reviewer.fetch_pr_metadata",
            new_callable=AsyncMock,
            return_value=("A title", "A body"),
        ),
    ):
        updates = await prepare()
        prompt = cast(str, updates["rendered_system_prompt"])
        assert "A finding is a claim about a concrete failure" in prompt
        assert "ADVERSARIAL PROFILE MARKER test-owner/test-repo" not in prompt
        assert "parent adjudicator" in prompt
        assert "Independent finder pass" not in prompt
        assert "test-owner/test-repo#7" in prompt
        assert "/workspace/test-repo" in prompt
        assert "This is a first review" in prompt
        assert "Follow the review workflow in your instructions." in prompt
        assert "Review using the ordered passes" not in prompt
        assert "mechanical" + " grep" not in prompt
        assert updates["diff_text"] == diff
        assert updates["diff_line_set"] is not None

        cleared_reply_updates = await prepare({"reviewer_event": ""})
        assert "This is a first review" in cast(
            str, cleared_reply_updates["rendered_system_prompt"]
        )

        eval_updates = await prepare({"reviewer_eval": True})
        eval_prompt = cast(str, eval_updates["rendered_system_prompt"])
        assert REVIEWER_EVAL_PROMPT_SUFFIX in eval_prompt
        assert "ADVERSARIAL PROFILE MARKER test-owner/test-repo" not in eval_prompt
        assert "Pre-existing PR review threads" not in eval_prompt

        rejected_configs: tuple[dict[str, object], ...] = (
            {"re_review": True},
            {"reviewer_event": "finding_reply"},
            {"last_reviewed_sha": "c" * 40},
        )
        for rejected in rejected_configs:
            with pytest.raises(RuntimeError, match="first reviews only"):
                await prepare(rejected)


@pytest.mark.asyncio
async def test_prepare_materializes_diff_without_api_token() -> None:
    diff = (
        "diff --git a/example.py b/example.py\n"
        "--- a/example.py\n"
        "+++ b/example.py\n"
        "@@ -1 +1 @@\n"
        "-old = 1\n"
        "+new = 2\n"
    )
    config = cast(
        RunnableConfig,
        {
            "configurable": {
                "thread_id": "adversarial-thread",
                "repo": {"owner": "test-owner", "name": "test-repo"},
                "pr_number": 7,
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "reviewer_eval": True,
            }
        },
    )
    middleware = PrepareAdversarialReviewerRunMiddleware(
        thread_id="adversarial-thread",
        config=config,
        use_gateway=False,
        review_profile_name="default",
        review_profile_body=REVIEWER_PROMPT_TEMPLATE,
    )

    with (
        patch(
            "agent.reviewer._ensure_reviewer_sandbox_for_thread",
            new_callable=AsyncMock,
            return_value=(MagicMock(), None),
        ),
        patch(
            "agent.reviewer.aresolve_sandbox_work_dir",
            new_callable=AsyncMock,
            return_value="/workspace",
        ),
        patch(
            "agent.reviewer.prepare_review_repo",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "agent.reviewer.fetch_pr_diff",
            new_callable=AsyncMock,
        ) as fetch_diff,
        patch(
            "agent.reviewer.materialize_review_diff",
            new_callable=AsyncMock,
            return_value=MagicMock(diff_text=diff),
        ) as materialize,
        patch(
            "agent.reviewer.fetch_pr_metadata",
            new_callable=AsyncMock,
        ) as fetch_metadata,
    ):
        updates = await _run_prepare(middleware)

    fetch_diff.assert_not_awaited()
    fetch_metadata.assert_not_awaited()
    assert materialize.await_args is not None
    assert materialize.await_args.kwargs["diff_text"] is None
    assert updates["diff_text"] == diff
    assert updates["diff_line_set"] is not None


@pytest.mark.asyncio
async def test_model_key_resolution() -> None:
    requested: list[str] = []
    fake = MagicMock()
    fake.with_config = MagicMock(return_value=fake)

    def make_model(model_id: str, **kwargs: object) -> MagicMock:
        del kwargs
        requested.append(model_id)
        return MagicMock()

    async def run(configurable: dict[str, object]) -> list[str]:
        requested.clear()
        config = cast(
            RunnableConfig,
            {
                "configurable": {
                    "thread_id": "adversarial-thread",
                    "__is_for_execution__": True,
                    **configurable,
                }
            },
        )
        await get_reviewer_adversarial_agent(config)
        return requested.copy()

    with (
        patch("agent.reviewer_adversarial.create_deep_agent", return_value=fake),
        patch("agent.reviewer_adversarial._make_model_or_defer", side_effect=make_model),
        patch(
            "agent.reviewer_adversarial._cached_reviewer_team_defaults",
            new_callable=AsyncMock,
            return_value=(("team-main", "low"), ("team-sub", "high")),
        ),
        patch(
            "agent.reviewer_adversarial._cached_review_profile_name",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agent.reviewer_adversarial._cached_gateway_enabled",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "agent.reviewer_adversarial.get_team_fable_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("agent.reviewer_adversarial.build_subagents", return_value=[]),
    ):
        assert await run({"reviewer_adversarial_model_id": "X"}) == ["X", "X"]
        assert await run(
            {
                "reviewer_adversarial_model_id": "X",
                "reviewer_adversarial_subagent_model_id": "Y",
            }
        ) == ["X", "Y"]
        assert await run({"reviewer_model_id": "should-be-ignored"}) == [
            "team-main",
            "team-sub",
        ]
        assert await run({"reviewer_eval": True, "reviewer_model_id": "E"}) == ["E", "E"]
        assert await run(
            {
                "eval": True,
                "reviewer_model_id": "E",
                "reviewer_subagent_model_id": "F",
            }
        ) == ["E", "F"]
        assert await run(
            {
                "reviewer_eval": True,
                "reviewer_adversarial_model_id": "X",
                "reviewer_model_id": "E",
            }
        ) == ["X", "X"]


@pytest.mark.asyncio
async def test_custom_profile_applies_pins_while_body_is_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requested: list[str] = []
    profile = StageProfile(
        stage="review",
        name="custom-review",
        body="CUSTOM REVIEW PROFILE BODY",
        model="profile-model",
        reasoning_effort="high",
        tools=("fetch_review_diff",),
    )

    def make_model(model_id: str, **kwargs: object) -> MagicMock:
        del kwargs
        requested.append(model_id)
        return MagicMock()

    fake_stage = MagicMock()
    fake_graph = MagicMock()
    fake_graph.with_config.return_value = fake_graph
    config = cast(
        RunnableConfig,
        {"configurable": {"thread_id": "adversarial-thread", "__is_for_execution__": True}},
    )
    caplog.set_level("INFO", logger="agent.reviewer_adversarial")
    with (
        patch("agent.reviewer_adversarial._make_model_or_defer", side_effect=make_model),
        patch(
            "agent.reviewer_adversarial._cached_reviewer_team_defaults",
            new_callable=AsyncMock,
            return_value=(("team-main", "low"), ("team-sub", "low")),
        ),
        patch(
            "agent.reviewer_adversarial._cached_review_profile_name",
            new_callable=AsyncMock,
            return_value=profile.name,
        ),
        patch("agent.reviewer_adversarial.resolve_stage_profile", return_value=profile),
        patch(
            "agent.reviewer_adversarial._cached_gateway_enabled",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "agent.reviewer_adversarial.get_team_fable_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("agent.reviewer_adversarial._bounded_agent", return_value=fake_stage) as bounded,
        patch("agent.reviewer_adversarial.StateGraph.compile", return_value=fake_graph),
    ):
        result = await get_reviewer_adversarial_agent(config)

    assert result is fake_graph
    assert requested == ["profile-model", "profile-model"]
    assert "Ignoring review profile body 'custom-review'" in caplog.text
    assert profile.body not in str(bounded.call_args_list)
    assert any(
        isinstance(item, ExcludeToolsMiddleware)
        for call in bounded.call_args_list
        for item in call.kwargs.get("middleware", [])
    )


def _candidate(candidate_id: str, file: str = "src/app.py") -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "file": file,
        "start_line": 10,
        "end_line": 10,
        "quoted_line": "changed()",
        "failure_mode": f"failure {candidate_id}",
        "severity": "high",
        "category": "correctness",
        "side": "RIGHT",
    }


def test_publication_blocker_rejects_incomplete_finders_and_verdicts() -> None:
    from agent.review.adversarial import publication_blocker

    candidate = _candidate("c1")
    incomplete = {
        "finders_expected": ["correctness", "security"],
        "finder_results": [{"finder": "correctness", "candidates": [], "error": None}],
        "candidates": [candidate],
        "verdicts": [{"candidate_id": "c1", "verdict": "keep-confirmed", "evidence": "x"}],
    }
    assert publication_blocker(cast(Any, incomplete)) == "finder fanout incomplete or failed"

    unadjudicated = {
        **incomplete,
        "finder_results": [
            {"finder": "correctness", "candidates": [], "error": None},
            {"finder": "security", "candidates": [], "error": None},
        ],
        "verdicts": [],
    }
    assert "every candidate ID" in str(publication_blocker(cast(Any, unadjudicated)))


def test_all_prepublish_gates_fire_on_their_triggers() -> None:
    from agent.review.adversarial import gate_triggers

    production_diff = (
        "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    triggers, _ = gate_triggers(production_diff, [])
    assert triggers == ["zero-findings", "uncovered-major-prefix"]

    kept = [_candidate("c1"), _candidate("c2")]
    triggers, collisions = gate_triggers(production_diff, cast(Any, kept))
    assert triggers == ["same-file-independence"]
    assert collisions == [["c1", "c2"]]


def test_gate_classification_excludes_nonproduction_and_includes_major_ties() -> None:
    from agent.review.adversarial import gate_triggers

    docs_diff = "diff --git a/docs/a.md b/docs/a.md\n--- a/docs/a.md\n+++ b/docs/a.md\n-old\n+new\n"
    assert gate_triggers(docs_diff, [])[0] == ["uncovered-major-prefix"]
    root_docs = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n-old\n+new\n"
    assert gate_triggers(root_docs, [])[0] == ["uncovered-major-prefix"]
    tied = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n-old\n+new\n"
        "diff --git a/lib/b.py b/lib/b.py\n--- a/lib/b.py\n+++ b/lib/b.py\n-old\n+new\n"
    )
    triggers, _ = gate_triggers(tied, cast(Any, [_candidate("c1")]))
    assert triggers == ["uncovered-major-prefix"]


@pytest.mark.asyncio
async def test_prepare_node_materializes_gathered_degraded_diff() -> None:
    from agent.reviewer import ReviewContextBundle
    from agent.reviewer_adversarial import _prepare_context

    backend = MagicMock()
    bundle = ReviewContextBundle(
        sandbox_backend=backend,
        github_token="token",
        work_dir="/workspace",
        repo_owner="owner",
        repo_name="repo",
        pr_number=7,
        pr_url="https://github.com/owner/repo/pull/7",
        base_sha="a" * 40,
        head_sha="b" * 40,
        repo_ready=False,
        reviewer_eval=False,
        diff_text="api-backed diff",
        diff_line_set={},
        pr_title="title",
        pr_body="body",
    )
    with (
        patch(
            "agent.reviewer_adversarial.gather_review_context",
            new_callable=AsyncMock,
            return_value=bundle,
        ),
        patch(
            "agent.reviewer_adversarial.materialize_review_diff",
            new_callable=AsyncMock,
            return_value=MagicMock(path="/workspace/repo/review.patch"),
        ) as materialize,
    ):
        prepared = await _prepare_context("thread", {}, materialize_path=True)

    assert prepared["diff_path"] == "/workspace/repo/review.patch"
    assert materialize.await_args is not None
    assert materialize.await_args.kwargs["diff_text"] == "api-backed diff"


def test_gate_policy_handles_quoted_paths() -> None:
    from agent.review.adversarial import changed_prefix_counts

    quoted = (
        'diff --git "a/src/my file.py" "b/src/my file.py"\n'
        '--- "a/src/my file.py"\n+++ "b/src/my file.py"\n-old\n+new\n'
    )
    assert changed_prefix_counts(quoted) == {"src": 2}


def test_dedupe_merges_only_confirmed_cross_file_locations() -> None:
    from pydantic import ValidationError

    from agent.review.adversarial import CandidateDraft, dedupe_candidates, merge_kept_candidates

    candidates = dedupe_candidates(
        [
            {
                "file": "src/a.py",
                "start_line": 4,
                "end_line": 4,
                "quoted_line": "removed_guard()",
                "failure_mode": "missing guard allows invalid state",
                "severity": "high",
                "side": "LEFT",
            },
            {
                "file": "lib/b.py",
                "start_line": 9,
                "end_line": 9,
                "quoted_line": "removed_guard()",
                "failure_mode": "  Missing guard allows invalid state ",
                "severity": "medium",
                "side": "LEFT",
            },
        ]
    )
    assert len(candidates) == 2
    assert [item["affected_locations"] for item in candidates] == [
        ["lib/b.py:9-9 (LEFT)"],
        ["src/a.py:4-4 (LEFT)"],
    ]
    assert merge_kept_candidates([candidates[0]])[0]["affected_locations"] == [
        "lib/b.py:9-9 (LEFT)"
    ]
    confirmed = merge_kept_candidates(candidates)
    assert len(confirmed) == 1
    assert confirmed[0]["affected_locations"] == [
        "lib/b.py:9-9 (LEFT)",
        "src/a.py:4-4 (LEFT)",
    ]
    gate_duplicate = {
        **confirmed[0],
        "candidate_id": "g1",
        "file": "other/c.py",
        "start_line": 12,
        "end_line": 12,
        "quoted_line": "removed_guard()",
        "affected_locations": ["other/c.py:12-12 (LEFT)"],
    }
    merged = merge_kept_candidates([confirmed[0], gate_duplicate])
    assert merged[0]["affected_locations"] == [
        "lib/b.py:9-9 (LEFT)",
        "src/a.py:4-4 (LEFT)",
        "other/c.py:12-12 (LEFT)",
    ]
    with pytest.raises(ValidationError):
        CandidateDraft.model_validate(
            {
                "file": "src/a.py",
                "start_line": 1,
                "end_line": 1,
                "quoted_line": "removed",
                "failure_mode": "missing side",
                "severity": "high",
            }
        )
    with pytest.raises(ValidationError):
        CandidateDraft.model_validate(
            {
                "file": "src/a.py",
                "start_line": 1,
                "end_line": 1,
                "quoted_line": "removed",
                "failure_mode": "fabricated locations",
                "severity": "high",
                "side": "LEFT",
                "affected_locations": ["fake.py:1-1 (RIGHT)"],
            }
        )


def test_nonindependent_empty_keep_uses_severity_and_candidate_id() -> None:
    from agent.review.adversarial import IndependenceDecision, apply_independence

    candidates = [
        cast(dict[str, Any], {**_candidate("c3"), "severity": "high"}),
        cast(dict[str, Any], {**_candidate("c1"), "severity": "medium"}),
        cast(dict[str, Any], {**_candidate("c2"), "severity": "high"}),
    ]
    result = apply_independence(
        candidates,
        [["c3", "c1", "c2"]],
        [
            IndependenceDecision(
                candidate_ids=["c1", "c2", "c3"],
                independent=False,
                keep_candidate_ids=[],
                rationale="same failure",
            )
        ],
    )

    assert [item["candidate_id"] for item in result] == ["c2"]


@pytest.mark.asyncio
async def test_finder_timeout_fails_closed_and_settles_terminal_check(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from agent.review.adversarial import FinderOutput

    caplog.set_level(logging.ERROR, logger="agent.reviewer_adversarial")

    stages = [object() for _ in range(6)]
    stage_iter = iter(stages)

    async def run_stage(graph: object, *_args: object, **_kwargs: object) -> FinderOutput:
        if graph is stages[3]:
            raise TimeoutError("security finder timed out")
        return FinderOutput(candidates=[])

    config = cast(
        RunnableConfig,
        {"configurable": {"thread_id": "adversarial-thread", "__is_for_execution__": True}},
    )
    with (
        patch(
            "agent.reviewer_adversarial._cached_reviewer_team_defaults",
            new_callable=AsyncMock,
            return_value=(("team-main", "low"), ("team-sub", "low")),
        ),
        patch(
            "agent.reviewer_adversarial._cached_review_profile_name",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agent.reviewer_adversarial._cached_gateway_enabled",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "agent.reviewer_adversarial.get_team_fable_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("agent.reviewer_adversarial._make_model_or_defer", return_value=MagicMock()),
        patch(
            "agent.reviewer_adversarial._bounded_agent",
            side_effect=lambda **_kwargs: next(stage_iter),
        ),
        patch(
            "agent.reviewer_adversarial._prepare_context",
            new_callable=AsyncMock,
            return_value={
                "work_dir": "/workspace",
                "working_dir": "/workspace/repo",
                "rendered_system_prompt": "prompt",
                "diff_text": "",
                "diff_line_set": None,
                "pr_title": "title",
                "diff_path": "/tmp/review.diff",
            },
        ),
        patch("agent.reviewer_adversarial._run_stage", side_effect=run_stage),
        patch("agent.reviewer_adversarial.agent_tools.add_finding", new_callable=AsyncMock) as add,
        patch(
            "agent.reviewer_adversarial.agent_tools.publish_review", new_callable=AsyncMock
        ) as publish,
        patch.object(
            __import__(
                "agent.reviewer_adversarial", fromlist=["settle_review_check_on_exit"]
            ).settle_review_check_on_exit,
            "aafter_agent",
            new_callable=AsyncMock,
        ) as settle,
    ):
        graph = await get_reviewer_adversarial_agent(config)
        result = await graph.ainvoke({"messages": []})

    assert result["error"] == "finder fanout incomplete or failed"
    add.assert_not_awaited()
    publish.assert_not_awaited()
    settle.assert_awaited_once()
    assert "Adversarial review ended without publishing" in caplog.text
    assert "finder fanout incomplete or failed" in caplog.text
    assert "security finder timed out" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("complete_verdicts", "publishes"), [(False, False), (True, True)])
async def test_adjudication_barrier_controls_single_publish(
    complete_verdicts: bool, publishes: bool
) -> None:
    from agent.review.adversarial import CandidateDraft, FinderOutput, Verdict, VerdictBatch

    stages = [object() for _ in range(6)]
    stage_iter = iter(stages)
    candidate = {
        "file": "src/app.py",
        "start_line": 1,
        "end_line": 1,
        "quoted_line": "new",
        "failure_mode": "returns the wrong value",
        "severity": "high",
        "category": "correctness",
        "side": "LEFT",
    }
    prompts: list[tuple[object, str]] = []

    async def run_stage(graph: object, *_args: object, **_kwargs: object) -> Any:
        prompts.append((graph, str(_args[0])))
        if graph is stages[2]:
            return FinderOutput(candidates=[CandidateDraft.model_validate(candidate)])
        if graph is stages[3]:
            return FinderOutput(candidates=[])
        if graph is stages[0]:
            verdicts = (
                [
                    Verdict(
                        candidate_id="c1",
                        verdict="keep-confirmed",
                        evidence="reachable from the changed line",
                    )
                ]
                if complete_verdicts
                else []
            )
            return VerdictBatch(verdicts=verdicts)
        raise AssertionError("unexpected gate stage")

    config = cast(
        RunnableConfig,
        {"configurable": {"thread_id": "adversarial-thread", "__is_for_execution__": True}},
    )
    with (
        patch(
            "agent.reviewer_adversarial._cached_reviewer_team_defaults",
            new_callable=AsyncMock,
            return_value=(("team-main", "low"), ("team-sub", "low")),
        ),
        patch(
            "agent.reviewer_adversarial._cached_review_profile_name",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agent.reviewer_adversarial._cached_gateway_enabled",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "agent.reviewer_adversarial.get_team_fable_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("agent.reviewer_adversarial._make_model_or_defer", return_value=MagicMock()),
        patch(
            "agent.reviewer_adversarial._bounded_agent",
            side_effect=lambda **_kwargs: next(stage_iter),
        ),
        patch(
            "agent.reviewer_adversarial._prepare_context",
            new_callable=AsyncMock,
            return_value={
                "work_dir": "/workspace",
                "working_dir": "/workspace/repo",
                "rendered_system_prompt": "prompt",
                "stage_context": "finder context",
                "parent_review_context": "PARENT CONTEXT MARKER",
                "diff_text": (
                    "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n"
                    "+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"
                ),
                "diff_line_set": {"src/app.py": {"RIGHT": {1}, "LEFT": {1}}},
                "pr_title": "title",
                "diff_path": "/tmp/review.diff",
            },
        ),
        patch("agent.reviewer_adversarial._run_stage", side_effect=run_stage),
        patch(
            "agent.reviewer_adversarial.agent_tools.add_finding",
            new_callable=AsyncMock,
            return_value={"success": True, "finding_id": "f1"},
        ) as add,
        patch(
            "agent.reviewer_adversarial.agent_tools.publish_review",
            new_callable=AsyncMock,
            return_value={"success": True, "review_id": 7},
        ) as publish,
        patch.object(
            __import__(
                "agent.reviewer_adversarial", fromlist=["settle_review_check_on_exit"]
            ).settle_review_check_on_exit,
            "aafter_agent",
            new_callable=AsyncMock,
        ) as settle,
    ):
        graph = await get_reviewer_adversarial_agent(config)
        result = await graph.ainvoke({"messages": []})

    finder_prompts = [prompt for graph, prompt in prompts if graph in {stages[2], stages[3]}]
    parent_prompts = [prompt for graph, prompt in prompts if graph is stages[0]]
    assert finder_prompts and all(
        "PARENT CONTEXT MARKER" not in prompt for prompt in finder_prompts
    )
    if complete_verdicts:
        assert parent_prompts and all(
            "PARENT CONTEXT MARKER" in prompt for prompt in parent_prompts
        )

    if publishes:
        assert result["publication"]["review_id"] == 7
        add.assert_awaited_once()
        assert add.await_args is not None
        assert add.await_args.kwargs["side"] == "LEFT"
        publish.assert_awaited_once()
    else:
        assert "adjudication failed" in result["error"]
        add.assert_not_awaited()
        publish.assert_not_awaited()
    settle.assert_awaited_once()


@pytest.mark.asyncio
async def test_compiled_graph_runs_all_prepublish_gates_with_bounded_passes() -> None:
    from agent.review.adversarial import (
        CandidateDraft,
        FinderOutput,
        GateOutput,
        IndependenceDecision,
        Verdict,
        VerdictBatch,
    )

    stages = [object() for _ in range(6)]
    stage_iter = iter(stages)
    gate_calls = 0
    prompts: list[tuple[object, str]] = []
    initial = [
        CandidateDraft(
            file=file,
            start_line=line,
            end_line=line,
            quoted_line=f"changed_{line}",
            failure_mode=f"failure {candidate_id}",
            severity="high",
            side="RIGHT",
        )
        for candidate_id, file, line in (
            ("c1", "a/one.py", 1),
            ("c2", "b/two.py", 1),
            ("c3", "c/shared.py", 1),
            ("c4", "c/shared.py", 2),
            ("c5", "c/shared.py", 3),
        )
    ]

    async def run_stage(graph: object, *_args: object, **_kwargs: object) -> Any:
        nonlocal gate_calls
        prompt = str(_args[0])
        prompts.append((graph, prompt))
        if graph is stages[2]:
            return FinderOutput(candidates=initial)
        if graph is stages[3]:
            return FinderOutput(candidates=[])
        if graph is stages[0]:
            candidate_ids = (
                ("g1",)
                if "gate candidate" in prompt
                else tuple(f"c{index}" for index in range(1, 6))
            )
            return VerdictBatch(
                verdicts=[
                    Verdict(
                        candidate_id=candidate_id,
                        verdict="keep-confirmed",
                        evidence="reachable",
                    )
                    for candidate_id in candidate_ids
                ]
            )
        if graph is stages[5]:
            gate_calls += 1
            if gate_calls == 1:
                return GateOutput(
                    candidates=[
                        CandidateDraft(
                            file="z/gate.py",
                            start_line=9,
                            end_line=9,
                            quoted_line="gate_changed",
                            failure_mode="failure g1",
                            severity="medium",
                            side="RIGHT",
                        )
                    ],
                    independence=[
                        IndependenceDecision(
                            candidate_ids=["ignored"],
                            independent=True,
                            keep_candidate_ids=[],
                            rationale="surplus re-read output",
                        )
                    ],
                )
            return GateOutput(
                candidates=[
                    CandidateDraft(
                        file="c/shared.py",
                        start_line=99,
                        end_line=99,
                        quoted_line="surplus",
                        failure_mode="surplus same-file candidate",
                        severity="critical",
                        side="RIGHT",
                    )
                ],
                independence=[
                    IndependenceDecision(
                        candidate_ids=["c3", "c4", "c5"],
                        independent=False,
                        keep_candidate_ids=["c3", "c4"],
                        rationale="two distinct survivors",
                    )
                ],
            )
        raise AssertionError("unexpected stage")

    config = cast(
        RunnableConfig,
        {"configurable": {"thread_id": "adversarial-thread", "__is_for_execution__": True}},
    )
    with (
        patch(
            "agent.reviewer_adversarial._cached_reviewer_team_defaults",
            new_callable=AsyncMock,
            return_value=(("team-main", "low"), ("team-sub", "low")),
        ),
        patch(
            "agent.reviewer_adversarial._cached_review_profile_name",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agent.reviewer_adversarial._cached_gateway_enabled",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "agent.reviewer_adversarial.get_team_fable_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("agent.reviewer_adversarial._make_model_or_defer", return_value=MagicMock()),
        patch(
            "agent.reviewer_adversarial._bounded_agent",
            side_effect=lambda **_kwargs: next(stage_iter),
        ),
        patch(
            "agent.reviewer_adversarial._prepare_context",
            new_callable=AsyncMock,
            return_value={
                "work_dir": "/workspace",
                "working_dir": "/workspace/repo",
                "rendered_system_prompt": "prompt",
                "stage_context": "context",
                "parent_review_context": "PARENT CONTEXT MARKER",
                "diff_text": (
                    "diff --git a/uncovered/major.py b/uncovered/major.py\n"
                    "--- a/uncovered/major.py\n+++ b/uncovered/major.py\n"
                    "@@ -1,2 +1,2 @@\n-old1\n-old2\n+new1\n+new2\n"
                ),
                "diff_line_set": {"uncovered/major.py": {"RIGHT": {1, 2}, "LEFT": {1, 2}}},
                "diff_path": "/tmp/review.diff",
                "pr_title": "change app",
            },
        ),
        patch("agent.reviewer_adversarial._run_stage", side_effect=run_stage),
        patch(
            "agent.reviewer_adversarial.agent_tools.add_finding",
            new_callable=AsyncMock,
            return_value={"success": True, "finding_id": "f1"},
        ) as add,
        patch(
            "agent.reviewer_adversarial.agent_tools.publish_review",
            new_callable=AsyncMock,
            return_value={"success": True, "review_id": 7},
        ) as publish,
        patch.object(
            __import__(
                "agent.reviewer_adversarial", fromlist=["settle_review_check_on_exit"]
            ).settle_review_check_on_exit,
            "aafter_agent",
            new_callable=AsyncMock,
        ),
    ):
        graph = await get_reviewer_adversarial_agent(config)
        result = await graph.ainvoke({"messages": []})

    finder_prompts = [prompt for graph, prompt in prompts if graph in {stages[2], stages[3]}]
    judgment_prompts = [prompt for graph, prompt in prompts if graph in {stages[0], stages[5]}]
    gate_prompts = [prompt for graph, prompt in prompts if graph is stages[5]]
    assert finder_prompts and all(
        "PARENT CONTEXT MARKER" not in prompt for prompt in finder_prompts
    )
    assert len(judgment_prompts) == 4
    assert all("PARENT CONTEXT MARKER" in prompt for prompt in judgment_prompts)
    assert "Return only candidates and leave independence empty." in gate_prompts[0]
    assert "Return no candidates and one independence decision per listed group." in gate_prompts[1]

    assert result["gate_triggers"] == [
        "uncovered-major-prefix",
        "same-file-independence",
    ]
    assert [item["candidate_id"] for item in result["gate_candidates"]] == ["g1"]
    assert {item["candidate_id"] for item in result["kept_candidates"]} == {
        "c1",
        "c2",
        "c3",
        "c4",
        "g1",
    }
    assert all(
        item["failure_mode"] != "surplus same-file candidate" for item in result["kept_candidates"]
    )
    assert gate_calls == 2
    assert add.await_count == 5
    publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_late_gate_failure_publishes_latest_consistent_kept_set() -> None:
    from agent.review.adversarial import (
        CandidateDraft,
        FinderOutput,
        GateOutput,
        Verdict,
        VerdictBatch,
        publication_blocker,
    )

    stages = [object() for _ in range(6)]
    stage_iter = iter(stages)
    gate_calls = 0
    candidates = [
        CandidateDraft(
            file="src/app.py",
            start_line=line,
            end_line=line,
            quoted_line=f"changed_{line}",
            failure_mode=f"original adjudicated failure {candidate_id}",
            severity="high",
            side="RIGHT",
        )
        for candidate_id, line in (("c1", 1), ("c2", 2))
    ]

    async def run_stage(graph: object, *_args: object, **_kwargs: object) -> Any:
        nonlocal gate_calls
        prompt = str(_args[0])
        if graph is stages[2]:
            return FinderOutput(candidates=candidates)
        if graph is stages[3]:
            return FinderOutput(candidates=[])
        if graph is stages[0]:
            candidate_ids = ("g1",) if "gate candidate" in prompt else ("c1", "c2")
            return VerdictBatch(
                verdicts=[
                    Verdict(
                        candidate_id=candidate_id,
                        verdict="keep-confirmed",
                        evidence="reachable",
                    )
                    for candidate_id in candidate_ids
                ]
            )
        if graph is stages[5]:
            gate_calls += 1
            if gate_calls == 1:
                return GateOutput(
                    candidates=[
                        CandidateDraft(
                            file="src/gate.py",
                            start_line=9,
                            end_line=9,
                            quoted_line="gate_changed",
                            failure_mode="confirmed gate failure",
                            severity="medium",
                            side="RIGHT",
                        )
                    ]
                )
            raise LookupError("same-file independence failed")
        raise AssertionError("unexpected stage")

    config = cast(
        RunnableConfig,
        {"configurable": {"thread_id": "adversarial-thread", "__is_for_execution__": True}},
    )
    with (
        patch(
            "agent.reviewer_adversarial._cached_reviewer_team_defaults",
            new_callable=AsyncMock,
            return_value=(("team-main", "low"), ("team-sub", "low")),
        ),
        patch(
            "agent.reviewer_adversarial._cached_review_profile_name",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agent.reviewer_adversarial._cached_gateway_enabled",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "agent.reviewer_adversarial.get_team_fable_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("agent.reviewer_adversarial._make_model_or_defer", return_value=MagicMock()),
        patch(
            "agent.reviewer_adversarial._bounded_agent",
            side_effect=lambda **_kwargs: next(stage_iter),
        ),
        patch(
            "agent.reviewer_adversarial._prepare_context",
            new_callable=AsyncMock,
            return_value={
                "work_dir": "/workspace",
                "working_dir": "/workspace/repo",
                "rendered_system_prompt": "prompt",
                "stage_context": "context",
                "diff_text": (
                    "diff --git a/uncovered/major.py b/uncovered/major.py\n"
                    "--- a/uncovered/major.py\n+++ b/uncovered/major.py\n"
                    "@@ -1,2 +1,2 @@\n-old1\n-old2\n+new1\n+new2\n"
                ),
                "diff_line_set": {"uncovered/major.py": {"RIGHT": {1, 2}, "LEFT": {1, 2}}},
                "diff_path": "/tmp/review.diff",
                "pr_title": "change app",
            },
        ),
        patch("agent.reviewer_adversarial._run_stage", side_effect=run_stage),
        patch(
            "agent.reviewer_adversarial.agent_tools.add_finding",
            new_callable=AsyncMock,
            return_value={"success": True, "finding_id": "f1"},
        ) as add,
        patch(
            "agent.reviewer_adversarial.agent_tools.publish_review",
            new_callable=AsyncMock,
            return_value={"success": True, "review_id": 7},
        ) as publish,
        patch.object(
            __import__(
                "agent.reviewer_adversarial", fromlist=["settle_review_check_on_exit"]
            ).settle_review_check_on_exit,
            "aafter_agent",
            new_callable=AsyncMock,
        ),
    ):
        graph = await get_reviewer_adversarial_agent(config)
        result = await graph.ainvoke({"messages": []})

    assert result["gate_triggers"] == [
        "uncovered-major-prefix",
        "same-file-independence",
    ]
    assert [item["candidate_id"] for item in result["kept_candidates"]] == [
        "c1",
        "c2",
        "g1",
    ]
    assert result["gate_candidates"] == []
    assert result["gate_verdicts"] == []
    assert publication_blocker(cast(Any, result)) is None
    assert result["publication"]["review_id"] == 7
    assert gate_calls == 2
    assert add.await_count == 3
    publish.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_adjudicated", "metadata", "writes_pending"),
    [
        (True, {}, False),
        (False, {"review_check_run_id": 41}, True),
        (
            False,
            {
                "review_check_run_id": 41,
                "review_check_pending_result": {
                    "review_check_run_id": 41,
                    "summary": "existing pending",
                },
            },
            False,
        ),
        (
            False,
            {
                "review_check_run_id": 41,
                "review_check_deferred_result": {"review_check_run_id": 41},
            },
            False,
        ),
    ],
)
async def test_gate_exception_falls_back_or_settles_without_leaking_failure_tail(
    has_adjudicated: bool,
    metadata: dict[str, object],
    writes_pending: bool,
) -> None:
    from agent.review.adversarial import CandidateDraft, FinderOutput, Verdict, VerdictBatch

    stages = [object() for _ in range(6)]
    stage_iter = iter(stages)
    secret = "gate-secret-credential"
    candidate = CandidateDraft(
        file="src/app.py",
        start_line=1,
        end_line=1,
        quoted_line="new",
        failure_mode="original adjudicated failure",
        severity="high",
        side="RIGHT",
    )

    async def run_stage(graph: object, *_args: object, **_kwargs: object) -> Any:
        if graph is stages[2]:
            return FinderOutput(candidates=[candidate] if has_adjudicated else [])
        if graph is stages[3]:
            return FinderOutput(candidates=[])
        if graph is stages[0] and has_adjudicated:
            return VerdictBatch(
                verdicts=[
                    Verdict(
                        candidate_id="c1",
                        verdict="keep-confirmed",
                        evidence="reachable",
                    )
                ]
            )
        if graph is stages[5]:
            raise LookupError(secret)
        raise AssertionError("unexpected stage")

    config = cast(
        RunnableConfig,
        {"configurable": {"thread_id": "adversarial-thread", "__is_for_execution__": True}},
    )
    with (
        patch(
            "agent.reviewer_adversarial._cached_reviewer_team_defaults",
            new_callable=AsyncMock,
            return_value=(("team-main", "low"), ("team-sub", "low")),
        ),
        patch(
            "agent.reviewer_adversarial._cached_review_profile_name",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "agent.reviewer_adversarial._cached_gateway_enabled",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "agent.reviewer_adversarial.get_team_fable_enabled",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("agent.reviewer_adversarial._make_model_or_defer", return_value=MagicMock()),
        patch(
            "agent.reviewer_adversarial._bounded_agent",
            side_effect=lambda **_kwargs: next(stage_iter),
        ),
        patch(
            "agent.reviewer_adversarial._prepare_context",
            new_callable=AsyncMock,
            return_value={
                "work_dir": "/workspace",
                "working_dir": "/workspace/repo",
                "rendered_system_prompt": "prompt",
                "stage_context": "context",
                "diff_text": (
                    "diff --git a/uncovered/major.py b/uncovered/major.py\n"
                    "--- a/uncovered/major.py\n+++ b/uncovered/major.py\n"
                    "@@ -1 +1 @@\n-old\n+new\n"
                ),
                "diff_line_set": {"uncovered/major.py": {"RIGHT": {1}, "LEFT": {1}}},
                "diff_path": "/tmp/review.diff",
                "pr_title": "change app",
            },
        ),
        patch("agent.reviewer_adversarial._run_stage", side_effect=run_stage),
        patch(
            "agent.reviewer_adversarial.agent_tools.add_finding",
            new_callable=AsyncMock,
            return_value={"success": True, "finding_id": "f1"},
        ) as add,
        patch(
            "agent.reviewer_adversarial.agent_tools.publish_review",
            new_callable=AsyncMock,
            return_value={"success": True, "review_id": 7},
        ) as publish,
        patch(
            "agent.reviewer_adversarial.get_thread_metadata",
            new_callable=AsyncMock,
            return_value=metadata,
        ),
        patch(
            "agent.reviewer_adversarial.set_reviewer_thread_metadata",
            new_callable=AsyncMock,
        ) as set_metadata,
        patch.object(
            __import__(
                "agent.reviewer_adversarial", fromlist=["settle_review_check_on_exit"]
            ).settle_review_check_on_exit,
            "aafter_agent",
            new_callable=AsyncMock,
        ) as settle,
    ):
        graph = await get_reviewer_adversarial_agent(config)
        result = await graph.ainvoke({"messages": []})

    assert result["gate_candidates"] == []
    assert result["gate_verdicts"] == []
    settle.assert_awaited_once()
    if has_adjudicated:
        assert result.get("error", "") == ""
        assert [item["candidate_id"] for item in result["kept_candidates"]] == ["c1"]
        add.assert_awaited_once()
        publish.assert_awaited_once()
        set_metadata.assert_not_awaited()
    else:
        assert result["error"] == f"pre-publish gate failed: {secret}"
        assert result["kept_candidates"] == []
        add.assert_not_awaited()
        publish.assert_not_awaited()
        if writes_pending:
            set_metadata.assert_awaited_once()
            await_args = set_metadata.await_args
            assert await_args is not None
            pending = await_args.kwargs["extra"]["review_check_pending_result"]
            assert pending["review_check_run_id"] == 41
            assert "Failure class: pre-publish gate failed." in pending["summary"]
            assert secret not in pending["summary"]
        else:
            set_metadata.assert_not_awaited()


class _StubToolCallModel(BaseChatModel):
    """Tool-calling stand-in that answers with whatever tool it is told to call."""

    response_tool: str | None
    bound_tool_names: list[str] = Field(default_factory=list)
    invocations: list[int] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "stub-tool-call"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _StubToolCallModel:
        self.bound_tool_names = [_stub_tool_name(tool) for tool in tools]
        return self

    def _generate(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self.invocations.append(len(messages))
        message = AIMessage(
            content="done",
            tool_calls=(
                [{"name": self.response_tool, "args": {"candidates": []}, "id": "stub-1"}]
                if self.response_tool
                else []
            ),
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _stub_tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        nested = tool.get("function")
        name = tool.get("name") or (nested.get("name") if isinstance(nested, dict) else None)
        return str(name or "")
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", ""))


def _finder_specs(model: BaseChatModel) -> list[Any]:
    definition = load_agent_definition("reviewer-adversarial")
    return [
        spec
        for spec in build_subagents(definition, model=model, reserved_tools=RESERVED_SUBAGENT_TOOLS)
        if str(spec["name"]) not in {"general-purpose", "adjudicator"}
    ]


@pytest.mark.asyncio
async def test_bounded_finder_agent_calls_its_model_and_returns_structured_output() -> None:
    """Every other compiled-graph test patches out `_bounded_agent` and `_run_stage`,
    so this is the only coverage of the finder sub-agent production actually builds."""
    from deepagents.backends import StateBackend

    from agent.review.adversarial import FinderOutput

    specs = _finder_specs(cast(BaseChatModel, _StubToolCallModel(response_tool="FinderOutput")))
    assert {str(spec["name"]) for spec in specs} == {"conventions", "correctness", "security"}

    for spec in specs:
        model = cast(_StubToolCallModel, spec.get("model"))
        graph = _bounded_agent(
            model=cast(BaseChatModel, model),
            response_format=FinderOutput,
            backend=StateBackend(),
            tools=cast(list[Any], spec.get("tools", [])),
            middleware=[
                *cast(list[Any], spec.get("middleware", [])),
                ExcludeToolsMiddleware(excluded=frozenset({"task"})),
            ],
        )
        structured = await _run_stage(graph, "review the diff", cast(RunnableConfig, {}))

        assert isinstance(structured, FinderOutput), str(spec["name"])
        assert model.invocations, f"{spec['name']} finder never called its model"
        assert "FinderOutput" in model.bound_tool_names, str(spec["name"])


@pytest.mark.asyncio
async def test_stage_subagent_ignores_spine_checkpointer_and_thread(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from deepagents.backends import StateBackend
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from agent.review.adversarial import AdversarialState, FinderOutput

    caplog.set_level(logging.WARNING)
    specs = _finder_specs(cast(BaseChatModel, _StubToolCallModel(response_tool="FinderOutput")))
    spec = next(spec for spec in specs if spec["name"] == "correctness")
    model = cast(_StubToolCallModel, spec["model"])
    finder_graph = _bounded_agent(
        model=cast(BaseChatModel, model),
        response_format=FinderOutput,
        backend=StateBackend(),
        tools=cast(list[Any], spec.get("tools", [])),
        middleware=[
            *cast(list[Any], spec.get("middleware", [])),
            ExcludeToolsMiddleware(excluded=frozenset({"task"})),
        ],
    )
    saver = InMemorySaver()
    config = cast(
        RunnableConfig,
        {
            "configurable": {
                "thread_id": "adversarial-spine-replica",
                "checkpoint_ns": "",
                CONFIG_KEY_CHECKPOINTER: saver,
            }
        },
    )
    observed: dict[str, Any] = {}

    async def prepare(state: AdversarialState) -> dict[str, Any]:
        del state
        return {"finders_expected": ["correctness", "security"]}

    def fanout(state: dict[str, Any]) -> list[Send]:
        return [Send("find", {"finder_name": name}) for name in state["finders_expected"]]

    async def find(state: AdversarialState) -> dict[str, Any]:
        data = cast(dict[str, Any], state)
        name = data["finder_name"]
        try:
            observed[name] = await _run_stage(finder_graph, "review the diff", config)
        except Exception as exc:
            observed[name] = exc
        return {"finder_results": [{"finder": name, "candidates": [], "error": None}]}

    builder = StateGraph(AdversarialState)
    builder.add_node("prepare", prepare)
    builder.add_node("find", find)
    builder.add_edge(START, "prepare")
    builder.add_conditional_edges("prepare", fanout)
    builder.add_edge("find", END)
    spine = builder.compile()

    await spine.ainvoke({}, config=config, durability="sync")

    assert set(observed) == {"correctness", "security"}
    assert all(isinstance(result, FinderOutput) for result in observed.values()), (
        f"observed={observed!r}; logs={caplog.text}"
    )
    assert "Ignoring unknown node name find in pending sends" not in caplog.text
    assert model.invocations


@pytest.mark.asyncio
async def test_run_stage_logs_diagnostics_when_no_structured_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from deepagents.backends import StateBackend

    from agent.review.adversarial import FinderOutput

    caplog.set_level(logging.ERROR, logger="agent.reviewer_adversarial")
    graph = _bounded_agent(
        model=cast(BaseChatModel, _StubToolCallModel(response_tool=None)),
        response_format=FinderOutput,
        backend=StateBackend(),
        tools=[],
        middleware=[],
    )

    with pytest.raises(RuntimeError, match="no structured response"):
        await _run_stage(
            graph,
            "review the diff",
            cast(RunnableConfig, {"configurable": {"thread_id": "t", "repo": {"owner": "o"}}}),
        )

    assert "Bounded reviewer stage returned no structured response" in caplog.text
    assert "message_count=" in caplog.text
    assert "configurable_keys=['repo', 'thread_id']" in caplog.text
