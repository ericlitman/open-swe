from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.review import publish as reviewer_publish
from agent.utils import github_checks


class _FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        error: bool = False,
        status_code: int = 200,
    ) -> None:
        self._payload = payload or {}
        self._error = error
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        if self._error:
            raise httpx.HTTPStatusError("forbidden", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    last_post: dict[str, Any] | None = None
    last_patch: dict[str, Any] | None = None
    last_get: dict[str, Any] | None = None
    post_response: _FakeResponse = _FakeResponse({"id": 42})
    patch_response: _FakeResponse = _FakeResponse({})
    get_response: _FakeResponse = _FakeResponse({"head": {"sha": "abc123"}})

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        type(self).last_post = {"url": url, **kwargs}
        return type(self).post_response

    async def patch(self, url: str, **kwargs: Any) -> _FakeResponse:
        type(self).last_patch = {"url": url, **kwargs}
        return type(self).patch_response

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        type(self).last_get = {"url": url, **kwargs}
        return type(self).get_response


@pytest.fixture(autouse=True)
def _reset_fake_client() -> None:
    _FakeAsyncClient.last_post = None
    _FakeAsyncClient.last_patch = None
    _FakeAsyncClient.last_get = None
    _FakeAsyncClient.post_response = _FakeResponse({"id": 42})
    _FakeAsyncClient.patch_response = _FakeResponse({})
    _FakeAsyncClient.get_response = _FakeResponse({"head": {"sha": "abc123"}})


async def test_create_review_check_run_posts_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_checks.httpx, "AsyncClient", _FakeAsyncClient)

    check_run_id = await github_checks.create_review_check_run(
        owner="acme",
        repo="widgets",
        head_sha="abc123",
        token="tok",
        details_url="https://example.com/thread",
    )

    assert check_run_id == 42
    assert _FakeAsyncClient.last_post is not None
    assert _FakeAsyncClient.last_post["url"].endswith("/repos/acme/widgets/check-runs")
    body = _FakeAsyncClient.last_post["json"]
    assert body["name"] == github_checks.REVIEW_CHECK_RUN_NAME
    assert body["head_sha"] == "abc123"
    assert body["status"] == "in_progress"
    assert body["details_url"] == "https://example.com/thread"


async def test_create_review_check_run_returns_none_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_checks.httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.post_response = _FakeResponse(error=True)

    check_run_id = await github_checks.create_review_check_run(
        owner="acme", repo="widgets", head_sha="abc123", token="tok"
    )

    assert check_run_id is None


async def test_create_completed_review_check_run_posts_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_checks.httpx, "AsyncClient", _FakeAsyncClient)

    ok = await github_checks.create_completed_review_check_run(
        owner="acme",
        repo="widgets",
        head_sha="abc123",
        token="tok",
        conclusion="failure",
        title="Found 2 potential issues",
        summary="standing findings remain",
    )

    assert ok is True
    assert _FakeAsyncClient.last_post is not None
    body = _FakeAsyncClient.last_post["json"]
    assert body["name"] == github_checks.REVIEW_CHECK_RUN_NAME
    assert body["head_sha"] == "abc123"
    assert body["status"] == "completed"
    assert body["conclusion"] == "failure"
    assert body["output"] == {
        "title": "Found 2 potential issues",
        "summary": "standing findings remain",
    }


async def test_complete_review_check_run_patches_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_checks.httpx, "AsyncClient", _FakeAsyncClient)

    ok = await github_checks.complete_review_check_run(
        owner="acme",
        repo="widgets",
        check_run_id=42,
        token="tok",
        conclusion="neutral",
        title="Found 2 potential issues",
        summary="…",
    )

    assert ok is True
    assert _FakeAsyncClient.last_patch is not None
    assert _FakeAsyncClient.last_patch["url"].endswith("/repos/acme/widgets/check-runs/42")
    body = _FakeAsyncClient.last_patch["json"]
    assert body["status"] == "completed"
    assert body["conclusion"] == "neutral"
    assert body["output"]["title"] == "Found 2 potential issues"


async def test_fetch_pull_request_head_sha_returns_current_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_checks.httpx, "AsyncClient", _FakeAsyncClient)

    head_sha = await github_checks.fetch_pull_request_head_sha(
        owner="acme", repo="widgets", pr_number=77, token="tok"
    )

    assert head_sha == "abc123"
    assert _FakeAsyncClient.last_get is not None
    assert _FakeAsyncClient.last_get["url"].endswith("/repos/acme/widgets/pulls/77")


