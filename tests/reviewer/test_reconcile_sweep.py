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
        "auto_merge_recovery_attempted": False,
    }
    base.update(metadata)
    return {"thread_id": "agent-thread", "metadata": base}


def _pr_data(**overrides: Any) -> dict[str, Any]:
    pr = {
        "id": "PR_1",
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "main",
        "headRefName": "open-swe/change",
        "headRefOid": "abc123",
        "mergeStateStatus": "CLEAN",
        "isInMergeQueue": False,
        "autoMergeRequest": {"enabledAt": "now"},
        "labels": {"nodes": []},
        "statusCheckRollup": {"state": "SUCCESS"},
    }
    pr.update(overrides)
    return {"repository": {"defaultBranchRef": {"name": "main"}, "pullRequest": pr}}


@asynccontextmanager
async def _fake_github_client(**_kwargs: Any):
    yield object()


@pytest.mark.asyncio
async def test_auto_merge_stall_cycles_disable_then_rearm_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    threads = _FakeThreads(
        [
            [
                _auto_merge_thread(
                    auto_merge_phase="green",
                    auto_merge_phase_at=old,
                    auto_merge_head_sha="abc123",
                )
            ]
        ]
    )
    client = _FakeClient(threads, _FakeRuns({}))
    _patch(monkeypatch, client)
    monkeypatch.setattr(reconcile, "github_client", _fake_github_client)
    monkeypatch.setattr(
        reconcile, "get_github_app_installation_token", lambda **_kw: _coro("token")
    )
    queries: list[str] = []

    async def fake_graphql(_client: Any, query: str, _variables: dict[str, Any]):
        queries.append(query)
        return _pr_data() if len(queries) == 1 else {}

    monkeypatch.setattr(reconcile, "_graphql", fake_graphql)

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["rearmed"] == 1
    assert queries[1] == reconcile._DISABLE_AUTO_MERGE
    assert queries[2] == reconcile._ENABLE_AUTO_MERGE
    updates = [call.kwargs["metadata"] for call in threads.update.await_args_list]
    assert updates[0]["auto_merge_recovery_attempted"] is True


@pytest.mark.asyncio
async def test_auto_merge_green_draft_alerts_without_readying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads = _FakeThreads([[_auto_merge_thread()]])
    client = _FakeClient(threads, _FakeRuns({}))
    _patch(monkeypatch, client)
    monkeypatch.setattr(reconcile, "github_client", _fake_github_client)
    monkeypatch.setattr(
        reconcile, "get_github_app_installation_token", lambda **_kw: _coro("token")
    )
    monkeypatch.setattr(
        reconcile,
        "_graphql",
        lambda *_a, **_kw: _coro(_pr_data(isDraft=True, autoMergeRequest=None)),
    )
    alert = AsyncMock(return_value=None)
    monkeypatch.setattr(reconcile, "_post_alert", alert)

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["alerted"] == 1
    alert.assert_awaited_once()
    assert threads.update.await_args is not None
    assert threads.update.await_args.kwargs["metadata"]["auto_merge_reconcile"] is False


@pytest.mark.asyncio
async def test_auto_merge_hold_disables_and_never_rearms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads = _FakeThreads([[_auto_merge_thread()]])
    _patch(monkeypatch, _FakeClient(threads, _FakeRuns({})))
    monkeypatch.setattr(reconcile, "github_client", _fake_github_client)
    monkeypatch.setattr(
        reconcile, "get_github_app_installation_token", lambda **_kw: _coro("token")
    )
    queries: list[str] = []

    async def fake_graphql(_client: Any, query: str, _variables: dict[str, Any]):
        queries.append(query)
        return _pr_data(labels={"nodes": [{"name": "hold-merge"}]}) if len(queries) == 1 else {}

    monkeypatch.setattr(reconcile, "_graphql", fake_graphql)

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["held_disabled"] == 1
    assert queries == [reconcile._AUTO_MERGE_QUERY, reconcile._DISABLE_AUTO_MERGE]


