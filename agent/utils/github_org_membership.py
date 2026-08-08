"""GitHub organization membership checks for webhook gating."""

from __future__ import annotations

import logging
import os

import httpx

from .github_app import get_github_app_installation_token

logger = logging.getLogger(__name__)

_DEFAULT_INTERNAL_BOT_LOGINS = frozenset({"open-swe[bot]", "openswe-dev[bot]"})


def _parse_bot_logins(raw: str) -> frozenset[str]:
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def _configured_bot_logins() -> frozenset[str]:
    """Return every login we post as, defaults plus ``GITHUB_BOT_LOGINS``.

    Every deployment runs under its own GitHub App, so the bot login is a
    deployment fact rather than a constant — this install posts as
    ``openswebot[bot]``, which matched none of the hard-coded names. A login
    missing here makes the bot's own events look like a third party's: the
    reviewer answers its own finding replies, each answer re-triggers the
    reviewer, and the resulting run interrupts the review still holding the
    check. Read from the env on each call so the value is never baked in.
    """
    return _DEFAULT_INTERNAL_BOT_LOGINS | _parse_bot_logins(os.environ.get("GITHUB_BOT_LOGINS", ""))


INTERNAL_BOT_LOGINS: frozenset[str] = _configured_bot_logins()


def is_internal_bot_login(login: str | None) -> bool:
    """Return whether a REST ``sender.login`` is one of our own bot identities.

    Deliberately exact (case-insensitive only): REST payloads always carry the
    suffixed ``name[bot]`` login for Apps and the bare login for users, and
    GitHub lets both exist independently — ``open-swe`` is a User account while
    ``open-swe[bot]`` is our App. Accepting the bare slug here would hand that
    unrelated human every exemption this predicate grants, including the
    ``PUBLIC_REPO_ORG_GATE`` bypass. Use :func:`is_internal_bot_author` for
    GraphQL authors, which report the slug instead.
    """
    if not isinstance(login, str) or not login:
        return False
    return login.strip().lower() in _configured_bot_logins()


def is_internal_bot_author(author: str | None) -> bool:
    """Return whether a GraphQL comment ``author`` login is one of ours.

    GraphQL reports an App's bare slug (``open-swe``) where REST reports the
    suffixed login, so both spellings must match here. This decides only
    whether a comment is our own, never whether a sender is authorized.
    """
    if not isinstance(author, str) or not author:
        return False
    candidate = author.strip().lower()
    known = _configured_bot_logins()
    return candidate in known or f"{candidate}[bot]" in known


async def is_user_active_org_member(username: str, org: str) -> bool:
    """Return True if ``username`` is an *active* member of ``org``.

    Uses the GitHub App installation token so that private organization
    memberships are visible (the same approach as the reference
    ``tag-external-contributions.yml`` workflow). On any API error, returns
    ``False`` — fail-closed for security.

    Requires the GitHub App to have the ``Organization -> Members: Read-only``
    permission; the ``GET /orgs/{org}/memberships/{username}`` endpoint returns
    403 (-> ``False``) without it. See docs/INSTALLATION.md.
    """
    if not username or not org:
        return False

    token = await get_github_app_installation_token()
    if not token:
        logger.warning(
            "GitHub App token unavailable; cannot verify org membership for %s", username
        )
        return False

    url = f"https://api.github.com/orgs/{org}/memberships/{username}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
    except Exception:
        logger.exception("Error calling GitHub org membership API for %s/%s", org, username)
        return False

    if response.status_code == 404:
        return False
    if response.status_code != 200:
        logger.warning(
            "Unexpected status %s checking %s membership for %s",
            response.status_code,
            org,
            username,
        )
        return False

    try:
        state = response.json().get("state")
    except ValueError:
        logger.warning("Failed to parse org membership response for %s/%s", org, username)
        return False
    return state == "active"
