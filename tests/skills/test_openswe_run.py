from __future__ import annotations

import argparse
import asyncio
import importlib.util
import io
import json
import re
import subprocess
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
SKILL = ROOT / ".claude/skills/openswe-run"
WAVE_SKILL = ROOT / ".claude/skills/openswe-wave"
SCRIPT_PATH = SKILL / "scripts/openswe-run"
MODULE_PATH = SKILL / "scripts/openswe_run.py"
SPEC = importlib.util.spec_from_file_location("openswe_run", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
run = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = run
SPEC.loader.exec_module(run)


def test_entry_point_is_executable_and_self_describes() -> None:
    result = subprocess.run(
        [str(SCRIPT_PATH), "--help"], text=True, capture_output=True, check=False
    )

    assert SCRIPT_PATH.stat().st_mode & 0o111
    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


def test_handoffs_use_checkout_local_exclude(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True, text=True)
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.tmp\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", ".gitignore"],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv("OPENSWE_STABLE_ROOT", str(tmp_path))

    handoffs = run.ensure_handoffs()

    assert handoffs.is_dir()
    assert gitignore.read_text() == "*.tmp\n"
    ignored = subprocess.run(
        ["git", "-C", str(tmp_path), "check-ignore", "handoffs/probe"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode == 0


def test_handoffs_use_linked_worktree_local_exclude(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    (repo / "tracked").write_text("tracked\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "linked", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv("OPENSWE_STABLE_ROOT", str(worktree))

    assert run.ensure_handoffs().is_dir()
    ignored = subprocess.run(
        ["git", "-C", str(worktree), "check-ignore", "handoffs/probe"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode == 0
    assert not (worktree / ".gitignore").exists()


def test_handoff_creation_survives_unavailable_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unavailable_git(*args: Any, **kwargs: Any) -> None:
        raise OSError

    monkeypatch.setenv("OPENSWE_STABLE_ROOT", str(tmp_path))
    monkeypatch.setattr(run.subprocess, "run", unavailable_git)

    assert run.ensure_handoffs().is_dir()


def test_monitor_python_override_expands_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENSWE_RUN_PYTHON", "~/venv/bin/python")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        run, "_monitor_python_error", lambda command: commands.append(command) or None
    )

    assert run.resolve_monitor_python() == [str(tmp_path / "venv/bin/python")]
    assert commands == [[str(tmp_path / "venv/bin/python")]]


def test_monitor_python_override_failure_is_a_run_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_python(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("not found")

    monkeypatch.setenv("OPENSWE_RUN_PYTHON", "python -m uv")
    monkeypatch.setattr(run.subprocess, "run", missing_python)

    with pytest.raises(run.RunError, match="OPENSWE_RUN_PYTHON.*not found"):
        run.resolve_monitor_python()


def test_monitor_python_probe_requires_both_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def subprocess_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing sdk")

    monkeypatch.setattr(run.subprocess, "run", subprocess_run)

    assert run._monitor_python_error(["python"]) == "missing sdk"
    assert commands == [["python", "-c", "import httpx, langgraph_sdk"]]


def test_monitor_python_falls_through_unusable_control_plane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    control_plane = tmp_path / "python"
    control_plane.touch()
    monkeypatch.delenv("OPENSWE_RUN_PYTHON", raising=False)
    monkeypatch.setattr(run, "CONTROL_PLANE_PYTHON", str(control_plane))
    monkeypatch.setattr(run.shutil, "which", lambda command: "/usr/bin/uv")
    commands: list[list[str]] = []

    def probe(command: list[str]) -> str | None:
        commands.append(command)
        return "missing sdk" if command == [str(control_plane)] else None

    monkeypatch.setattr(run, "_monitor_python_error", probe)

    resolved = run.resolve_monitor_python()

    assert resolved[0] == "uv"
    assert commands == [[str(control_plane)], resolved]


@pytest.fixture
def cold_tunnel(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """A live SSH tunnel can miss the 3s cold first hit, then answer immediately."""
    attempts: list[int] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    def urlopen(url: str, *, timeout: int):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise TimeoutError("cold tunnel first hit")
        return Response()

    monkeypatch.setattr(run.urllib.request, "urlopen", urlopen)
    return attempts


def _env_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENSWE_STABLE_ROOT", str(tmp_path))
    monkeypatch.setattr(run, "resolve_monitor_python", lambda: ["python-with-sdk"])
    monkeypatch.setattr(run, "skill_checkout_warning", lambda: None)


def test_env_prints_every_missing_export_fix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env_setup(monkeypatch, tmp_path)
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("LANGGRAPH_URL", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(run, "probe_local_langgraph", lambda: False)
    monkeypatch.setattr(run, "gh_auth_token", lambda: "")

    assert run.cmd_env(argparse.Namespace()) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.out)["ready"] is False
    assert run.LINEAR_ENV_ERROR in captured.err
    assert run.LANGGRAPH_ENV_ERROR in captured.err
    assert run.GH_ENV_ERROR in captured.err


def test_env_is_not_ready_when_monitor_python_is_unusable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENSWE_STABLE_ROOT", str(tmp_path))
    monkeypatch.setenv("LINEAR_API_KEY", "linear")
    monkeypatch.setenv("LANGGRAPH_URL", "https://langgraph.invalid")
    monkeypatch.setenv("GH_TOKEN", "github")

    def unusable_monitor_python() -> None:
        raise run.RunError("bad sdk")

    monkeypatch.setattr(run, "skill_checkout_warning", lambda: None)
    monkeypatch.setattr(run, "resolve_monitor_python", unusable_monitor_python)

    assert run.cmd_env(argparse.Namespace()) == 2

    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is False
    assert report["monitor_python"] == "UNRESOLVED: bad sdk"


def test_env_requires_a_token_not_just_the_gh_executable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env_setup(monkeypatch, tmp_path)
    monkeypatch.setenv("LINEAR_API_KEY", "linear")
    monkeypatch.setenv("LANGGRAPH_URL", "https://langgraph.invalid")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(run.shutil, "which", lambda command: "/usr/bin/gh")
    monkeypatch.setattr(
        run.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr=""),
    )

    assert run.cmd_env(argparse.Namespace()) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.out)["gh_token"] is False
    assert run.GH_ENV_ERROR in captured.err


def test_env_accepts_a_healthy_tunnel_after_a_cold_first_hit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cold_tunnel: list[int],
) -> None:
    _env_setup(monkeypatch, tmp_path)
    monkeypatch.setenv("LINEAR_API_KEY", "linear")
    monkeypatch.delenv("LANGGRAPH_URL", raising=False)
    monkeypatch.setenv("GH_TOKEN", "github")

    assert run.cmd_env(argparse.Namespace()) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is True
    assert report["langgraph_url"] == run.LOCAL_LANGGRAPH_URL
    assert cold_tunnel == [3, 3]


@pytest.mark.parametrize("fetch_returncode", [0, 1])
def test_skill_checkout_warning_detects_refreshed_or_stale_origin(
    monkeypatch: pytest.MonkeyPatch,
    fetch_returncode: int,
) -> None:
    def git(command, **kwargs):
        if command[-2:] == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(command, 0, stdout="/checkout\n", stderr="")
        if "fetch" in command:
            return subprocess.CompletedProcess(
                command, fetch_returncode, stdout="", stderr="offline"
            )
        if "@{upstream}" in command:
            return subprocess.CompletedProcess(command, 0, stdout="origin/main\n", stderr="")
        if "rev-list" in command:
            return subprocess.CompletedProcess(command, 0, stdout="2\n", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(run.subprocess, "run", git)

    warning = run.skill_checkout_warning()

    assert warning is not None
    assert "2 commits behind origin/main" in warning
    assert ("origin refresh failed, using stale origin/main" in warning) is bool(fetch_returncode)
    assert "git -C /checkout pull" in warning


def test_env_warns_about_checkout_drift_without_blocking_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env_setup(monkeypatch, tmp_path)
    monkeypatch.setenv("LINEAR_API_KEY", "linear")
    monkeypatch.setenv("LANGGRAPH_URL", "https://langgraph.invalid")
    monkeypatch.setenv("GH_TOKEN", "github")
    monkeypatch.setattr(
        run,
        "skill_checkout_warning",
        lambda: "skill checkout is 1 commit behind origin/main. Update it with: git pull",
    )

    assert run.cmd_env(argparse.Namespace()) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["ready"] is True
    assert "warning: skill checkout is 1 commit behind origin/main" in captured.err


def test_every_reference_path_named_in_skill_md_resolves_exactly() -> None:
    """A doc that points at a missing reference is a broken skill, not a typo."""
    skill_md = (SKILL / "SKILL.md").read_text()
    references = sorted(set(re.findall(r"`((?:\.\./)?[A-Za-z0-9_./-]+\.md)`", skill_md)))

    for reference in references:
        assert (SKILL / reference).is_file(), f"SKILL.md names missing path {reference}"


def test_wave_assets_resolve_from_the_sibling_skill_in_a_checkout() -> None:
    """The wave assets come from the sibling skill; without this fallback the
    skill is unrunnable from a checkout."""
    assert not (SKILL / "scripts/openswe_wave.py").exists()

    resolved = run.wave_scripts_dir()

    # wave_scripts_dir() derives from Path(__file__).resolve(); compare resolved
    # paths so a checkout reached through a symlink (macOS /tmp) still matches.
    assert resolved.resolve() == (WAVE_SKILL / "scripts").resolve()
    assert (resolved / "openswe_wave.py").is_file()
    assert (resolved / "wave-monitor").is_file()


def test_wave_symbols_the_script_calls_still_exist() -> None:
    """Guards against a rename in openswe-wave silently breaking this skill."""
    wave = run.import_wave_module()

    assert callable(wave.derive_linear_thread_id)


@pytest.mark.parametrize(
    "body",
    [
        "@openswe repo owner/name — Execute ABC-1 only. See <https://example.com>.",
        "@openswe plain body with no placeholders",
    ],
)
def test_placeholder_guard_allows_filled_bodies(body: str) -> None:
    run.guard_placeholders("ABC-1", body, False)


def test_placeholder_guard_rejects_unfilled_bodies() -> None:
    with pytest.raises(run.RunError, match="placeholder"):
        run.guard_placeholders("ABC-1", "@openswe Execute <TICKET> only.", False)


def test_dispatch_template_leaves_no_placeholder_its_own_guard_would_reject() -> None:
    body = run.DISPATCH_TEMPLATE.format(
        repo="owner/name",
        ticket="ABC-1",
        ref="main",
        scope="do the thing",
        boundaries="nothing else",
        verify="focused tests",
    )

    run.guard_placeholders("ABC-1", body, False)


def test_dispatch_template_matches_reference_docs() -> None:
    def dispatch_body(path: Path) -> str:
        section = path.read_text().split("## Dispatch\n", 1)[1]
        fenced = section.split("```markdown\n", 1)[1]
        return fenced.split("\n```", 1)[0] + "\n"

    expected = run.DISPATCH_TEMPLATE.format(
        repo="<owner/repo>",
        ticket="<TICKET>",
        ref="<ref>",
        scope="<scope>",
        boundaries="<non-goals>",
        verify="<focused tests>, `make lint`, and `make typecheck`",
    )

    assert dispatch_body(SKILL / "references/run-templates.md") == expected
    assert dispatch_body(WAVE_SKILL / "references/comment-templates.md") == expected


def _comment(
    comment_id: str,
    body: str,
    created_at: str,
    user_id: str,
    user_name: str,
) -> dict:
    return {
        "id": comment_id,
        "body": body,
        "createdAt": created_at,
        "user": {"id": user_id, "name": user_name},
    }


def _plan_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    comments: list[dict],
    *,
    last: int | None = None,
    viewer_id: str = "viewer-1",
) -> str:
    monkeypatch.setattr(run, "ensure_env", lambda *args, **kwargs: [])
    monkeypatch.setattr(run, "resolve_issue", lambda ticket: {"id": "issue-1"})
    monkeypatch.setattr(
        run,
        "linear_snapshot",
        lambda issue_id: {"viewer": {"id": viewer_id}, "comments": comments},
    )

    assert run.cmd_plan(argparse.Namespace(ticket="ABC-1", last=last)) == 0
    return capsys.readouterr().out


def test_plan_defaults_to_all_non_viewer_comments_since_latest_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    comments = [
        _comment(
            "approval", "@openswe Plan approved.", "2026-07-27T21:38:30Z", "viewer-1", "Operator"
        ),
        _comment("plan", "## Plan\nImplement it", "2026-07-27T21:37:58Z", "agent-1", "Open SWE"),
        _comment(
            "progress", "Re-anchoring against main", "2026-07-27T21:34:58Z", "agent-1", "Open SWE"
        ),
        _comment("ack", "On it!", "2026-07-27T21:34:41Z", "agent-1", "Open SWE"),
        _comment(
            "dispatch",
            "@openswe repo owner/name — Execute ABC-1 only.\n\nRequired scope: fix it.",
            "2026-07-27T21:34:40Z",
            "viewer-1",
            "Operator",
        ),
        _comment("old", "Earlier run", "2026-07-27T20:00:00Z", "agent-1", "Open SWE"),
    ]

    output = _plan_output(monkeypatch, capsys, comments)

    assert "Earlier run" not in output
    assert output.index("On it!") < output.index("Re-anchoring against main")
    assert output.index("Re-anchoring against main") < output.index("## Plan")
    assert output.count("----- Open SWE at") == 3


def test_plan_scopes_after_custom_repo_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    comments = [
        _comment("old", "Earlier run", "2026-07-27T20:00:00Z", "agent-1", "Open SWE"),
        _comment(
            "dispatch",
            "@openswe repo owner/name\n\nCustom dispatch body for ABC-1.",
            "2026-07-27T21:34:40Z",
            "viewer-1",
            "Operator",
        ),
        _comment("ack", "On it!", "2026-07-27T21:34:41Z", "agent-1", "Open SWE"),
        _comment("plan", "## Plan", "2026-07-27T21:37:58Z", "agent-1", "Open SWE"),
    ]

    output = _plan_output(monkeypatch, capsys, comments)

    assert "Earlier run" not in output
    assert output.index("On it!") < output.index("## Plan")
    assert output.count("----- Open SWE at") == 2


def test_plan_without_dispatch_falls_back_to_all_comments_with_true_authors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    comments = [
        _comment("agent", "## Plan", "2026-07-27T21:37:58Z", "agent-1", "Open SWE"),
        _comment(
            "operator", "Please revise", "2026-07-27T21:35:00Z", "operator-1", "Mobilyze Agents"
        ),
    ]

    output = _plan_output(monkeypatch, capsys, comments, viewer_id="service-viewer")

    assert output.index("Mobilyze Agents") < output.index("Open SWE")
    assert "----- Mobilyze Agents at 2026-07-27T21:35:00Z -----" in output
    assert "----- Open SWE at 2026-07-27T21:37:58Z -----" in output


@pytest.mark.parametrize(
    ("last", "expected", "excluded"),
    [
        (2, ["Progress", "## Plan"], ["On it!"]),
        (10, ["On it!", "Progress", "## Plan"], []),
    ],
)
def test_plan_last_narrows_the_dispatch_scoped_set(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    last: int,
    expected: list[str],
    excluded: list[str],
) -> None:
    comments = [
        _comment("plan", "## Plan", "2026-07-27T21:37:58Z", "agent-1", "Open SWE"),
        _comment("progress", "Progress", "2026-07-27T21:34:58Z", "agent-1", "Open SWE"),
        _comment("ack", "On it!", "2026-07-27T21:34:41Z", "agent-1", "Open SWE"),
        _comment(
            "dispatch",
            "@openswe repo owner/name — Execute ABC-1 only.",
            "2026-07-27T21:34:40Z",
            "viewer-1",
            "Operator",
        ),
    ]

    output = _plan_output(monkeypatch, capsys, comments, last=last)

    positions = [output.index(body) for body in expected]
    assert positions == sorted(positions)
    assert all(body not in output for body in excluded)


def test_locked_plan_statuses_match_the_products_refusals() -> None:
    """The dashboard refuses these; this path must not be more permissive."""
    assert set(run.PLAN_STATUS_LOCKED) == {"shared", "cancelled"}
    assert run.PLAN_STATUS_LOCKED[0] in run.PLAN_STATUS_SNIPPET
    assert run.PLAN_STATUS_LOCKED[1] in run.PLAN_STATUS_SNIPPET


def test_midnight_run_uses_one_log_and_report_includes_all_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Clock:
        current = datetime(2026, 7, 26, 23, 59, tzinfo=UTC)

        @classmethod
        def now(cls, tz: object) -> datetime:
            return cls.current

    monkeypatch.setenv("OPENSWE_STABLE_ROOT", str(tmp_path))
    monkeypatch.setattr(run, "datetime", Clock)

    path = run.log_path("ABC-1", new_run=True)
    run.dogfood("ABC-1", "ISSUE", "before midnight")
    Clock.current = datetime(2026, 7, 27, 0, 1, tzinfo=UTC)
    run.dogfood("ABC-1", "ISSUE", "after midnight")

    assert run.log_path("ABC-1") == path
    assert list((tmp_path / "handoffs").glob("ABC-1-*-run.md")) == [path]
    assert run.cmd_report(argparse.Namespace(ticket="ABC-1")) == 0
    output = capsys.readouterr().out
    assert "dogfood issues (2):" in output
    assert "before midnight" in output
    assert "after midnight" in output


def test_failed_start_retry_reuses_log_and_report_keeps_issues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENSWE_STABLE_ROOT", str(tmp_path))

    path = run.log_path("ABC-1", new_run=True)
    run.dogfood("ABC-1", "ISSUE", "failed start root cause: missing [cmd] dispatched marker")

    assert run.log_path("ABC-1", new_run=True) == path

    run.dogfood("ABC-1", "cmd", "dispatched ABC-1 to owner/name (https://linear/ABC-1)")
    run.dogfood("ABC-1", "ISSUE", "successful retry follow-up")

    assert run.cmd_report(argparse.Namespace(ticket="ABC-1")) == 0
    output = capsys.readouterr().out
    assert "dogfood issues (2):" in output
    assert "failed start root cause" in output
    assert "successful retry follow-up" in output


def test_start_after_confirmed_dispatch_rotates_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENSWE_STABLE_ROOT", str(tmp_path))

    first = run.log_path("ABC-1", new_run=True)
    run.dogfood("ABC-1", "cmd", "dispatched ABC-1 to owner/name (https://linear/ABC-1)")

    second = run.log_path("ABC-1", new_run=True)

    assert second != first
    assert sorted((tmp_path / "handoffs").glob("ABC-1-*-run.md")) == [first, second]


def test_non_start_log_establishes_a_timestamped_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENSWE_STABLE_ROOT", str(tmp_path))

    args = argparse.Namespace(ticket="ABC-1", issue=False, text="pre-dispatch evidence")
    assert run.cmd_log(args) == 0

    logs = list((tmp_path / "handoffs").glob("ABC-1-*-run.md"))
    assert len(logs) == 1
    assert re.fullmatch(r"ABC-1-\d{8}T\d{12}Z-run\.md", logs[0].name)
    assert str(logs[0]) in capsys.readouterr().out
    assert "pre-dispatch evidence" in logs[0].read_text()


def test_log_path_uses_the_lexically_latest_ticket_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENSWE_STABLE_ROOT", str(tmp_path))
    handoffs = run.ensure_handoffs()
    older = handoffs / "ABC-1-20260726-run.md"
    latest = handoffs / "ABC-1-20260727T000100000000Z-run.md"
    older.write_text("older")
    latest.write_text("latest")

    assert run.log_path("ABC-1") == latest


def test_report_discovers_a_legacy_dated_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENSWE_STABLE_ROOT", str(tmp_path))
    legacy = run.ensure_handoffs() / "ABC-1-20260726-run.md"
    legacy.write_text("- 2026-07-26T23:59:00Z [ISSUE] legacy evidence\n")

    assert run.cmd_report(argparse.Namespace(ticket="ABC-1")) == 0

    output = capsys.readouterr().out
    assert f"log: {legacy}" in output
    assert "legacy evidence" in output


def test_report_prints_merged_pr_url_and_sha(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENSWE_STABLE_ROOT", str(tmp_path))
    handoffs = run.ensure_handoffs()
    path = handoffs / "ABC-1-20260727T000100000000Z-run.md"
    path.write_text(
        "- 2026-07-27T00:01:00Z [cmd] dispatched ABC-1 to owner/name (https://linear/ABC-1)\n"
        "- 2026-07-27T00:02:00Z [wake] pr_opened via wave-monitor: PR #42 opened ready\n"
        "- 2026-07-27T00:03:00Z [wake] terminal_merged via wave-monitor: PR merged\n"
    )
    monkeypatch.setattr(run, "ensure_handoffs", lambda: handoffs)
    calls: list[list[str]] = []

    def gh(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "url": "https://github.com/owner/name/pull/42",
                    "mergeCommit": {"oid": "abc123"},
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(run.subprocess, "run", gh)

    assert run.cmd_report(argparse.Namespace(ticket="ABC-1")) == 0

    output = capsys.readouterr().out
    assert "terminal state: terminal_merged" in output
    assert "PR: https://github.com/owner/name/pull/42" in output
    assert "merge SHA: abc123" in output
    assert calls == [
        [
            "gh",
            "pr",
            "view",
            "42",
            "--repo",
            "owner/name",
            "--json",
            "url,mergeCommit",
        ]
    ]


def test_report_merged_lookup_failure_falls_back_without_hiding_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENSWE_STABLE_ROOT", str(tmp_path))
    handoffs = run.ensure_handoffs()
    path = handoffs / "ABC-1-20260727T000100000000Z-run.md"
    path.write_text(
        "- 2026-07-27T00:01:00Z [cmd] dispatched ABC-1 to owner/name (https://linear/ABC-1)\n"
        "- 2026-07-27T00:02:00Z [wake] pr_opened via wave-monitor: PR #42 opened ready\n"
        "- 2026-07-27T00:03:00Z [wake] terminal_merged via wave-monitor: PR merged\n"
        "- 2026-07-27T00:04:00Z [ISSUE] retained evidence\n"
    )
    monkeypatch.setattr(run, "ensure_handoffs", lambda: handoffs)

    def unavailable_gh(command: list[str], **kwargs: Any) -> None:
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(run.subprocess, "run", unavailable_gh)

    assert run.cmd_report(argparse.Namespace(ticket="ABC-1")) == 0

    output = capsys.readouterr().out
    assert "terminal state: terminal_merged" in output
    assert "PR: owner/name#42" in output
    assert "merge SHA:" not in output
    assert "retained evidence" in output
    assert "Reminder: end the run" in output


def test_report_refuses_to_create_a_missing_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENSWE_STABLE_ROOT", str(tmp_path))

    with pytest.raises(run.RunError, match="No dogfood log found for ABC-1"):
        run.cmd_report(argparse.Namespace(ticket="ABC-1"))

    assert not list((tmp_path / "handoffs").glob("ABC-1-*-run.md"))


def test_no_operator_home_path_is_hardcoded_in_the_source() -> None:
    """This repo is public. The resolved value contains a home path by design;
    what must not appear is a literal one baked into the source."""
    assert run.DEFAULT_STABLE_ROOT.startswith(str(Path.home()))

    for path in (SCRIPT_PATH, SKILL / "SKILL.md"):
        assert not re.search(r"/(Users|home)/[a-z]", path.read_text()), (
            f"{path.name} hardcodes an operator home directory"
        )


@pytest.mark.parametrize(
    "body",
    [
        "@openswe Plan approved.",
        "@openswe repo owner/name — Execute ABC-1 only.",
        "@openswe repo:owner/name — Execute ABC-1 only.",
        "@OpenSWE Plan approved.",
        "@OpenSWE Repo owner/name — Execute ABC-1 only.",
        "@OpEnSwE rEpO:owner/name — Execute ABC-1 only.",
    ],
)
def test_body_hygiene_allows_only_the_first_line_directive(body: str) -> None:
    run.guard_body_hygiene(body)


@pytest.mark.parametrize(
    "body",
    [
        " @openswe Plan approved.",
        "@openswe Plan approved.\n@openswe Continue.",
        "@openswe Plan approved.\n@OpenSWE Continue.",
        "@openswe Plan approved.\nExample: repo owner/name",
        "@openswe Plan approved.\nExample: RePo owner/name",
        "@openswe Plan approved.\nExample: repo:owner/name",
        "@openswe Plan approved.\nExample: repo: owner/name",
        "@openswe Continue in repo owner/name.",
    ],
)
def test_body_hygiene_rejects_ambiguous_directives(body: str) -> None:
    with pytest.raises(run.RunError):
        run.guard_body_hygiene(body)


def test_force_cannot_bypass_body_hygiene(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        run,
        "_post_with_handoff",
        lambda *args, **kwargs: pytest.fail("handoff child must not start after a hygiene refusal"),
    )
    args = argparse.Namespace(ticket="ABC-1", force=True)

    with pytest.raises(run.RunError, match="begin exactly"):
        run._post_prepared(
            args,
            "comment",
            "not a directive",
            issue={"id": "issue-1", "identifier": "ABC-1"},
        )


def test_handoff_monitor_uses_one_sdk_import_and_recent_run_window() -> None:
    assert run.HANDOFF_MONITOR_SNIPPET.count("from langgraph_sdk import get_client") == 1
    assert "client = get_client(url=URL)" in run.HANDOFF_MONITOR_SNIPPET
    assert "snapshot(client)" in run.HANDOFF_MONITOR_SNIPPET
    assert "sys.stdin.readline()" in run.HANDOFF_MONITOR_SNIPPET
    assert "runs.list(THREAD, limit=100)" in run.HANDOFF_MONITOR_SNIPPET
    assert "limit=1000" not in run.HANDOFF_MONITOR_SNIPPET


def test_handoff_start_spawns_one_child_and_waits_for_baseline_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict]] = []

    class Stdout:
        def readline(self) -> str:
            return run.HANDOFF_BASELINE_SENTINEL + "child-owned baseline"

    class Process:
        stdout = Stdout()

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return Process()

    monkeypatch.setattr(run, "resolve_monitor_python", lambda: ["python-with-sdk"])
    monkeypatch.setattr(run.subprocess, "Popen", popen)
    monkeypatch.setattr(run.select, "select", lambda *args: ([Process.stdout], [], []))

    process = run._start_handoff_process("comment", "ABC-1", "thread-1")

    assert isinstance(process, Process)
    assert len(calls) == 1
    assert calls[0][0] == ["python-with-sdk", "-c", run.HANDOFF_MONITOR_SNIPPET]


@pytest.mark.parametrize("baseline_status", ["missing", "idle", "busy"])
def test_child_preserves_handoff_success_rules(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    baseline_status: str,
) -> None:
    class Missing(Exception):
        status_code = 404

    if baseline_status == "missing":
        statuses = [Missing(), {"status": "busy"}]
        run_lists = [[]]
    elif baseline_status == "idle":
        statuses = [{"status": "idle"}, {"status": "busy"}]
        run_lists = [[{"run_id": "run-1"}], [{"run_id": "run-1"}]]
    else:
        statuses = [{"status": "busy"}, {"status": "busy"}, {"status": "busy"}]
        run_lists = [
            [{"run_id": "run-1"}],
            [{"run_id": "run-1"}],
            [{"run_id": "run-1"}, {"run_id": "run-2"}],
        ]

    class Threads:
        async def get(self, thread_id):
            value = statuses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

    class Runs:
        async def list(self, thread_id, *, limit):
            assert limit == 100
            return run_lists.pop(0)

    client = types.SimpleNamespace(threads=Threads(), runs=Runs())
    sdk: Any = types.ModuleType("langgraph_sdk")
    sdk.get_client = lambda *, url: client
    monkeypatch.setitem(sys.modules, "langgraph_sdk", sdk)
    monkeypatch.setenv("OPENSWE_HANDOFF_THREAD", "thread-1")
    monkeypatch.setenv("OPENSWE_HANDOFF_ACTION", "comment")
    monkeypatch.setenv("OPENSWE_HANDOFF_TICKET", "ABC-1")
    monkeypatch.setenv("OPENSWE_HANDOFF_PLAN_CONTEXT", "null")
    monkeypatch.setenv("OPENSWE_HANDOFF_TIMEOUT", "1")
    monkeypatch.setenv("OPENSWE_HANDOFF_POLL_INTERVAL", "0")
    monkeypatch.setattr(sys, "stdin", io.StringIO("posted\n"))

    exec(run.HANDOFF_MONITOR_SNIPPET, {})

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    result = json.loads(lines[-1][len(run.HANDOFF_RESULT_SENTINEL) :])
    assert result["handoff"]["thread_status"] == "busy"
    if baseline_status == "busy":
        assert result["handoff"]["run_ids"] == ["run-1", "run-2"]


def test_child_poll_timeout_is_aggregate_and_cancels_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cancelled = False
    thread_reads = 0

    class Threads:
        async def get(self, thread_id):
            nonlocal cancelled, thread_reads
            thread_reads += 1
            if thread_reads == 1:
                return {"status": "idle"}
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                cancelled = True
                raise

    class Runs:
        async def list(self, thread_id, *, limit):
            return []

    client = types.SimpleNamespace(threads=Threads(), runs=Runs())
    sdk: Any = types.ModuleType("langgraph_sdk")
    sdk.get_client = lambda *, url: client
    monkeypatch.setitem(sys.modules, "langgraph_sdk", sdk)
    monkeypatch.setenv("OPENSWE_HANDOFF_THREAD", "thread-1")
    monkeypatch.setenv("OPENSWE_HANDOFF_ACTION", "comment")
    monkeypatch.setenv("OPENSWE_HANDOFF_TICKET", "ABC-1")
    monkeypatch.setenv("OPENSWE_HANDOFF_PLAN_CONTEXT", "null")
    monkeypatch.setenv("OPENSWE_HANDOFF_TIMEOUT", "0.01")
    monkeypatch.setenv("OPENSWE_HANDOFF_POLL_INTERVAL", "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO("posted\n"))

    exec(run.HANDOFF_MONITOR_SNIPPET, {})

    lines = capsys.readouterr().out.splitlines()
    result = json.loads(lines[-1][len(run.HANDOFF_RESULT_SENTINEL) :])
    assert cancelled is True
    assert "LangGraph handoff timeout" in result["error"]
    assert '"final": {"error": ""}' in result["error"]
    assert "async with asyncio.timeout(remaining)" in run.HANDOFF_MONITOR_SNIPPET
    assert (
        "await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))" in run.HANDOFF_MONITOR_SNIPPET
    )
    assert "time.sleep(" not in run.HANDOFF_MONITOR_SNIPPET


def test_shared_post_helper_uses_one_child_for_baseline_post_and_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Stdin:
        def write(self, value: str) -> None:
            assert value == "posted\n"
            events.append("signal")

        def flush(self) -> None:
            return None

    process = types.SimpleNamespace(stdin=Stdin())

    def start(*args, **kwargs):
        events.append("baseline")
        return process

    monkeypatch.setattr(run, "_start_handoff_process", start)
    monkeypatch.setattr(run, "post_comment", lambda *args: events.append("post"))
    monkeypatch.setattr(
        run,
        "_await_handoff",
        lambda actual_process, thread_id: (
            events.append("poll") or {"thread_status": "busy", "run_ids": ["run-1"]}
        ),
    )

    final = run._post_with_handoff("comment", "ABC-1", "issue-1", "@openswe Continue", "thread-1")

    assert final == {"thread_status": "busy", "run_ids": ["run-1"]}
    assert events == ["baseline", "post", "signal", "poll"]


@pytest.mark.parametrize(
    ("command", "action", "status", "plan_mode"),
    [
        (run.cmd_approve, "approval", "approved", False),
        (run.cmd_reject, "rejection", "revising", True),
    ],
)
def test_plan_actions_guard_transition_shared_baseline_post_then_poll(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command,
    action: str,
    status: str,
    plan_mode: bool,
) -> None:
    events: list[str] = []
    logs: list[str] = []
    monkeypatch.setattr(run, "ensure_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "read_body", lambda args: "@openswe Adjudication body")
    monkeypatch.setattr(run, "guard_body_hygiene", lambda body: events.append("body_hygiene"))
    monkeypatch.setattr(
        run,
        "guard_placeholders",
        lambda ticket, body, force: events.append("placeholders"),
    )
    monkeypatch.setattr(
        run,
        "resolve_issue",
        lambda ticket: {"id": "issue-1", "identifier": "ABC-1"},
    )
    monkeypatch.setattr(
        run,
        "import_wave_module",
        lambda: type("Wave", (), {"derive_linear_thread_id": lambda issue_id: "thread-1"}),
    )

    def set_status(thread_id: str, actual_status: str, *, plan_mode: bool) -> dict:
        assert actual_status == status
        assert plan_mode is expected_plan_mode
        events.append("plan_transition")
        return {"previous": "ready", "status": actual_status, "metadata_ok": True}

    expected_plan_mode = plan_mode
    monkeypatch.setattr(run, "set_plan_status", set_status)
    monkeypatch.setattr(run, "dogfood", lambda ticket, tag, message: logs.append(message))

    def post_with_handoff(actual_action: str, *args, **kwargs) -> dict:
        assert actual_action == action
        events.extend(["baseline", "post_comment", "poll"])
        return {"thread_status": "busy", "run_ids": ["run-1", "run-2"]}

    monkeypatch.setattr(run, "_post_with_handoff", post_with_handoff)
    args = argparse.Namespace(ticket="ABC-1", body_file="body.md", force=False, adjudicated=True)

    assert command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "identifier": "ABC-1",
        "posted": action,
        "handoff": {"thread_status": "busy", "run_ids": ["run-1", "run-2"]},
    }
    assert any("plan record 'ready' ->" in message for message in logs)
    assert any(
        f"{action} posted on ABC-1; handoff status=busy runs=2" in message for message in logs
    )
    assert events == [
        "body_hygiene",
        "placeholders",
        "plan_transition",
        "baseline",
        "post_comment",
        "poll",
    ]


def test_timeout_annotation_exists_only_in_child_timeout_evidence() -> None:
    assert run.HANDOFF_MONITOR_SNIPPET.count("plan_status_nontransactional") == 1
    source = MODULE_PATH.read_text()
    assert source.count("plan_status_nontransactional") == 1
    assert 'evidence["plan_status_nontransactional"] = PLAN_CONTEXT' in source


def test_child_timeout_result_is_reported_directly() -> None:
    evidence = {
        "action": "approval",
        "ticket": "ABC-1",
        "thread_id": "thread-1",
        "baseline": {"thread_status": "busy", "run_ids": ["run-1"]},
        "final": {"thread_status": "busy", "run_ids": ["run-1"]},
        "timeout_seconds": 60.0,
        "plan_status_nontransactional": {"status": "approved", "rollback": "not automatic"},
    }

    class Process:
        returncode = 0

        def communicate(self, timeout=None):
            payload = {
                "error": "LangGraph handoff timeout: " + json.dumps(evidence, sort_keys=True)
            }
            return run.HANDOFF_RESULT_SENTINEL + json.dumps(payload) + "\n", ""

    with pytest.raises(run.RunError) as raised:
        run._await_handoff(Process(), "thread-1")

    message = str(raised.value)
    assert '"action": "approval"' in message
    assert '"baseline"' in message
    assert '"final"' in message
    assert '"plan_status_nontransactional"' in message


def _start_args(**overrides) -> argparse.Namespace:
    values = {
        "ticket": "ABC-1",
        "repo": "owner/name",
        "ref": "main",
        "scope": None,
        "boundaries": None,
        "verify": None,
        "body_file": None,
        "dry_run": False,
        "force": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_issue_query_resolves_workflow_state_and_terminal_timestamps() -> None:
    assert "state { type name }" in run.ISSUE_QUERY
    assert "completedAt" in run.ISSUE_QUERY
    assert "canceledAt" in run.ISSUE_QUERY


@pytest.mark.parametrize(
    ("state_type", "state_name", "timestamp_field", "timestamp"),
    [
        ("completed", "Done", "completedAt", "2026-07-26T16:11:00Z"),
        ("canceled", "Canceled", "canceledAt", "2026-07-26T17:12:00Z"),
    ],
)
def test_start_refuses_terminal_issue_and_dogfood_logs_evidence(
    monkeypatch: pytest.MonkeyPatch,
    state_type: str,
    state_name: str,
    timestamp_field: str,
    timestamp: str,
) -> None:
    logs: list[tuple[str, str]] = []
    issue = {
        "id": "issue-1",
        "identifier": "ABC-1",
        "url": "https://linear.example/ABC-1",
        "state": {"type": state_type, "name": state_name},
        timestamp_field: timestamp,
    }
    monkeypatch.setattr(run, "log_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "ensure_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "resolve_issue", lambda ticket: issue)
    monkeypatch.setattr(
        run,
        "import_wave_module",
        lambda: type("Wave", (), {"derive_linear_thread_id": lambda issue_id: "thread-1"}),
    )
    monkeypatch.setattr(run, "dogfood", lambda ticket, tag, message: logs.append((tag, message)))
    monkeypatch.setattr(
        run,
        "_post_with_handoff",
        lambda *args, **kwargs: pytest.fail("terminal issue must be refused before handoff"),
    )

    with pytest.raises(run.RunError) as raised:
        run.cmd_start(_start_args())

    message = str(raised.value)
    assert state_name in message
    assert state_type in message
    assert f"{timestamp_field}={timestamp}" in message
    assert "--force" in message
    assert logs == [
        (
            "error",
            f"refused dispatch for ABC-1: state {state_name!r} (type {state_type!r}); "
            f"completion evidence: {timestamp_field}={timestamp}",
        )
    ]


def test_start_terminal_issue_without_timestamp_uses_state_only_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs: list[str] = []
    issue = {
        "id": "issue-1",
        "identifier": "ABC-1",
        "url": "https://linear.example/ABC-1",
        "state": {"type": "completed", "name": "Done"},
        "completedAt": None,
    }
    monkeypatch.setattr(run, "log_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "ensure_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "resolve_issue", lambda ticket: issue)
    monkeypatch.setattr(
        run,
        "import_wave_module",
        lambda: type("Wave", (), {"derive_linear_thread_id": lambda issue_id: "thread-1"}),
    )
    monkeypatch.setattr(run, "dogfood", lambda ticket, tag, message: logs.append(message))

    with pytest.raises(run.RunError) as raised:
        run.cmd_start(_start_args())

    assert "state 'Done' (type 'completed')" in str(raised.value)
    assert "completedAt" not in str(raised.value)
    assert "state 'Done' (type 'completed')" in logs[0]


def test_start_force_logs_terminal_override_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    issue = {
        "id": "issue-1",
        "identifier": "ABC-1",
        "url": "https://linear.example/ABC-1",
        "state": {"type": "completed", "name": "Done"},
        "completedAt": "2026-07-26T16:11:00Z",
    }
    monkeypatch.setattr(run, "log_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "ensure_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "resolve_issue", lambda ticket: issue)
    monkeypatch.setattr(
        run,
        "import_wave_module",
        lambda: type("Wave", (), {"derive_linear_thread_id": lambda issue_id: "thread-1"}),
    )
    monkeypatch.setattr(run, "dogfood", lambda ticket, tag, message: events.append(message))
    monkeypatch.setattr(
        run,
        "_post_with_handoff",
        lambda *args, **kwargs: (
            events.append("handoff") or {"thread_status": "busy", "run_ids": ["run-1"]}
        ),
    )

    assert run.cmd_start(_start_args(force=True)) == 0
    capsys.readouterr()
    assert "--force overriding terminal Linear state 'Done'" in events[0]
    assert "completedAt=2026-07-26T16:11:00Z" in events[0]
    assert events[1] == "handoff"
    assert events[2].startswith("dispatched ABC-1 to owner/name")


def test_start_force_cannot_bypass_body_hygiene(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run, "log_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "ensure_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        run,
        "resolve_issue",
        lambda ticket: {
            "id": "issue-1",
            "identifier": "ABC-1",
            "url": "https://linear.example/ABC-1",
            "state": {"type": "completed", "name": "Done"},
        },
    )
    monkeypatch.setattr(run, "read_body", lambda args: "not a directive")
    monkeypatch.setattr(
        run,
        "import_wave_module",
        lambda: pytest.fail("body hygiene must run before terminal-state force handling"),
    )

    with pytest.raises(run.RunError, match="begin exactly"):
        run.cmd_start(_start_args(body_file="body.md", force=True))


@pytest.mark.parametrize("terminal_period", ["", "."])
def test_start_dry_run_normalizes_punctuation_without_posting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    terminal_period: str,
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(run, "ensure_env", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(
        run,
        "resolve_issue",
        lambda ticket: {
            "id": "issue-1",
            "identifier": "ABC-1",
            "url": "https://linear.example/ABC-1",
            "state": {"type": "completed", "name": "Done"},
            "completedAt": "2026-07-26T16:11:00Z",
        },
    )
    monkeypatch.setattr(run, "dogfood", lambda *args: pytest.fail("dry-run must not log"))
    monkeypatch.setattr(
        run,
        "import_wave_module",
        lambda: type("Wave", (), {"derive_linear_thread_id": lambda issue_id: "thread-1"}),
    )
    monkeypatch.setattr(
        run,
        "_post_with_handoff",
        lambda *args, **kwargs: pytest.fail("dry-run must not start a handoff child"),
    )
    monkeypatch.setattr(
        run,
        "post_comment",
        lambda issue_id, body: pytest.fail("dry-run must not post"),
    )
    args = argparse.Namespace(
        ticket="ABC-1",
        repo="owner/name",
        ref="main",
        scope=f"do the thing{terminal_period}",
        boundaries=f"nothing else{terminal_period}",
        verify=f"focused tests{terminal_period}",
        body_file=None,
        dry_run=True,
        force=False,
    )

    assert run.cmd_start(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["issue_state"] == {"type": "completed", "name": "Done"}
    assert "Required scope: do the thing.\n" in payload["body"]
    assert "Boundaries: nothing else.\n" in payload["body"]
    assert "Verification: focused tests.\n" in payload["body"]
    assert calls == [{"langgraph": False, "github": False}]


def test_start_success_records_handoff_in_json_and_dogfood(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    logs: list[str] = []
    final = {"thread_status": "busy", "run_ids": ["run-1"]}
    monkeypatch.setattr(run, "ensure_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        run,
        "resolve_issue",
        lambda ticket: {
            "id": "issue-1",
            "identifier": "ABC-1",
            "url": "https://linear.example/ABC-1",
        },
    )
    monkeypatch.setattr(
        run,
        "import_wave_module",
        lambda: type("Wave", (), {"derive_linear_thread_id": lambda issue_id: "thread-1"}),
    )
    monkeypatch.setattr(run, "_post_with_handoff", lambda *args, **kwargs: final)
    new_runs: list[bool] = []
    monkeypatch.setattr(run, "log_path", lambda ticket, *, new_run=False: new_runs.append(new_run))
    monkeypatch.setattr(run, "dogfood", lambda ticket, tag, message: logs.append(message))
    args = argparse.Namespace(
        ticket="ABC-1",
        repo="owner/name",
        ref="main",
        scope=None,
        boundaries=None,
        verify=None,
        body_file=None,
        dry_run=False,
        force=False,
    )

    assert run.cmd_start(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["handoff"] == final
    assert new_runs == [True]
    assert logs == [
        "dispatched ABC-1 to owner/name (https://linear.example/ABC-1); handoff status=busy runs=1"
    ]


def test_watch_parser_defaults_to_plan_and_keeps_timeout_override_authoritative() -> None:
    parser = run.build_parser()
    default = parser.parse_args(["watch", "--ticket", "ABC-1", "--repo", "owner/name"])
    explicit = parser.parse_args(
        [
            "watch",
            "--ticket",
            "ABC-1",
            "--repo",
            "owner/name",
            "--phase",
            "delivery",
            "--timeout-min",
            "7.5",
        ]
    )

    assert default.phase == "plan"
    assert default.timeout_min is None
    assert default.func is run.cmd_watch
    assert run.watch_timeout_min(default) == 30.0
    assert run.watch_timeout_min(explicit) == 7.5


@pytest.mark.parametrize(
    ("phase", "override", "expected"),
    [("plan", None, 30.0), ("delivery", None, 90.0), ("plan", 7.5, 7.5)],
)
def test_watch_phase_defaults_and_explicit_override(
    phase: str, override: float | None, expected: float
) -> None:
    args = argparse.Namespace(phase=phase, timeout_min=override)

    assert run.watch_timeout_min(args) == expected


@pytest.mark.parametrize("command_name", ["comment", "nudge"])
def test_midrun_posts_require_langgraph_for_handoff(
    monkeypatch: pytest.MonkeyPatch, command_name: str
) -> None:
    environments: list[dict] = []
    monkeypatch.setattr(run, "ensure_env", lambda *args, **kwargs: environments.append(kwargs))
    monkeypatch.setattr(
        run,
        "resolve_issue",
        lambda ticket: {"id": "issue-1", "identifier": "ABC-1"},
    )
    monkeypatch.setattr(run, "read_body", lambda args: "@openswe Continue")
    monkeypatch.setattr(run, "_post_prepared", lambda *args, **kwargs: 0)
    if command_name == "comment":
        args = argparse.Namespace(ticket="ABC-1", body_file="body.md", force=False)
        result = run.cmd_comment(args)
    else:
        args = argparse.Namespace(ticket="ABC-1", minutes=30)
        result = run.cmd_nudge(args)

    assert result == 0
    assert environments == [{"langgraph": True, "github": False}]


@pytest.mark.parametrize(
    ("phase", "override", "elapsed", "expected_timeout"),
    [("plan", None, 1801.0, 30.0), ("delivery", 12.0, 721.0, 12.0)],
)
def test_watch_timeout_evidence_includes_phase_and_effective_deadline(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    override: float | None,
    elapsed: float,
    expected_timeout: float,
) -> None:
    wakes: list[dict] = []
    logs: list[str] = []

    class Process:
        def terminate(self) -> None:
            return None

    moments = iter([0.0, 0.0, elapsed])
    monkeypatch.setattr(run.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(run, "ensure_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "import_wave_module", lambda: object())
    monkeypatch.setattr(
        run,
        "resolve_issue",
        lambda ticket: {"id": "issue-1", "identifier": "ABC-1"},
    )
    monkeypatch.setattr(
        run,
        "linear_snapshot",
        lambda issue_id: {"viewer": {"id": "viewer-1"}, "comments": []},
    )
    monkeypatch.setattr(run, "dogfood", lambda ticket, tag, text: logs.append(text))
    monkeypatch.setattr(run, "_spawn_monitor", lambda args, issue_id: (Process(), []))
    monkeypatch.setattr(run, "_emit_wake", lambda ticket, wake, source: wakes.append(wake))
    args = argparse.Namespace(
        ticket="ABC-1",
        repo="owner/name",
        pr_number=None,
        phase=phase,
        interval=60,
        timeout_min=override,
        heartbeat_min=10,
        max_restarts=2,
        follow=False,
    )

    assert run.cmd_watch(args) == run.WAKE_TIMEOUT_EXIT
    assert phase in logs[0]
    assert wakes == [
        {
            "wake_node": "watch_timeout",
            "summary": f"no {phase} wake within {expected_timeout} minutes; monitor stopped",
            "evidence": {
                "issue_id": "issue-1",
                "identifier": "ABC-1",
                "phase": phase,
                "timeout_min": expected_timeout,
            },
        }
    ]