async def test_post_autofix_status_check_completes_neutral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_checks.httpx, "AsyncClient", _FakeAsyncClient)

    ok = await github_checks.post_autofix_status_check(
        owner="acme",
        repo="widgets",
        head_sha="abc123",
        token="tok",
        title="Auto-fixing 1 failing check(s)",
        summary="working on it",
        details_url="https://example.com/thread",
    )

    assert ok is True
    assert _FakeAsyncClient.last_post is not None
    assert _FakeAsyncClient.last_post["url"].endswith("/repos/acme/widgets/check-runs")
    body = _FakeAsyncClient.last_post["json"]
    assert body["name"] == github_checks.AUTOFIX_CHECK_RUN_NAME
    assert body["status"] == "completed"
    assert body["conclusion"] == "neutral"
    assert body["details_url"] == "https://example.com/thread"


@pytest.mark.parametrize(
    ("flag", "surfaced_count", "expected"),
    [
        (
            None,
            0,
            (
                "success",
                "No issues found",
                "Open SWE reviewed this pull request and found no issues.",
            ),
        ),
        (
            None,
            3,
            (
                "success",
                "Found 3 potential issues",
                "Open SWE surfaced 3 potential issues on this pull request.",
            ),
        ),
        (
            "true",
            0,
            (
                "success",
                "No issues found",
                "Open SWE reviewed this pull request and found no issues.",
            ),
        ),
        (
            "true",
            1,
            (
                "failure",
                "Found 1 potential issue",
                "Open SWE surfaced 1 potential issue on this pull request.",
            ),
        ),
        (
            "TRUE",
            2,
            (
                "failure",
                "Found 2 potential issues",
                "Open SWE surfaced 2 potential issues on this pull request.",
            ),
        ),
        (
            "false",
            2,
            (
                "success",
                "Found 2 potential issues",
                "Open SWE surfaced 2 potential issues on this pull request.",
            ),
        ),
    ],
    ids=[
        "flag-unset-zero-findings-success",
        "flag-unset-three-findings-success",
        "flag-true-zero-findings-success",
        "flag-true-one-finding-failure",
        "flag-uppercase-true-two-findings-failure",
        "flag-false-two-findings-success",
    ],
)
def test_review_check_conclusion_mapping(
    monkeypatch: pytest.MonkeyPatch,
    flag: str | None,
    surfaced_count: int,
    expected: tuple[str, str, str],
) -> None:
    if flag is None:
        monkeypatch.delenv("REVIEW_CHECK_BLOCKING", raising=False)
    else:
        monkeypatch.setenv("REVIEW_CHECK_BLOCKING", flag)

    assert github_checks.review_check_conclusion(surfaced_count) == expected


async def test_settle_review_check_run_noop_without_tracked_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_thread_metadata(thread_id: str) -> dict[str, Any]:
        return {}

    completed: list[dict[str, Any]] = []

    async def fake_complete(**kwargs: Any) -> bool:
        completed.append(kwargs)
        return True

    monkeypatch.setattr(reviewer_publish, "get_thread_metadata", fake_get_thread_metadata)
    monkeypatch.setattr(reviewer_publish, "complete_review_check_run", fake_complete)

    await reviewer_publish.settle_review_check_run(
        thread_id="t1",
        owner="acme",
        repo="widgets",
        token="tok",
        conclusion="success",
        title="t",
        summary="s",
    )

    assert completed == []


@pytest.mark.parametrize("blocking", [False, True])
async def test_settle_review_check_run_publish_path_creates_only_when_blocking(
    monkeypatch: pytest.MonkeyPatch,
    blocking: bool,
) -> None:
    monkeypatch.setenv("REVIEW_CHECK_BLOCKING", "true" if blocking else "false")
    monkeypatch.setattr(reviewer_publish, "get_thread_metadata", AsyncMock(return_value={}))
    create = AsyncMock(return_value=True)
    monkeypatch.setattr(reviewer_publish, "create_completed_review_check_run", create)

    await reviewer_publish.settle_review_check_run(
        thread_id="t1",
        owner="acme",
        repo="widgets",
        token="tok",
        conclusion="success",
        title="No issues found",
        summary="standing ledger is clear",
        head_sha="abc123",
        create_if_missing=True,
    )

    if blocking:
        create.assert_awaited_once_with(
            owner="acme",
            repo="widgets",
            head_sha="abc123",
            token="tok",
            conclusion="success",
            title="No issues found",
            summary="standing ledger is clear",
        )
    else:
        create.assert_not_awaited()


async def test_settle_review_check_run_surfaces_replacement_creation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_CHECK_BLOCKING", "true")
    monkeypatch.setattr(reviewer_publish, "get_thread_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(
        reviewer_publish,
        "create_completed_review_check_run",
        AsyncMock(return_value=False),
    )

    with pytest.raises(RuntimeError, match="Failed to create completed review check"):
        await reviewer_publish.settle_review_check_run(
            thread_id="t1",
            owner="acme",
            repo="widgets",
            token="tok",
            conclusion="success",
            title="No issues found",
            summary="standing ledger is clear",
            head_sha="abc123",
            create_if_missing=True,
        )


