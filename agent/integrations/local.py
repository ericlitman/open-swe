import os

from deepagents.backends import LocalShellBackend

_LOCAL_ENV_DEFAULTS = (
    "HOME",
    "LANG",
    "LC_ALL",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
)
_DEFAULT_LOCAL_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _local_environment() -> dict[str, str]:
    names: set[str] = set(_LOCAL_ENV_DEFAULTS)
    names.update(
        name.strip()
        for name in os.getenv("LOCAL_SANDBOX_ENV_ALLOWLIST", "").split(",")
        if name.strip()
    )
    environment = {name: os.environ[name] for name in names if name in os.environ}
    environment["PATH"] = os.getenv("LOCAL_SANDBOX_PATH", _DEFAULT_LOCAL_PATH)
    return environment


def create_local_sandbox(sandbox_id: str | None = None):
    """Create a local shell sandbox with no isolation.

    WARNING: This runs commands directly on the host machine with no sandboxing.
    Only use for local development with human-in-the-loop enabled.

    The root directory defaults to the current working directory and can be
    overridden via the LOCAL_SANDBOX_ROOT_DIR environment variable. It is
    created if it does not already exist.

    Args:
        sandbox_id: Ignored for local sandboxes; accepted for interface compatibility.

    Returns:
        LocalShellBackend instance implementing SandboxBackendProtocol.
    """
    root_dir = os.getenv("LOCAL_SANDBOX_ROOT_DIR", os.getcwd())
    os.makedirs(root_dir, exist_ok=True)

    return LocalShellBackend(
        root_dir=root_dir,
        virtual_mode=True,
        env=_local_environment(),
        inherit_env=False,
    )
