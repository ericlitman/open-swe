from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from deepagents.middleware.skills import _parse_skill_metadata
from langchain_core.runnables import RunnableConfig

from agent.analyzer import PrepareAnalyzerRunMiddleware
from agent.dashboard.review_style_jobs import (
    build_continual_run_configurable,
    build_continual_run_input,
    start_bootstrap_analysis,
)
from agent.middleware import PrepareRunState
from agent.review.style_collector import ReviewStyleSamples
from agent.utils.analyzer_skills import (
    ANALYZER_MODES,
    SKILLS_DIR,
    build_skill_files,
    skill_path_for_mode,
)


def test_build_skill_files_stripped_keys_and_valid_file_data() -> None:
    files = build_skill_files()
    assert set(files) == {
        "/bootstrap-repo-analysis/SKILL.md",
        "/continual-learning/SKILL.md",
    }
    for entry in files.values():
        assert entry["encoding"] == "utf-8"
        assert isinstance(entry["content"], str) and entry["content"].strip()
        assert "created_at" in entry and "modified_at" in entry


def test_skill_path_for_mode() -> None:
    assert skill_path_for_mode("bootstrap") == "/skills/bootstrap-repo-analysis/SKILL.md"
    assert skill_path_for_mode("continual") == "/skills/continual-learning/SKILL.md"
    # Unknown modes fall back to bootstrap.
    assert skill_path_for_mode("whatever") == "/skills/bootstrap-repo-analysis/SKILL.md"


def test_bundled_skill_md_parse() -> None:
    for skill in ANALYZER_MODES.values():
        path = SKILLS_DIR / skill / "SKILL.md"
        meta = _parse_skill_metadata(path.read_text(), str(path), path.parent.name)
        assert meta is not None
        assert meta["name"] == skill
        assert meta["description"].strip()


def test_skills_dir_resolves() -> None:
    assert SKILLS_DIR.name == "skills"
    assert (SKILLS_DIR / "bootstrap-repo-analysis" / "SKILL.md").exists()
    assert isinstance(SKILLS_DIR, pathlib.Path)


def test_continual_run_payload_carries_mode_and_skill_files() -> None:
    configurable = build_continual_run_configurable("o/r")
    assert configurable["analyzer_mode"] == "continual"
    assert configurable["review_style_full_name"] == "o/r"
    assert configurable.get("thread_id")

    run_input = build_continual_run_input("o/r")
    assert "/continual-learning/SKILL.md" in run_input["files"]
    assert run_input["messages"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_analyzer_oauth_token_is_not_used_for_sandbox_provisioning() -> None:
    config: RunnableConfig = {
        "configurable": {
            "review_style_full_name": "public/repo",
            "review_style_github_token": "oauth-token",
        }
    }
    middleware = PrepareAnalyzerRunMiddleware(thread_id="thread-1", config=config)
    sandbox = MagicMock()

    state: PrepareRunState = {"messages": []}

    with (
        patch(
            "agent.analyzer.ensure_sandbox_for_thread", new=AsyncMock(return_value=sandbox)
        ) as ensure,
        patch("agent.analyzer.aresolve_sandbox_work_dir", new=AsyncMock(return_value="/workspace")),
    ):
        await middleware._prepare(state, MagicMock())

    ensure.assert_awaited_once_with(
        "thread-1",
        repo={"owner": "public", "name": "repo"},
    )


@pytest.mark.asyncio
async def test_bootstrap_oauth_token_stays_in_control_plane() -> None:
    samples = ReviewStyleSamples(
        full_name="public/repo",
        owner="public",
        name="repo",
    )
    collect = AsyncMock(return_value=samples)
    create_run = AsyncMock(return_value={"run_id": "run-1"})

    with (
        patch("agent.dashboard.review_style_jobs.collect_review_samples", collect),
        patch("agent.dashboard.review_style_jobs.mark_analysis_running", new=AsyncMock()),
        patch("agent.dashboard.review_style_jobs.create_durable_run", create_run),
        patch(
            "agent.dashboard.review_style_jobs.update_review_style",
            new=AsyncMock(return_value={"status": "running"}),
        ),
        patch("agent.dashboard.review_style_jobs._client", return_value=MagicMock()),
    ):
        await start_bootstrap_analysis(
            "public/repo",
            github_token="oauth-token",
            created_by="octocat",
        )

    collect.assert_awaited_once_with("oauth-token", "public", "repo")
    assert create_run.await_args is not None
    configurable = create_run.await_args.kwargs["config"]["configurable"]
    assert "review_style_github_token" not in configurable
