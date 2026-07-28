from __future__ import annotations

import json
from pathlib import Path


def test_thread_retention_is_bounded() -> None:
    config = json.loads((Path(__file__).parents[2] / "langgraph.json").read_text())

    ttl = config["checkpointer"]["ttl"]
    assert ttl["strategy"] == "delete"
    assert ttl["default_ttl"] == 24 * 60
