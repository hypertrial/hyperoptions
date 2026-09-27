from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("dev_update", ROOT / "scripts" / "dev_update.py")
assert SPEC and SPEC.loader
dev_update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dev_update)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def repo_pair(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-q")
    local = tmp_path / "local"
    local.mkdir()
    git(local, "init", "-q", "-b", "main")
    git(local, "config", "user.name", "Test")
    git(local, "config", "user.email", "test@example.com")
    (local / "README.md").write_text("base\n")
    git(local, "add", ".")
    git(local, "commit", "-qm", "base")
    git(local, "remote", "add", "origin", str(remote))
    git(local, "push", "-q", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    git(other, "config", "user.name", "Test")
    git(other, "config", "user.email", "test@example.com")
    monkeypatch.setattr(dev_update, "CANONICAL_ORIGINS", {str(remote)})
    return local, other, remote


def test_dev_update_fast_forwards_and_refuses_dirty_or_feature_branch(
    tmp_path, monkeypatch
) -> None:
    local, other, _ = repo_pair(tmp_path, monkeypatch)
    (other / "README.md").write_text("new\n")
    git(other, "commit", "-qam", "new")
    git(other, "push", "-q", "origin", "main")
    assert dev_update.update(local) == "updated"
    assert git(local, "rev-parse", "HEAD") == git(other, "rev-parse", "HEAD")
    assert dev_update.update(local) == "current"

    (local / "untracked").write_text("unsafe")
    with pytest.raises(RuntimeError, match="commit or stash"):
        dev_update.update(local)
    (local / "untracked").unlink()
    git(local, "checkout", "-qb", "codex/feature")
    with pytest.raises(RuntimeError, match="checkout main"):
        dev_update.update(local)


def test_dev_update_refuses_divergence_and_allows_offline_clean_main(tmp_path, monkeypatch) -> None:
    local, other, remote = repo_pair(tmp_path, monkeypatch)
    (local / "README.md").write_text("local\n")
    git(local, "commit", "-qam", "local")
    (other / "README.md").write_text("remote\n")
    git(other, "commit", "-qam", "remote")
    git(other, "push", "-q", "origin", "main")
    with pytest.raises(RuntimeError, match="diverged"):
        dev_update.update(local)

    remote.rename(tmp_path / "remote-offline.git")
    with pytest.raises(RuntimeError, match="diverged"):
        dev_update.update(local)
    git(local, "reset", "--hard", "HEAD~1")
    assert dev_update.update(local) == "offline"


def test_dev_update_timeout_is_unverified_but_wrong_origin_is_rejected(
    tmp_path, monkeypatch
) -> None:
    local, _, remote = repo_pair(tmp_path, monkeypatch)
    real_git = dev_update.git

    def timed_out(root, *args, **kwargs):
        if args[0] == "fetch":
            raise subprocess.TimeoutExpired(["git", "fetch"], 15)
        return real_git(root, *args, **kwargs)

    monkeypatch.setattr(dev_update, "git", timed_out)
    assert dev_update.update(local) == "offline"
    git(local, "remote", "set-url", "origin", str(remote) + "-unexpected")
    with pytest.raises(RuntimeError, match="canonical"):
        dev_update.update(local)
