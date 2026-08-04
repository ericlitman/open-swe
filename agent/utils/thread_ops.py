"""Shared LangGraph thread helpers for the dashboard.

The webhook triggers (Slack / Linear / GitHub) dispatch through
``agent.dispatch.dispatch_agent_run`` with ``multitask_strategy="interrupt"``,
so they no longer need a busy-check or an in-process lock. The store-queue
below is retained for the dashboard's deliberate "inject a follow-up into a
run that's already in flight" path (``thread_api.send_dashboard_message``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from langgraph_sdk import get_client

logger = logging.getLogger(__name__)

MAX_QUEUED_MESSAGES = 100


def langgraph_url() -> str:
    return os.environ.get("LANGGRAPH_URL") or os.environ.get(
        "LANGGRAPH_URL_PROD", "http://localhost:2024"
    )


def langgraph_client():
    return get_client(url=langgraph_url())


async def get_thread_active_status(thread_id: str) -> bool | None:
    """Return whether the thread is active, or None when status cannot be determined."""
    try:
        thread = await langgraph_client().threads.get(thread_id)
        status = thread.get("status", "idle") if isinstance(thread, dict) else "idle"
        logger.info("Thread %s status check: status=%s", thread_id, status)
        return status == "busy"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to get thread status for %s: %s", thread_id, exc)
        return None


async def reset_thread_preserving_metadata(
    thread_id: str, *, client: Any | None = None
) -> dict[str, Any]:
    """Delete and recreate a thread, preserving metadata except failure-tracking keys."""
    active_client = client if client is not None else langgraph_client()
    try:
        thread = await active_client.threads.get(thread_id)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Thread {thread_id} does not exist") from exc
    if not isinstance(thread, dict):
        raise ValueError(f"Thread {thread_id} does not exist")
    metadata = thread.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    preserved: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in metadata.items():
        if key.startswith("failure_streak") or key.startswith("latest_run_"):
            dropped.append(key)
        else:
            preserved[key] = value

    try:
        runs = await active_client.runs.list(thread_id, limit=1)
        latest = runs[0] if runs else None
        if isinstance(latest, dict):
            status = latest.get("status")
            run_id = latest.get("run_id") or latest.get("id")
        else:
            status = getattr(latest, "status", None)
            run_id = getattr(latest, "run_id", None) or getattr(latest, "id", None)
        if (
            isinstance(status, str)
            and status.lower() in {"pending", "running"}
            and isinstance(run_id, str)
            and run_id
        ):
            await asyncio.wait_for(
                active_client.runs.cancel(thread_id, run_id, wait=True), timeout=30
            )
    except Exception:  # noqa: BLE001
        logger.debug("Failed to cancel active run before resetting thread %s", thread_id)

    await active_client.threads.delete(thread_id)
    await active_client.threads.create(thread_id=thread_id, metadata=preserved)
    return {
        "thread_id": thread_id,
        "preserved_keys": sorted(preserved),
        "dropped_keys": sorted(dropped),
    }


async def queue_message_for_thread(
    thread_id: str, message_content: str | list[dict[str, Any]] | dict[str, Any]
) -> bool:
    """Queue a follow-up message for a busy thread (FIFO store namespace).

    Used by the dashboard to inject a follow-up into a run that's already in
    flight; webhook triggers use ``multitask_strategy="interrupt"`` instead.
    """
    client = langgraph_client()
    try:
        namespace = ("queue", thread_id)
        key = "pending_messages"
        new_message = {"content": message_content}

        existing_messages: list[dict[str, Any]] = []
        try:
            existing_item = await client.store.get_item(namespace, key)
            if existing_item and existing_item.get("value"):
                existing_messages = existing_item["value"].get("messages", [])
        except Exception:  # noqa: BLE001
            logger.debug("No existing queued messages for thread %s", thread_id)

        existing_messages.append(new_message)
        if len(existing_messages) > MAX_QUEUED_MESSAGES:
            existing_messages = existing_messages[-MAX_QUEUED_MESSAGES:]
            logger.warning(
                "Thread %s queue capped at %d messages (dropped oldest)",
                thread_id,
                MAX_QUEUED_MESSAGES,
            )
        await client.store.put_item(namespace, key, {"messages": existing_messages})
        logger.info(
            "Queued message for thread %s (total queued: %d)",
            thread_id,
            len(existing_messages),
        )
        return True
    except Exception:
        logger.exception("Failed to queue message for thread %s", thread_id)
        return False
