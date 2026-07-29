#!/usr/bin/env python3
"""Provision the studio2 workspace-scoped Linear Comment webhook."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass

LINEAR_URL = "https://api.linear.app/graphql"
DEFAULT_WEBHOOK_URL = "https://studio2.tail062eee.ts.net:8443/webhooks/linear"
DEFAULT_LABEL = "open-swe-studio2"
LEGACY_WEBHOOK_TEAMS = {
    "7afda488-f809-49d0-83ed-6bde826d5a64": "OSWE",
    "3dc766dd-2b1f-4b4a-b4a0-ed602c6c81fc": "BEAR",
    "11387313-7056-4f29-9b75-87f57b500e8a": "MASTRA",
}
LEGACY_WEBHOOK_IDS = tuple(LEGACY_WEBHOOK_TEAMS)

WEBHOOKS_QUERY = """
query ProvisionWebhooks($cursor: String) {
  webhooks(first: 100, after: $cursor) {
    nodes {
      id label url enabled secret allPublicTeams resourceTypes teamIds
      team { id key name }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

CREATE_MUTATION = """
mutation ProvisionWebhook($input: WebhookCreateInput!) {
  webhookCreate(input: $input) { success webhook { id enabled } }
}
"""

DELETE_MUTATION = """
mutation DeleteWebhook($id: String!) {
  webhookDelete(id: $id) { success }
}
"""


class ProvisionError(RuntimeError):
    """An actionable provisioning failure."""


@dataclass(frozen=True)
class CutoverPlan:
    """The idempotent mutations required for one cutover."""

    create_workspace_webhook: bool
    workspace_webhook_id: str | None
    delete_webhook_ids: tuple[str, ...]


def linear_gql(query: str, variables: dict) -> dict:
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        raise ProvisionError("LINEAR_API_KEY is not set")
    request = urllib.request.Request(
        LINEAR_URL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProvisionError(f"Linear request failed: {exc}") from exc
    if payload.get("errors"):
        raise ProvisionError(f"Linear GraphQL returned errors: {payload['errors']}")
    return payload.get("data") or {}


def list_webhooks() -> list[dict]:
    variables: dict = {}
    webhooks: list[dict] = []
    while True:
        connection = linear_gql(WEBHOOKS_QUERY, variables).get("webhooks") or {}
        webhooks.extend(node for node in connection.get("nodes") or [] if isinstance(node, dict))
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return webhooks
        cursor = page.get("endCursor")
        if not cursor:
            raise ProvisionError("Linear webhook pagination returned no end cursor")
        variables = {"cursor": cursor}


def _is_workspace_comment_webhook(webhook: dict, url: str, secret: str) -> bool:
    return (
        webhook.get("url") == url
        and webhook.get("enabled") is True
        and webhook.get("secret") == secret
        and webhook.get("allPublicTeams") is True
        and set(webhook.get("resourceTypes") or []) == {"Comment"}
    )


def _validate_legacy_webhook(webhook: dict, url: str, expected_team: str) -> None:
    team = webhook.get("team") or {}
    if (
        webhook.get("url") != url
        or webhook.get("allPublicTeams") is True
        or set(webhook.get("resourceTypes") or []) != {"Comment"}
        or team.get("key") != expected_team
    ):
        raise ProvisionError(
            f"Legacy webhook {webhook.get('id')} no longer has the expected team-scoped "
            f"Comment shape for {url}; refusing to delete it"
        )


def plan_cutover(webhooks: list[dict], url: str, secret: str) -> CutoverPlan:
    desired = [hook for hook in webhooks if _is_workspace_comment_webhook(hook, url, secret)]
    if len(desired) > 1:
        raise ProvisionError(f"Multiple enabled workspace Comment webhooks already target {url}")
    legacy_by_id = {str(hook.get("id")): hook for hook in webhooks if hook.get("id")}
    delete_ids = []
    for webhook_id, expected_team in LEGACY_WEBHOOK_TEAMS.items():
        webhook = legacy_by_id.get(webhook_id)
        if webhook is None:
            continue
        _validate_legacy_webhook(webhook, url, expected_team)
        delete_ids.append(webhook_id)
    allowed_ids = set(LEGACY_WEBHOOK_IDS)
    if desired:
        allowed_ids.add(str(desired[0]["id"]))
    unexpected = [
        str(hook.get("id"))
        for hook in webhooks
        if hook.get("url") == url
        and hook.get("enabled") is True
        and str(hook.get("id")) not in allowed_ids
    ]
    if unexpected:
        raise ProvisionError(
            f"Unexpected enabled webhook(s) already target {url}: {', '.join(unexpected)}"
        )
    return CutoverPlan(
        create_workspace_webhook=not desired,
        workspace_webhook_id=str(desired[0]["id"]) if desired else None,
        delete_webhook_ids=tuple(delete_ids),
    )


def create_workspace_webhook(url: str, label: str, secret: str) -> str:
    result = (
        linear_gql(
            CREATE_MUTATION,
            {
                "input": {
                    "url": url,
                    "label": label,
                    "secret": secret,
                    "resourceTypes": ["Comment"],
                    "allPublicTeams": True,
                }
            },
        ).get("webhookCreate")
        or {}
    )
    webhook = result.get("webhook") or {}
    if result.get("success") is not True or not webhook.get("id"):
        raise ProvisionError("Linear did not confirm workspace webhook creation")
    return str(webhook["id"])


def delete_webhook(webhook_id: str) -> None:
    result = linear_gql(DELETE_MUTATION, {"id": webhook_id}).get("webhookDelete") or {}
    if result.get("success") is not True:
        raise ProvisionError(f"Linear did not confirm deletion of webhook {webhook_id}")


def apply_cutover(url: str, label: str, secret: str) -> CutoverPlan:
    plan = plan_cutover(list_webhooks(), url, secret)
    if plan.create_workspace_webhook:
        create_workspace_webhook(url, label, secret)
        plan = plan_cutover(list_webhooks(), url, secret)
        if plan.create_workspace_webhook or not plan.workspace_webhook_id:
            raise ProvisionError("Workspace webhook was created but could not be confirmed")
    for webhook_id in plan.delete_webhook_ids:
        delete_webhook(webhook_id)
    final = plan_cutover(list_webhooks(), url, secret)
    if final.create_workspace_webhook or final.delete_webhook_ids:
        raise ProvisionError("Webhook cutover did not converge; re-run the command")
    return final


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_WEBHOOK_URL)
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        secret = os.environ.get("LINEAR_WEBHOOK_SECRET")
        if not secret:
            raise ProvisionError("LINEAR_WEBHOOK_SECRET is not set")
        if args.apply:
            plan = apply_cutover(args.url, args.label, secret)
            print(json.dumps({"mode": "apply", "result": asdict(plan)}, indent=2))
        else:
            plan = plan_cutover(list_webhooks(), args.url, secret)
            print(json.dumps({"mode": "preview", "plan": asdict(plan)}, indent=2))
        return 0
    except ProvisionError as exc:
        print(f"provision-linear-webhook: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
