from __future__ import annotations

import json
from pathlib import Path


def test_thread_retention_is_bounded() -> None:
    config = json.loads((Path(__file__).parents[2] / "langgraph.json").read_text())

    ttl = config["checkpointer"]["ttl"]
    assert ttl["strategy"] == "delete"
    assert ttl["default_ttl"] == 24 * 60


def test_gh_cli_download_is_checksum_pinned() -> None:
    config = json.loads((Path(__file__).parents[2] / "langgraph.json").read_text())

    assert config["dockerfile_lines"][:2] == [
        (
            "# gh v2.62.0 linux_arm64 checksum from "
            "https://github.com/cli/cli/releases/download/v2.62.0/gh_2.62.0_checksums.txt; "
            "bump version and checksum together"
        ),
        (
            "ADD --checksum=sha256:"
            "a165413209aab98bfb1db9629b97bc9c59778d38bb7378a33a0363cf822e7965 "
            "https://github.com/cli/cli/releases/download/v2.62.0/"
            "gh_2.62.0_linux_arm64.tar.gz /tmp/gh.tgz"
        ),
    ]
