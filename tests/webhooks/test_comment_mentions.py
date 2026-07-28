"Tests for code-aware webhook mention classification."

import pytest

from agent.utils.comment_mentions import (
    classify_comment_mention,
    extract_adjacent_repo_directive,
)


@pytest.mark.parametrize(
    ("text", "disposition"),
    [
        ("@openswe continue", "accepted"),
        ("Context\n@OpenSWE continue", "accepted"),
        ("Please ask @openswe to continue", "mid_line"),
        ("Discuss `@openswe` here", "inline_code"),
        ("Discuss ``@openswe`` here", "inline_code"),
        ("Discuss `\n@openswe\n` here", "inline_code"),
        ("\\`\n@openswe continue\n`", "accepted"),
        ("```markdown\n@openswe continue\n```", "fenced_code"),
        ("``` lang`x\n@openswe continue\n```", "accepted"),
        ("~~~text\n@openswe continue\n~~~", "fenced_code"),
        ("```markdown\n@openswe continue", "fenced_code"),
        ("No agent mention", "no_mention"),
    ],
)
def test_classify_comment_mention(text: str, disposition: str) -> None:
    result = classify_comment_mention(text, ("@openswe",))

    assert result.disposition == disposition


def test_valid_mention_after_code_example_is_accepted() -> None:
    result = classify_comment_mention("Example: `@openswe`\n@openswe continue", ("@openswe",))

    assert result.disposition == "accepted"
    assert result.start == len("Example: `@openswe`\n")


@pytest.mark.parametrize(
    "alias",
    ["@openswe", "@open-swe", "@openswe-dev", "@OpEnSwE"],
)
def test_github_aliases_are_preserved(alias: str) -> None:
    result = classify_comment_mention(
        f"{alias} continue", ("@openswe", "@open-swe", "@openswe-dev")
    )

    assert result.disposition == "accepted"


def test_alias_prefix_is_not_a_mention() -> None:
    result = classify_comment_mention("@openswe-helper continue", ("@openswe",))

    assert result.disposition == "no_mention"


@pytest.mark.parametrize(
    ("text", "repo"),
    [
        ("@openswe repo owner/name — Execute TEST-1 only.", {"owner": "owner", "name": "name"}),
        ("@openswe repo:owner/name — Execute TEST-1 only.", {"owner": "owner", "name": "name"}),
        ("@openswe continue in repo owner/name", None),
        ("@openswe Plan approved.\nExample: repo owner/name", None),
        ("@openswe repo name", None),
        ("@openswe repo owner/name/extra", None),
    ],
)
def test_extract_adjacent_repo_directive(text: str, repo: dict[str, str] | None) -> None:
    mention = classify_comment_mention(text, ("@openswe",))

    assert extract_adjacent_repo_directive(text, mention) == repo