async def test_settle_review_check_run_completes_and_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_thread_metadata(thread_id: str) -> dict[str, Any]:
        return {"review_check_run_id": 42}

    completed: list[dict[str, Any]] = []
    metadata_writes: list[dict[str, Any]] = []

    async def fake_complete(**kwargs: Any) -> bool:
        completed.append(kwargs)
        return True

    async def fake_set_metadata(thread_id: str, **kwargs: Any) -> None:
        metadata_writes.append({"thread_id": thread_id, **kwargs})

    monkeypatch.setattr(reviewer_publish, "get_thread_metadata", fake_get_thread_metadata)
    monkeypatch.setattr(reviewer_publish, "complete_review_check_run", fake_complete)
    monkeypatch.setattr(reviewer_publish, "set_reviewer_thread_metadata", fake_set_metadata)

    await reviewer_publish.settle_review_check_run(
        thread_id="t1",
        owner="acme",
        repo="widgets",
        token="tok",
        conclusion="neutral",
        title="t",
        summary="s",
    )

    assert len(completed) == 1
    assert completed[0]["check_run_id"] == 42
    assert completed[0]["conclusion"] == "neutral"
    assert metadata_writes == [
        {
            "thread_id": "t1",
            "extra": {
                "review_check_run_id": None,
                "review_check_pending_result": None,
                "review_check_deferred_result": None,
            },
        }
    ]


async def test_settle_review_check_run_keeps_id_on_patch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_thread_metadata(thread_id: str) -> dict[str, Any]:
        return {"review_check_run_id": 42}

    metadata_writes: list[dict[str, Any]] = []

    async def fake_complete(**kwargs: Any) -> bool:
        return False

    async def fake_set_metadata(thread_id: str, **kwargs: Any) -> None:
        metadata_writes.append({"thread_id": thread_id, **kwargs})

    monkeypatch.setattr(reviewer_publish, "get_thread_metadata", fake_get_thread_metadata)
    monkeypatch.setattr(reviewer_publish, "complete_review_check_run", fake_complete)
    monkeypatch.setattr(reviewer_publish, "set_reviewer_thread_metadata", fake_set_metadata)

    await reviewer_publish.settle_review_check_run(
        thread_id="t1",
        owner="acme",
        repo="widgets",
        token="tok",
        conclusion="success",
        title="t",
        summary="s",
    )

    assert metadata_writes == [
        {
            "thread_id": "t1",
            "extra": {
                "review_check_pending_result": {
                    "conclusion": "success",
                    "title": "t",
                    "summary": "s",
                }
            },
        }
    ]


async def test_expected_check_settlement_targets_held_check_without_clearing_current_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reviewer_publish,
        "get_thread_metadata",
        AsyncMock(return_value={"review_check_run_id": 42}),
    )
    complete = AsyncMock(return_value=True)
    set_metadata = AsyncMock()
    monkeypatch.setattr(reviewer_publish, "complete_review_check_run", complete)
    monkeypatch.setattr(reviewer_publish, "set_reviewer_thread_metadata", set_metadata)

    await reviewer_publish.settle_review_check_run(
        thread_id="t1",
        owner="acme",
        repo="widgets",
        token="tok",
        conclusion="success",
        title="No issues found",
        summary="done",
        expected_check_run_id=42,
    )

    complete.assert_awaited_once_with(
        owner="acme",
        repo="widgets",
        check_run_id=42,
        token="tok",
        conclusion="success",
        title="No issues found",
        summary="done",
    )
    set_metadata.assert_awaited_once_with(
        "t1",
        extra={
            "review_check_pending_result": None,
            "review_check_deferred_result": None,
        },
    )


async def test_expected_check_settlement_skips_replacement_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reviewer_publish,
        "get_thread_metadata",
        AsyncMock(return_value={"review_check_run_id": 99}),
    )
    complete = AsyncMock(return_value=True)
    set_metadata = AsyncMock()
    monkeypatch.setattr(reviewer_publish, "complete_review_check_run", complete)
    monkeypatch.setattr(reviewer_publish, "set_reviewer_thread_metadata", set_metadata)

    await reviewer_publish.settle_review_check_run(
        thread_id="t1",
        owner="acme",
        repo="widgets",
        token="tok",
        conclusion="success",
        title="No issues found",
        summary="done",
        expected_check_run_id=42,
    )

    complete.assert_not_awaited()
    set_metadata.assert_not_awaited()
