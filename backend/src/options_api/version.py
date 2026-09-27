"""Read-only running and remote revision status for the local UI."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[3]
CANONICAL_ORIGINS = {
    "git@github.com:hypertrial/hyperoptions.git",
    "ssh://git@github.com/hypertrial/hyperoptions.git",
    "https://github.com/hypertrial/hyperoptions.git",
    "https://github.com/hypertrial/hyperoptions",
}
SHA = re.compile(r"^[0-9a-f]{40}$")
POLL_SECONDS = 60
SSH_COMMAND = "ssh -oBatchMode=yes -oConnectTimeout=4 -oConnectionAttempts=1"
router = APIRouter()


class VersionStatus(BaseModel):
    running_sha: str | None
    branch: str | None
    remote_sha: str | None
    status: Literal["current", "update_available", "offline", "unverified_checkout"]
    checked_at: datetime
    frontend_matches: bool | None


def _git(*args: str, timeout: int = 5) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": SSH_COMMAND},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


_running_sha = os.getenv("OPTIONS_APP_SHA") or _git("rev-parse", "HEAD")
if _running_sha is not None and SHA.fullmatch(_running_sha) is None:
    _running_sha = None
_branch = _git("symbolic-ref", "--short", "HEAD")
_cache_lock = threading.Lock()
_cached_at = 0.0
_cached_status: tuple[str | None, str, datetime] | None = None


def get_version_status(frontend_sha: str | None = None) -> VersionStatus:
    global _cached_at, _cached_status
    with _cache_lock:
        if _cached_status is None or time.monotonic() - _cached_at >= POLL_SECONDS:
            checked_at = datetime.now(UTC)
            origin = _git("remote", "get-url", "origin")
            if _running_sha is None or _branch != "main" or origin not in CANONICAL_ORIGINS:
                remote_sha, status = None, "unverified_checkout"
            else:
                response = _git("ls-remote", "origin", "refs/heads/main", timeout=8)
                remote_sha = response.split("\t", 1)[0] if response else None
                if remote_sha is None or SHA.fullmatch(remote_sha) is None:
                    remote_sha, status = None, "offline"
                elif remote_sha == _running_sha:
                    status = "current"
                else:
                    status = "update_available"
            _cached_status = remote_sha, status, checked_at
            _cached_at = time.monotonic()
        remote_sha, status, checked_at = _cached_status

    return VersionStatus(
        running_sha=_running_sha,
        branch=_branch,
        remote_sha=remote_sha,
        status=status,
        checked_at=checked_at,
        frontend_matches=(_running_sha == frontend_sha) if frontend_sha is not None else None,
    )


@router.get("/api/version", response_model=VersionStatus)
async def version(
    frontend_sha: Annotated[str | None, Query(pattern=r"^[0-9a-f]{40}$")] = None,
) -> VersionStatus:
    return await asyncio.to_thread(get_version_status, frontend_sha)
