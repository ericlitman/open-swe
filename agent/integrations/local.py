import os
from pathlib import Path

from deepagents.backends import LocalShellBackend

_WORKSPACE_ALIAS = "/workspace"


class LocalHostPathBackend(LocalShellBackend):
    """`LocalShellBackend` with a `/workspace` alias onto the local root.

    `/workspace` is the langsmith sandbox layout that prompts and plan storage
    rely on (`agent/prompt.py`, `plan_store.PLAN_FILE_DIRECTORY`); this alias
    emulates it under the local root while every other path stays host-real,
    so file tools and the shell agree (the OSWE-271 incident class).
    """

    def _resolve_path(self, key: str) -> Path:
        if key == _WORKSPACE_ALIAS or key.startswith(f"{_WORKSPACE_ALIAS}/"):
            rest = key[len(_WORKSPACE_ALIAS) :]
            if ".." in rest or "~" in rest:
                msg = "Path traversal not allowed"
                raise ValueError(msg)
            alias_root = (self.cwd / "workspace").resolve()
            full = (alias_root / rest.lstrip("/")).resolve()
            try:
                full.relative_to(alias_root)
            except ValueError:
                msg = f"Path:{full} outside root directory: {alias_root}"
                raise ValueError(msg) from None
            return full
        return super()._resolve_path(key)


def create_local_sandbox(sandbox_id: str | None = None):
    """Create a local shell sandbox with no isolation.

    WARNING: This runs commands directly on the host machine with no sandboxing.
    Only use for local development with human-in-the-loop enabled.

    The root directory defaults to the current working directory and can be
    overridden via the LOCAL_SANDBOX_ROOT_DIR environment variable. It is
    created if it does not already exist. File tools resolve `/workspace/...`
    under `{root_dir}/workspace` (see `LocalHostPathBackend`); every other
    path resolves host-real, matching the shell.

    Args:
        sandbox_id: Ignored for local sandboxes; accepted for interface compatibility.

    Returns:
        LocalHostPathBackend instance implementing SandboxBackendProtocol.
    """
    root_dir = os.getenv("LOCAL_SANDBOX_ROOT_DIR", os.getcwd())
    os.makedirs(root_dir, exist_ok=True)

    # File tools and the unrestricted shell must share host paths; virtual mode adds no
    # isolation here and made prompt-advertised paths invisible to file tools.
    return LocalHostPathBackend(
        root_dir=root_dir,
        virtual_mode=False,
        inherit_env=True,
    )