async def _coro(value: Any) -> Any:
    return value


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


@pytest.mark.asyncio
async def test_auto_merge_persisted_hold_disables_without_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads = _FakeThreads([[_auto_merge_thread(merge_hold_requested=True)]])
    _patch(monkeypatch, _FakeClient(threads, _FakeRuns({})))
    monkeypatch.setattr(reconcile, "github_client", _fake_github_client)
    monkeypatch.setattr(
        reconcile, "get_github_app_installation_token", lambda **_kw: _coro("token")
    )
    queries: list[str] = []

    async def fake_graphql(_client: Any, query: str, _variables: dict[str, Any]):
        queries.append(query)
        return _pr_data() if len(queries) == 1 else {}

    monkeypatch.setattr(reconcile, "_graphql", fake_graphql)

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["held_disabled"] == 1
    assert queries == [reconcile._AUTO_MERGE_QUERY, reconcile._DISABLE_AUTO_MERGE]


@pytest.mark.asyncio
async def test_auto_merge_failed_rearm_becomes_alertable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    threads = _FakeThreads(
        [
            [
                _auto_merge_thread(
                    auto_merge_phase="recovery",
                    auto_merge_phase_at=old,
                    auto_merge_head_sha="abc123",
                    auto_merge_recovery_attempted=True,
                )
            ]
        ]
    )
    _patch(monkeypatch, _FakeClient(threads, _FakeRuns({})))
    monkeypatch.setattr(reconcile, "github_client", _fake_github_client)
    monkeypatch.setattr(
        reconcile, "get_github_app_installation_token", lambda **_kw: _coro("token")
    )
    monkeypatch.setattr(
        reconcile,
        "_graphql",
        lambda *_a, **_kw: _coro(_pr_data(autoMergeRequest=None)),
    )
    alert = AsyncMock(return_value=None)
    monkeypatch.setattr(reconcile, "_post_alert", alert)

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["alerted"] == 1
    assert counts["armed"] == 0
    alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_merge_hold_dequeues_queued_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads = _FakeThreads([[_auto_merge_thread(merge_hold_requested=True)]])
    _patch(monkeypatch, _FakeClient(threads, _FakeRuns({})))
    monkeypatch.setattr(reconcile, "github_client", _fake_github_client)
    monkeypatch.setattr(
        reconcile, "get_github_app_installation_token", lambda **_kw: _coro("token")
    )
    queries: list[str] = []

    async def fake_graphql(_client: Any, query: str, _variables: dict[str, Any]):
        queries.append(query)
        return _pr_data(isInMergeQueue=True) if len(queries) == 1 else {}

    monkeypatch.setattr(reconcile, "_graphql", fake_graphql)

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["held_dequeued"] == 1
    assert counts["held_disabled"] == 1
    assert queries == [
        reconcile._AUTO_MERGE_QUERY,
        reconcile._DEQUEUE_PULL_REQUEST,
        reconcile._DISABLE_AUTO_MERGE,
    ]


@pytest.mark.asyncio
async def test_auto_merge_queue_entry_keeps_reconciliation_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads = _FakeThreads([[_auto_merge_thread()]])
    _patch(monkeypatch, _FakeClient(threads, _FakeRuns({})))
    monkeypatch.setattr(reconcile, "github_client", _fake_github_client)
    monkeypatch.setattr(
        reconcile, "get_github_app_installation_token", lambda **_kw: _coro("token")
    )
    monkeypatch.setattr(
        reconcile,
        "_graphql",
        lambda *_a, **_kw: _coro(_pr_data(isInMergeQueue=True)),
    )

    counts = await reconcile.reconcile_auto_merge_prs()

    assert counts["queued"] == 1
    assert threads.update.await_args is not None
    assert threads.update.await_args.kwargs["metadata"]["auto_merge_reconcile"] is True
