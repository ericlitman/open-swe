from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent import reconcile, scheduler


def _run(run_id: str, thread_id: str, age_seconds: float) -> dict[str, Any]:
    created = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return {
        "run_id": run_id,
        "thread_id": thread_id,
        "status": "pending",
        "created_at": created.isoformat(),
    }


class _FakeThreads:
    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self._pages = pages
        self.search_calls: list[dict[str, Any]] = []
        self.update = AsyncMock(return_value=None)

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.search_calls.append(kwargs)
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", 100)
        index = offset // limit if limit else 0
        if index < len(self._pages):
            return self._pages[index]
        return []


class _FakeRuns:
    def __init__(self, runs_by_thread: dict[str, Any]) -> None:
        self._runs_by_thread = runs_by_thread
        self.cancel_many = AsyncMock(return_value=None)
        self.list_calls: list[tuple[str, dict[str, Any]]] = []

    async def list(self, thread_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.list_calls.append((thread_id, kwargs))
        value = self._runs_by_thread.get(thread_id, [])
        if isinstance(value, Exception):
            raise value
        return value


class _FakeClient:
    def __init__(self, threads: _FakeThreads, runs: _FakeRuns) -> None:
        self.threads = threads
        self.runs = runs


def _patch(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    monkeypatch.setattr(reconcile, "langgraph_client", lambda: client)


@pytest.mark.asyncio
async def test_cancels_only_stale_pending_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = _FakeThreads([[{"thread_id": "t1"}]])
    runs = _FakeRuns(
        {
            "t1": [
                _run("old1", "t1", age_seconds=4000),
                _run("fresh1", "t1", age_seconds=60),
                _run("old2", "t1", age_seconds=10000),
            ]
        }
    )
    _patch(monkeypatch, _FakeClient(threads, runs))

    counts = await reconcile.reconcile_stale_runs(max_age_seconds=1800)

    assert counts == {"threads_checked": 1, "stale_runs": 2, "cancelled": 2}
    runs.cancel_many.assert_awaited_once()
    assert runs.cancel_many.await_args is not None
    kwargs = runs.cancel_many.await_args.kwargs
    assert kwargs["thread_id"] == "t1"
    assert sorted(kwargs["run_ids"]) == ["old1", "old2"]


@pytest.mark.asyncio
async def test_no_stale_runs_means_no_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = _FakeThreads([[{"thread_id": "t1"}]])
    runs = _FakeRuns({"t1": [_run("fresh1", "t1", age_seconds=30)]})
    _patch(monkeypatch, _FakeClient(threads, runs))

    counts = await reconcile.reconcile_stale_runs(max_age_seconds=1800)

    assert counts == {"threads_checked": 1, "stale_runs": 0, "cancelled": 0}
    runs.cancel_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_thread_does_not_abort_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = _FakeThreads([[{"thread_id": "bad"}, {"thread_id": "good"}]])
    runs = _FakeRuns(
        {
            "bad": RuntimeError("runs.list exploded"),
            "good": [_run("old1", "good", age_seconds=5000)],
        }
    )
    _patch(monkeypatch, _FakeClient(threads, runs))

    counts = await reconcile.reconcile_stale_runs(max_age_seconds=1800)

    # Both threads counted; the good thread is still reconciled despite the bad one.
    assert counts == {"threads_checked": 2, "stale_runs": 1, "cancelled": 1}
    runs.cancel_many.assert_awaited_once()
    assert runs.cancel_many.await_args is not None
    assert runs.cancel_many.await_args.kwargs["thread_id"] == "good"
    assert runs.cancel_many.await_args.kwargs["run_ids"] == ["old1"]


@pytest.mark.asyncio
async def test_paginates_busy_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    full_page = [{"thread_id": f"t{i}"} for i in range(reconcile._SEARCH_PAGE_SIZE)]
    second_page = [{"thread_id": "tail"}]
    threads = _FakeThreads([full_page, second_page])
    runs_by_thread: dict[str, Any] = {t["thread_id"]: [] for t in full_page}
    runs_by_thread["tail"] = [_run("old", "tail", age_seconds=9000)]
    runs = _FakeRuns(runs_by_thread)
    _patch(monkeypatch, _FakeClient(threads, runs))

    counts = await reconcile.reconcile_stale_runs(max_age_seconds=1800)

    assert counts["threads_checked"] == reconcile._SEARCH_PAGE_SIZE + 1
    assert counts["cancelled"] == 1
    # Two search calls: first full page triggers a second page fetch.
    assert len(threads.search_calls) == 2
    assert threads.search_calls[0]["offset"] == 0
    assert threads.search_calls[1]["offset"] == reconcile._SEARCH_PAGE_SIZE
    assert threads.search_calls[0]["status"] == "busy"


@pytest.mark.asyncio
async def test_unparseable_created_at_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    threads = _FakeThreads([[{"thread_id": "t1"}]])
    runs = _FakeRuns(
        {
            "t1": [
                {
                    "run_id": "bad",
                    "thread_id": "t1",
                    "status": "pending",
                    "created_at": "not-a-date",
                },
                _run("old", "t1", age_seconds=5000),
            ]
        }
    )
    _patch(monkeypatch, _FakeClient(threads, runs))

    counts = await reconcile.reconcile_stale_runs(max_age_seconds=1800)

    assert counts == {"threads_checked": 1, "stale_runs": 1, "cancelled": 1}
    assert runs.cancel_many.await_args is not None
    assert runs.cancel_many.await_args.kwargs["run_ids"] == ["old"]


def _auto_merge_thread(**metadata: Any) -> dict[str, Any]:
    base = {
        "pr_owner": "acme",
        "pr_repo": "widget",
        "pr_number": 7,
        "auto_merge_intent": True,
        "auto_merge_reconcile": True,
        "auto_merge_phase": "pending",
        "auto_merge_phase_at": datetime.now(UTC).isoformat(),
        "auto_merge_head_sha": "",
    }
    base.update(metadata)
    return {"thread_id": "agent-thread", "metadata": base}


def _pr_data(**overrides: Any) -> dict[str, Any]:
    pr = {
        "id": "PR_1",
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "main",
        "headRefOid": "abc123",
        "labels": {"nodes": []},
    }
    pr.update(overrides)
    return {"repository": {"defaultBranchRef": {"name": "main"}, "pullRequest": pr}}


def _check(
    name: str,
    *,
    head_sha: str = "abc123",
    status: str = "completed",
    conclusion: str | None = "success",
    app: str = "mergify",
) -> dict[str, Any]:
    return {
        "name": name,
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "app": {"slug": app},
    }


def _checks(
    *,
    head_sha: str = "abc123",
    protections_status: str = "completed",
    protections_conclusion: str | None = "success",
    queue_status: str = "completed",
    queue_conclusion: str | None = "neutral",
) -> dict[str, dict[str, Any]]:
    return {
        reconcile._MERGIFY_PROTECTIONS_CHECK: _check(
            reconcile._MERGIFY_PROTECTIONS_CHECK,
            head_sha=head_sha,
            status=protections_status,
            conclusion=protections_conclusion,
        ),
        reconcile._MERGIFY_QUEUE_CHECK: _check(
            reconcile._MERGIFY_QUEUE_CHECK,
            head_sha=head_sha,
            status=queue_status,
            conclusion=queue_conclusion,
        ),
    }


@asynccontextmanager
async def _fake_github_client(**_kwargs: Any):
    yield object()


async def _coro(value: Any) -> Any:
    return value


def _patch_auto_merge(
    monkeypatch: pytest.MonkeyPatch,
    threads: _FakeThreads,
    *,
    pr: dict[str, Any] | None = None,
    checks: dict[str, dict[str, Any]] | Exception | None = None,
) -> list[str]:
    _patch(monkeypatch, _FakeClient(threads, _FakeRuns({})))
    monkeypatch.setattr(reconcile, "github_client", _fake_github_client)
    monkeypatch.setattr(
        reconcile, "get_github_app_installation_token", lambda **_kw: _coro("token")
    )
    queries: list[str] = []

    async def fake_graphql(_client: Any, query: str, _variables: dict[str, Any]):
        queries.append(query)
        return pr or _pr_data()

    async def fake_checks(*_args: Any, **_kwargs: Any) -> dict[str, dict[str, Any]]:
        if isinstance(checks, Exception):
            raise checks
        return checks or _checks()

    monkeypatch.setattr(reconcile, "_graphql", fake_graphql)
    monkeypatch.setattr(reconcile, "_mergify_checks", fake_checks)
    return queries


def _last_phase(threads: _FakeThreads) -> str:
    await_args = threads.update.await_args
    assert await_args is not None
    return await_args.kwargs["metadata"]["auto_merge_phase"]


@pytest.mark.asyncio
async def test_auto_merge_observes_mergify_enqueue_on_exact_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads = _FakeThreads([[_auto_merge_thread()]])
    queries = _patch_auto_merge(monkeypatch, threads)

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["enqueue"] == 1
    assert _last_phase(threads) == "enqueue"
    assert queries == [reconcile._AUTO_MERGE_QUERY]


@pytest.mark.asyncio
async def test_auto_merge_awaits_mergify_checks_on_exact_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads = _FakeThreads([[_auto_merge_thread()]])
    _patch_auto_merge(
        monkeypatch,
        threads,
        checks=_checks(protections_status="in_progress", protections_conclusion=None),
    )

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["awaiting_checks"] == 1
    assert _last_phase(threads) == "awaiting_checks"


@pytest.mark.asyncio
async def test_auto_merge_observes_mergify_queue_on_exact_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads = _FakeThreads([[_auto_merge_thread()]])
    _patch_auto_merge(
        monkeypatch,
        threads,
        checks=_checks(queue_status="in_progress", queue_conclusion=None),
    )

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["queued"] == 1
    assert _last_phase(threads) == "queued"


@pytest.mark.asyncio
async def test_auto_merge_alerts_when_queue_dwell_exceeds_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phase_since = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    threads = _FakeThreads(
        [
            [
                _auto_merge_thread(
                    auto_merge_phase="queued",
                    auto_merge_phase_since=phase_since,
                    auto_merge_head_sha="abc123",
                )
            ]
        ]
    )
    _patch_auto_merge(
        monkeypatch,
        threads,
        checks=_checks(queue_status="in_progress", queue_conclusion=None),
    )

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["queue_stalled"] == 1
    await_args = threads.update.await_args
    assert await_args is not None
    written = await_args.kwargs["metadata"]
    assert written["auto_merge_phase_since"] == phase_since
    assert written["auto_merge_alert_reason"] == "queue_stall_in_queue"
    assert written["auto_merge_alert_at"]


@pytest.mark.asyncio
async def test_auto_merge_does_not_alert_for_recent_queue_dwell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phase_since = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    threads = _FakeThreads(
        [
            [
                _auto_merge_thread(
                    auto_merge_phase="queued",
                    auto_merge_phase_since=phase_since,
                    auto_merge_head_sha="abc123",
                )
            ]
        ]
    )
    _patch_auto_merge(
        monkeypatch,
        threads,
        checks=_checks(queue_status="in_progress", queue_conclusion=None),
    )

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["queue_stalled"] == 0
    await_args = threads.update.await_args
    assert await_args is not None
    written = await_args.kwargs["metadata"]
    assert written["auto_merge_phase_since"] == phase_since
    assert "auto_merge_alert_reason" not in written
    assert "auto_merge_alert_at" not in written


@pytest.mark.asyncio
@pytest.mark.parametrize("seeded_alert_reason", ["", "queue_stall_in_queue"])
async def test_auto_merge_resets_queue_dwell_when_head_changes(
    monkeypatch: pytest.MonkeyPatch,
    seeded_alert_reason: str,
) -> None:
    old_phase_since = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    thread_metadata: dict[str, Any] = {
        "auto_merge_phase": "queued",
        "auto_merge_phase_since": old_phase_since,
        "auto_merge_head_sha": "def456",
    }
    if seeded_alert_reason:
        thread_metadata["auto_merge_alert_reason"] = seeded_alert_reason
    threads = _FakeThreads([[_auto_merge_thread(**thread_metadata)]])
    _patch_auto_merge(
        monkeypatch,
        threads,
        checks=_checks(queue_status="in_progress", queue_conclusion=None),
    )

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["queue_stalled"] == 0
    await_args = threads.update.await_args
    assert await_args is not None
    written = await_args.kwargs["metadata"]
    if seeded_alert_reason:
        assert written["auto_merge_alert_reason"] == ""
    else:
        assert "auto_merge_alert_reason" not in written
    assert "auto_merge_alert_at" not in written
    assert written["auto_merge_phase_since"] == written["auto_merge_phase_at"]
    assert written["auto_merge_phase_since"] != old_phase_since
    assert written["auto_merge_head_sha"] == "abc123"


@pytest.mark.asyncio
async def test_auto_merge_starts_queue_dwell_on_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_phase_since = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    threads = _FakeThreads(
        [
            [
                _auto_merge_thread(
                    auto_merge_phase="awaiting_checks",
                    auto_merge_phase_since=old_phase_since,
                )
            ]
        ]
    )
    _patch_auto_merge(
        monkeypatch,
        threads,
        checks=_checks(queue_status="in_progress", queue_conclusion=None),
    )

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["queue_stalled"] == 0
    await_args = threads.update.await_args
    assert await_args is not None
    written = await_args.kwargs["metadata"]
    assert written["auto_merge_phase_since"] == written["auto_merge_phase_at"]
    assert written["auto_merge_phase_since"] != old_phase_since
    assert "auto_merge_alert_reason" not in written
    assert "auto_merge_alert_at" not in written


@pytest.mark.asyncio
async def test_auto_merge_repairs_missing_queue_phase_since_without_alerting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads = _FakeThreads([[_auto_merge_thread(auto_merge_phase="queued")]])
    _patch_auto_merge(
        monkeypatch,
        threads,
        checks=_checks(queue_status="in_progress", queue_conclusion=None),
    )

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["queue_stalled"] == 0
    await_args = threads.update.await_args
    assert await_args is not None
    written = await_args.kwargs["metadata"]
    assert written["auto_merge_phase_since"] == written["auto_merge_phase_at"]
    assert "auto_merge_alert_reason" not in written


@pytest.mark.asyncio
async def test_auto_merge_clears_queue_alert_when_phase_changes_to_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads = _FakeThreads(
        [
            [
                _auto_merge_thread(
                    auto_merge_phase="queued",
                    auto_merge_phase_since=(datetime.now(UTC) - timedelta(minutes=20)).isoformat(),
                    auto_merge_alert_reason="queue_stall_in_queue",
                )
            ]
        ]
    )
    _patch_auto_merge(monkeypatch, threads)

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["enqueue"] == 1
    await_args = threads.update.await_args
    assert await_args is not None
    assert await_args.kwargs["metadata"]["auto_merge_alert_reason"] == ""


@pytest.mark.asyncio
async def test_auto_merge_omits_queue_alert_clear_when_no_alert_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads = _FakeThreads([[_auto_merge_thread(auto_merge_phase="queued")]])
    _patch_auto_merge(monkeypatch, threads)

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["enqueue"] == 1
    await_args = threads.update.await_args
    assert await_args is not None
    assert "auto_merge_alert_reason" not in await_args.kwargs["metadata"]


@pytest.mark.asyncio
async def test_auto_merge_queue_stall_threshold_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_MERGE_QUEUE_STALL_MINUTES", "1")
    threads = _FakeThreads(
        [
            [
                _auto_merge_thread(
                    auto_merge_phase="queued",
                    auto_merge_phase_since=(datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
                    auto_merge_head_sha="abc123",
                )
            ]
        ]
    )
    _patch_auto_merge(
        monkeypatch,
        threads,
        checks=_checks(queue_status="in_progress", queue_conclusion=None),
    )

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["queue_stalled"] == 1
    await_args = threads.update.await_args
    assert await_args is not None
    assert await_args.kwargs["metadata"]["auto_merge_alert_reason"] == "queue_stall_in_queue"


@pytest.mark.asyncio
async def test_auto_merge_queue_stall_threshold_invalid_env_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTO_MERGE_QUEUE_STALL_MINUTES", "not-a-number")
    threads = _FakeThreads(
        [
            [
                _auto_merge_thread(
                    auto_merge_phase="queued",
                    auto_merge_phase_since=(datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
                    auto_merge_head_sha="abc123",
                )
            ]
        ]
    )
    _patch_auto_merge(
        monkeypatch,
        threads,
        checks=_checks(queue_status="in_progress", queue_conclusion=None),
    )

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["queue_stalled"] == 0
    await_args = threads.update.await_args
    assert await_args is not None
    assert "auto_merge_alert_reason" not in await_args.kwargs["metadata"]


@pytest.mark.asyncio
async def test_auto_merge_records_merged_pr_without_reading_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads = _FakeThreads(
        [
            [
                _auto_merge_thread(
                    auto_merge_phase="queued",
                    auto_merge_alert_reason="queue_stall_in_queue",
                )
            ]
        ]
    )
    checks = AsyncMock()
    _patch_auto_merge(monkeypatch, threads, pr=_pr_data(state="MERGED"))
    monkeypatch.setattr(reconcile, "_mergify_checks", checks)

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["merged"] == 1
    assert _last_phase(threads) == "merged"
    await_args = threads.update.await_args
    assert await_args is not None
    assert await_args.kwargs["metadata"]["auto_merge_reconcile"] is False
    assert await_args.kwargs["metadata"]["auto_merge_alert_reason"] == ""
    checks.assert_not_awaited()


@pytest.mark.parametrize(
    "checks",
    [RuntimeError("Mergify unavailable"), _checks(head_sha="old-head")],
    ids=["outage", "stale-head"],
)
@pytest.mark.asyncio
async def test_auto_merge_hold_returns_exact_head_pr_to_draft_before_mergify_state(
    monkeypatch: pytest.MonkeyPatch,
    checks: dict[str, dict[str, Any]] | Exception,
) -> None:
    threads = _FakeThreads([[_auto_merge_thread(merge_hold_requested=True)]])
    queries = _patch_auto_merge(monkeypatch, threads, checks=checks)

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["held"] == 1
    assert counts["held_drafted"] == 1
    assert counts["backend_unavailable"] == 0
    assert counts["stale_head"] == 0
    assert _last_phase(threads) == "held"
    assert queries == [reconcile._AUTO_MERGE_QUERY, reconcile._CONVERT_TO_DRAFT]


@pytest.mark.asyncio
async def test_auto_merge_stale_mergify_head_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads = _FakeThreads([[_auto_merge_thread()]])
    queries = _patch_auto_merge(monkeypatch, threads, checks=_checks(head_sha="old-head"))

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["stale_head"] == 1
    assert _last_phase(threads) == "stale_head"
    assert queries == [reconcile._AUTO_MERGE_QUERY]


@pytest.mark.asyncio
async def test_auto_merge_mergify_outage_fails_closed_and_recovers_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads = _FakeThreads([[_auto_merge_thread()]])
    queries = _patch_auto_merge(monkeypatch, threads, checks=RuntimeError("Mergify unavailable"))

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["backend_unavailable"] == 1
    assert _last_phase(threads) == "backend_unavailable"
    assert queries == [reconcile._AUTO_MERGE_QUERY]
    await_args = threads.update.await_args
    assert await_args is not None
    assert "auto_merge_reconcile" not in await_args.kwargs["metadata"]


@pytest.mark.asyncio
async def test_mergify_checks_require_mergify_app_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "check_runs": [
                    _check(reconcile._MERGIFY_PROTECTIONS_CHECK, app="other-app"),
                    _check(reconcile._MERGIFY_QUEUE_CHECK),
                ]
            }

    monkeypatch.setattr(reconcile, "github_request", lambda *_a, **_kw: _coro(Response()))

    client: Any = object()
    checks = await reconcile._mergify_checks(client, "acme", "widget", "abc123")

    assert reconcile._MERGIFY_PROTECTIONS_CHECK not in checks
    assert reconcile._mergify_state(checks, "abc123") == "backend_unavailable"


def test_auto_merge_reconciler_has_no_native_queue_state_or_mutations() -> None:
    source = reconcile._AUTO_MERGE_QUERY + reconcile._CONVERT_TO_DRAFT

    assert "isInMergeQueue" not in source
    assert "enablePullRequestAutoMerge" not in source
    assert "disablePullRequestAutoMerge" not in source
    assert "dequeuePullRequest" not in source


@pytest.mark.asyncio
async def test_scheduler_reconcile_runs_both_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = AsyncMock(return_value={"cancelled": 1})
    auto_merge = AsyncMock(return_value={"queued": 1})
    monkeypatch.setattr(scheduler, "reconcile_stale_runs", stale)
    monkeypatch.setattr(scheduler, "reconcile_auto_merge_prs", auto_merge)

    result = await scheduler._launch({"task": "reconcile"}, {})

    assert result == {
        "result": {
            "stale_runs": {"cancelled": 1},
            "auto_merge": {"queued": 1},
        }
    }
    stale.assert_awaited_once()
    auto_merge.assert_awaited_once()
