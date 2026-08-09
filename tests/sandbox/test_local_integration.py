from typing import cast

import pytest

import agent.integrations.local as local_mod


class _StubLocalShellBackend:
    def __init__(self, *, root_dir, virtual_mode, inherit_env):
        self.root_dir = root_dir
        self.virtual_mode = virtual_mode
        self.inherit_env = inherit_env


def test_create_local_sandbox_creates_missing_root_dir(monkeypatch, tmp_path):
    root = tmp_path / "nested" / "openswe-sandbox"
    monkeypatch.setenv("LOCAL_SANDBOX_ROOT_DIR", str(root))
    monkeypatch.setattr(local_mod, "LocalHostPathBackend", _StubLocalShellBackend)

    backend = local_mod.create_local_sandbox()

    assert root.is_dir()
    stub = cast(_StubLocalShellBackend, backend)
    assert stub.root_dir == str(root)
    assert stub.virtual_mode is False
    assert stub.inherit_env is True


def test_create_local_sandbox_defaults_to_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCAL_SANDBOX_ROOT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(local_mod, "LocalHostPathBackend", _StubLocalShellBackend)

    backend = local_mod.create_local_sandbox()

    stub = cast(_StubLocalShellBackend, backend)
    assert stub.root_dir == str(tmp_path)
    assert stub.virtual_mode is False


def test_create_local_sandbox_real_backend_uses_host_path_space(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_SANDBOX_ROOT_DIR", str(tmp_path))

    backend = local_mod.create_local_sandbox()

    assert backend.virtual_mode is False
    assert backend.cwd == tmp_path


def test_workspace_alias_write_then_read_round_trips_under_local_root(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_SANDBOX_ROOT_DIR", str(tmp_path))
    backend = local_mod.create_local_sandbox()

    write_result = backend.write("/workspace/plans/x.md", "hello plan")
    assert write_result.error is None

    real_path = tmp_path / "workspace" / "plans" / "x.md"
    assert real_path.is_file()
    assert real_path.read_text() == "hello plan"

    read_result = backend.read("/workspace/plans/x.md")
    assert read_result.error is None
    assert read_result.file_data is not None
    assert read_result.file_data["content"] == "hello plan"


def test_host_absolute_path_under_root_is_readable_via_file_api(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_SANDBOX_ROOT_DIR", str(tmp_path))
    backend = local_mod.create_local_sandbox()

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    file_path = repo_dir / "file.txt"
    file_path.write_text("shell-prepared content")

    read_result = backend.read(str(file_path))
    assert read_result.error is None
    assert read_result.file_data is not None
    assert read_result.file_data["content"] == "shell-prepared content"


def test_workspace_alias_rejects_traversal_and_home_escape(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_SANDBOX_ROOT_DIR", str(tmp_path))
    backend = local_mod.create_local_sandbox()

    with pytest.raises(ValueError):
        backend._resolve_path("/workspace/../escape")

    with pytest.raises(ValueError):
        backend._resolve_path("/workspace/~x")


def test_ls_of_local_root_is_unaffected_by_workspace_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_SANDBOX_ROOT_DIR", str(tmp_path))
    backend = local_mod.create_local_sandbox()

    (tmp_path / "example.txt").write_text("x")

    result = backend.ls(str(tmp_path))
    assert result.error is None
    assert result.entries is not None
    assert any(entry["path"] == str(tmp_path / "example.txt") for entry in result.entries)
