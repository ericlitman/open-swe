"""Bot-identity matching for self-authored GitHub events.

Every deployment runs under its own GitHub App, so the bot login is a
deployment fact. When it is not recognised, the reviewer treats its own
comments as a third party's and re-triggers itself.
"""

import pytest

from agent.utils.github_org_membership import is_internal_bot_login


@pytest.mark.parametrize(
    "login",
    ["open-swe[bot]", "openswe-dev[bot]", "open-swe", "OPEN-SWE[BOT]", " open-swe[bot] "],
)
def test_default_identities_match(monkeypatch: pytest.MonkeyPatch, login: str) -> None:
    monkeypatch.delenv("GITHUB_BOT_LOGINS", raising=False)
    assert is_internal_bot_login(login) is True


def test_deployment_login_matches_only_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_BOT_LOGINS", raising=False)
    assert is_internal_bot_login("openswebot[bot]") is False

    monkeypatch.setenv("GITHUB_BOT_LOGINS", "openswebot[bot]")
    assert is_internal_bot_login("openswebot[bot]") is True
    # GraphQL reports the bare slug where REST reports the suffixed login.
    assert is_internal_bot_login("openswebot") is True
    assert is_internal_bot_login("OpenSWEBot[bot]") is True


def test_multiple_and_malformed_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_BOT_LOGINS", " openswebot[bot] , ,mast-factory[bot], ")
    assert is_internal_bot_login("openswebot[bot]") is True
    assert is_internal_bot_login("mast-factory[bot]") is True
    assert is_internal_bot_login("") is False
    assert is_internal_bot_login(None) is False


def test_humans_and_third_party_bots_are_not_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_BOT_LOGINS", "openswebot[bot]")
    for login in ("octocat", "dependabot[bot]", "openswebot-staging[bot]"):
        assert is_internal_bot_login(login) is False
