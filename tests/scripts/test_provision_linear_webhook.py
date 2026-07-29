from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
MODULE_PATH = ROOT / "scripts/provision_linear_webhook.py"
SPEC = importlib.util.spec_from_file_location("provision_linear_webhook", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
provision = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = provision
SPEC.loader.exec_module(provision)


def legacy(webhook_id: str, *, url: str = provision.DEFAULT_WEBHOOK_URL) -> dict:
    return {
        "id": webhook_id,
        "url": url,
        "enabled": True,
        "allPublicTeams": False,
        "resourceTypes": ["Comment"],
        "team": {
            "id": f"team-{webhook_id}",
            "key": provision.LEGACY_WEBHOOK_TEAMS.get(webhook_id, "OTHER"),
        },
    }


def workspace(webhook_id: str = "workspace-1") -> dict:
    return {
        "id": webhook_id,
        "url": provision.DEFAULT_WEBHOOK_URL,
        "enabled": True,
        "secret": "shared-secret",
        "allPublicTeams": True,
        "resourceTypes": ["Comment"],
    }


def test_fresh_cutover_creates_workspace_webhook_then_deletes_all_legacy() -> None:
    hooks = [legacy(webhook_id) for webhook_id in provision.LEGACY_WEBHOOK_IDS]

    plan = provision.plan_cutover(hooks, provision.DEFAULT_WEBHOOK_URL, "shared-secret")

    assert plan.create_workspace_webhook is True
    assert plan.workspace_webhook_id is None
    assert plan.delete_webhook_ids == provision.LEGACY_WEBHOOK_IDS


def test_completed_cutover_is_a_noop() -> None:
    plan = provision.plan_cutover([workspace()], provision.DEFAULT_WEBHOOK_URL, "shared-secret")

    assert plan == provision.CutoverPlan(False, "workspace-1", ())


def test_interrupted_cutover_deletes_only_remaining_legacy_webhooks() -> None:
    remaining = provision.LEGACY_WEBHOOK_IDS[1]

    plan = provision.plan_cutover(
        [workspace(), legacy(remaining)], provision.DEFAULT_WEBHOOK_URL, "shared-secret"
    )

    assert plan == provision.CutoverPlan(False, "workspace-1", (remaining,))


def test_cutover_refuses_ambiguous_workspace_webhooks() -> None:
    with pytest.raises(provision.ProvisionError, match="Multiple enabled workspace"):
        provision.plan_cutover(
            [workspace("workspace-1"), workspace("workspace-2")],
            provision.DEFAULT_WEBHOOK_URL,
            "shared-secret",
        )


def test_cutover_refuses_to_delete_legacy_id_with_unexpected_shape() -> None:
    hook = legacy(provision.LEGACY_WEBHOOK_IDS[0])
    hook["resourceTypes"] = ["Issue"]

    with pytest.raises(provision.ProvisionError, match="refusing to delete"):
        provision.plan_cutover([hook], provision.DEFAULT_WEBHOOK_URL, "shared-secret")


def test_cutover_refuses_unknown_enabled_webhook_on_endpoint() -> None:
    unknown = legacy("unknown-webhook")

    with pytest.raises(provision.ProvisionError, match="Unexpected enabled webhook"):
        provision.plan_cutover([unknown], provision.DEFAULT_WEBHOOK_URL, "shared-secret")


def test_cutover_refuses_workspace_webhook_with_wrong_secret() -> None:
    hook = workspace()
    hook["secret"] = "different-secret"

    with pytest.raises(provision.ProvisionError, match="Unexpected enabled webhook"):
        provision.plan_cutover([hook], provision.DEFAULT_WEBHOOK_URL, "shared-secret")


def test_cutover_refuses_legacy_id_retargeted_to_another_team() -> None:
    hook = legacy(provision.LEGACY_WEBHOOK_IDS[0])
    hook["team"]["key"] = "EZRA"

    with pytest.raises(provision.ProvisionError, match="refusing to delete"):
        provision.plan_cutover([hook], provision.DEFAULT_WEBHOOK_URL, "shared-secret")


def test_apply_creates_before_deleting_and_converges(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy_hooks = [legacy(webhook_id) for webhook_id in provision.LEGACY_WEBHOOK_IDS]
    states = [legacy_hooks, [workspace(), *legacy_hooks], [workspace()]]
    events: list[str] = []

    monkeypatch.setattr(provision, "list_webhooks", lambda: states.pop(0))
    monkeypatch.setattr(
        provision,
        "create_workspace_webhook",
        lambda url, label, secret: events.append("create") or "workspace-1",
    )
    monkeypatch.setattr(
        provision, "delete_webhook", lambda webhook_id: events.append(f"delete:{webhook_id}")
    )

    result = provision.apply_cutover(
        provision.DEFAULT_WEBHOOK_URL, provision.DEFAULT_LABEL, "shared-secret"
    )

    assert result == provision.CutoverPlan(False, "workspace-1", ())
    assert events == ["create", *(f"delete:{item}" for item in provision.LEGACY_WEBHOOK_IDS)]


def test_preview_is_default_and_does_not_mutate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "shared-secret")
    monkeypatch.setattr(provision, "list_webhooks", lambda: [workspace()])
    monkeypatch.setattr(
        provision,
        "apply_cutover",
        lambda *args: pytest.fail("preview must not apply mutations"),
    )

    assert provision.main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "preview"
    assert payload["plan"]["create_workspace_webhook"] is False
