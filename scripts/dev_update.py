"""Verify and fast-forward a clean local main before starting the dev app."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CANONICAL_ORIGINS = {
    "git@github.com:hypertrial/hyperoptions.git",
    "ssh://git@github.com/hypertrial/hyperoptions.git",
    "https://github.com/hypertrial/hyperoptions.git",
    "https://github.com/hypertrial/hyperoptions",
}
SSH_COMMAND = "ssh -oBatchMode=yes -oConnectTimeout=4 -oConnectionAttempts=1"


def git(root: Path, *args: str, timeout: int = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": SSH_COMMAND},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def checked(root: Path, *args: str) -> str:
    result = git(root, *args)
    if result.returncode:
        detail = result.stderr.strip() or "unknown error"
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def require_fast_forward(root: Path, remote_sha: str) -> str:
    local_sha = checked(root, "rev-parse", "HEAD")
    if git(root, "merge-base", "--is-ancestor", local_sha, remote_sha).returncode:
        raise RuntimeError("main has diverged from origin/main; resolve it before starting")
    return local_sha


def update(root: Path) -> str:
    if Path(checked(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise RuntimeError("run ./scripts/dev from this repository checkout")
    branch = checked(root, "symbolic-ref", "--short", "HEAD")
    if branch != "main":
        raise RuntimeError(f"checkout main before starting (current branch: {branch})")
    if checked(root, "status", "--porcelain", "--untracked-files=normal"):
        raise RuntimeError("commit or stash local changes before starting on main")
    origin = checked(root, "remote", "get-url", "origin")
    if origin not in CANONICAL_ORIGINS:
        raise RuntimeError("origin must be the canonical hypertrial/hyperoptions GitHub repository")

    try:
        fetched = git(
            root,
            "fetch",
            "--no-tags",
            "origin",
            "refs/heads/main:refs/remotes/origin/main",
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        fetched = None
    if fetched is None or fetched.returncode:
        cached = git(root, "rev-parse", "--verify", "refs/remotes/origin/main")
        if cached.returncode == 0:
            require_fast_forward(root, cached.stdout.strip())
        return "offline"

    remote_sha = checked(root, "rev-parse", "refs/remotes/origin/main")
    local_sha = require_fast_forward(root, remote_sha)
    if local_sha == remote_sha:
        return "current"
    checked(root, "merge", "--ff-only", remote_sha)
    return "updated"


if __name__ == "__main__":
    try:
        print(update(Path(sys.argv[1])))
    except (IndexError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        sys.exit(f"error: {exc}")
