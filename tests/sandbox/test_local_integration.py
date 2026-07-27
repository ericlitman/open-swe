from typing import cast

import pytest

import agent.integrations.local as local_mod
from agent.utils.sandbox import validate_sandbox_startup_config


class _StubLocalShellBackend:
    def __init__(self, *, root_dir, virtual_mode, env, inherit_env):
        self.root_dir = root_dir
        self.virtual_mode = virtual_mode
        self.env = env
        self.inherit_env = inherit_env


def test_create_local_sandbox_creates_missing_root_dir(monkeypatch, tmp_path):
    root = tmp_path / "nested" / "openswe-sandbox"
    monkeypatch.setenv("LOCAL_SANDBOX_ROOT_DIR", str(root))
    monkeypatch.setattr(local_mod, "LocalShellBackend", _StubLocalShellBackend)

    backend = local_mod.create_local_sandbox()

    assert root.is_dir()
    stub = cast(_StubLocalShellBackend, backend)
    assert stub.root_dir == str(root)
    assert stub.virtual_mode is True
    assert stub.inherit_env is False
    assert "GITHUB_APP_PRIVATE_KEY" not in stub.env
    assert stub.env["PATH"]


def test_create_local_sandbox_defaults_to_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCAL_SANDBOX_ROOT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(local_mod, "LocalShellBackend", _StubLocalShellBackend)

    backend = local_mod.create_local_sandbox()

    stub = cast(_StubLocalShellBackend, backend)
    assert stub.root_dir == str(tmp_path)
    assert stub.virtual_mode is True
    assert stub.inherit_env is False


def test_create_local_sandbox_only_includes_allowlisted_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_SANDBOX_ROOT_DIR", str(tmp_path))
    monkeypatch.setenv("LOCAL_SANDBOX_ENV_ALLOWLIST", "SAFE_VALUE")
    monkeypatch.setenv("SAFE_VALUE", "included")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "excluded")
    monkeypatch.setattr(local_mod, "LocalShellBackend", _StubLocalShellBackend)

    backend = local_mod.create_local_sandbox()

    stub = cast(_StubLocalShellBackend, backend)
    assert stub.env["SAFE_VALUE"] == "included"
    assert "GITHUB_APP_PRIVATE_KEY" not in stub.env


def test_local_sandbox_refuses_github_app_credentials(monkeypatch):
    monkeypatch.setenv("SANDBOX_TYPE", "local")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "secret")
    monkeypatch.delenv("ALLOW_UNSAFE_LOCAL_SANDBOX", raising=False)

    with pytest.raises(ValueError, match="cannot run with GitHub App credentials"):
        validate_sandbox_startup_config()


def test_local_sandbox_unsafe_override_is_explicit(monkeypatch):
    monkeypatch.setenv("SANDBOX_TYPE", "local")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "secret")
    monkeypatch.setenv("ALLOW_UNSAFE_LOCAL_SANDBOX", "true")

    validate_sandbox_startup_config()
