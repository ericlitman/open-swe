from __future__ import annotations

from agent.webhooks import common


def test_parse_repo_aliases_parses_pairs_and_skips_malformed() -> None:
    aliases = common._parse_repo_aliases(
        "ericlitman/mastra-pilot=mobilyze-llc/mastra-pilot, bad-entry,"
        " old/name=new/name, missing-equals/x,"
        " extra/segments=new/name/extra, deep/old/path=new/name"
    )
    assert aliases == {
        "ericlitman/mastra-pilot": {"owner": "mobilyze-llc", "name": "mastra-pilot"},
        "old/name": {"owner": "new", "name": "name"},
    }


def test_parse_repo_aliases_empty_input() -> None:
    assert common._parse_repo_aliases("") == {}


def test_canonicalize_repo_config_maps_aliased_repo(monkeypatch) -> None:
    monkeypatch.setattr(
        common,
        "GITHUB_REPO_ALIASES",
        {"ericlitman/mastra-pilot": {"owner": "mobilyze-llc", "name": "mastra-pilot"}},
    )

    result = common.canonicalize_repo_config({"owner": "EricLitman", "name": "Mastra-Pilot"})

    assert result == {"owner": "mobilyze-llc", "name": "mastra-pilot"}


def test_canonicalize_repo_config_passes_through_unaliased_repo(monkeypatch) -> None:
    monkeypatch.setattr(
        common,
        "GITHUB_REPO_ALIASES",
        {"ericlitman/mastra-pilot": {"owner": "mobilyze-llc", "name": "mastra-pilot"}},
    )

    config = {"owner": "ericlitman", "name": "threadbear"}

    assert common.canonicalize_repo_config(config) is config
