from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent.utils import github_app


@pytest.fixture(autouse=True)
def _clear_app_token_cache() -> Any:
    github_app.clear_app_token_cache()
    yield
    github_app.clear_app_token_cache()


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    def __init__(
        self,
        calls: list[tuple[str, str, dict[str, Any]]],
        installations: dict[str, str],
        failing_gets: frozenset[str],
    ) -> None:
        self._calls = calls
        self._installations = installations
        self._failing_gets = failing_gets

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self._calls.append(("GET", url, kwargs))
        if url in self._failing_gets:
            raise RuntimeError("installation lookup failed")
        if url not in self._installations:
            raise AssertionError(f"Unexpected installation lookup: {url}")
        return _FakeResponse({"id": self._installations[url]})

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self._calls.append(("POST", url, kwargs))
        installation_id = url.split("/installations/", 1)[1].split("/", 1)[0]
        expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        return _FakeResponse({"token": f"token-{installation_id}", "expires_at": expires_at})


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    installations: dict[str, str],
    *,
    failing_gets: frozenset[str] = frozenset(),
) -> list[tuple[str, str, dict[str, Any]]]:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def factory(*_args: object, **_kwargs: object) -> _FakeAsyncClient:
        return _FakeAsyncClient(calls, installations, failing_gets)

    monkeypatch.setattr(github_app, "GITHUB_APP_ID", "1")
    monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY", "key")
    monkeypatch.setattr(github_app, "GITHUB_APP_INSTALLATION_ID", "123")
    monkeypatch.setattr(github_app, "_generate_app_jwt", lambda: "jwt")
    monkeypatch.setattr(github_app.httpx, "AsyncClient", factory)
    monkeypatch.delenv(github_app.GITHUB_APP_TARGET_REPO_ENV, raising=False)
    return calls


@pytest.mark.asyncio
async def test_resolves_org_installation_before_minting_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_url = "https://api.github.com/orgs/some-org/installation"
    calls = _configure(monkeypatch, {org_url: "999"})

    token = await github_app.get_github_app_installation_token(
        target_org="some-org", permissions={"members": "read"}
    )

    assert token == "token-999"
    assert [(method, url) for method, url, _ in calls] == [
        ("GET", org_url),
        ("POST", "https://api.github.com/app/installations/999/access_tokens"),
    ]
    assert calls[1][2]["json"] == {"permissions": {"members": "read"}}


@pytest.mark.asyncio
async def test_caches_org_installation_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    org_url = "https://api.github.com/orgs/some-org/installation"
    calls = _configure(monkeypatch, {org_url: "999"})

    first = await github_app.get_github_app_installation_token(target_org="some-org")
    second = await github_app.get_github_app_installation_token(target_org="some-org")

    assert first == second == "token-999"
    assert sum(method == "GET" and url == org_url for method, url, _ in calls) == 1


@pytest.mark.asyncio
async def test_org_resolution_failure_falls_back_to_static_installation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    org_url = "https://api.github.com/orgs/some-org/installation"
    calls = _configure(monkeypatch, {}, failing_gets=frozenset({org_url}))

    with caplog.at_level(logging.WARNING, logger=github_app.__name__):
        token = await github_app.get_github_app_installation_token(target_org="some-org")

    assert token == "token-123"
    assert any(
        record.levelno == logging.WARNING and "some-org" in record.getMessage()
        for record in caplog.records
    )
    assert any(
        method == "POST" and url == "https://api.github.com/app/installations/123/access_tokens"
        for method, url, _ in calls
    )


@pytest.mark.asyncio
async def test_explicit_repo_precedes_org_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    repo_url = "https://api.github.com/repos/owner/repo/installation"
    calls = _configure(monkeypatch, {repo_url: "456"})

    token = await github_app.get_github_app_installation_token(
        target_repo="owner/repo", target_org="some-org"
    )

    assert token == "token-456"
    assert [(method, url) for method, url, _ in calls] == [
        ("GET", repo_url),
        ("POST", "https://api.github.com/app/installations/456/access_tokens"),
    ]
