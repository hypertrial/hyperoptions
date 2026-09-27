from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from options_api import version


def test_version_reports_remote_change_and_frontend_mismatch(monkeypatch) -> None:
    running = "a" * 40
    remote = "b" * 40
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str, timeout: int = 5) -> str | None:
        calls.append(args)
        if args[:2] == ("remote", "get-url"):
            return "https://github.com/hypertrial/hyperoptions.git"
        if args[0] == "ls-remote":
            return f"{remote}\trefs/heads/main"
        raise AssertionError(args)

    monkeypatch.setattr(version, "_running_sha", running)
    monkeypatch.setattr(version, "_branch", "main")
    monkeypatch.setattr(version, "_cached_status", None)
    monkeypatch.setattr(version, "_git", fake_git)
    first = version.get_version_status("c" * 40)
    second = version.get_version_status(running)
    assert first.status == "update_available"
    assert first.remote_sha == remote
    assert first.frontend_matches is False
    assert second.frontend_matches is True
    assert calls.count(("ls-remote", "origin", "refs/heads/main")) == 1


def test_version_preserves_offline_and_unverified_distinction(monkeypatch) -> None:
    monkeypatch.setattr(version, "_running_sha", "a" * 40)
    monkeypatch.setattr(version, "_branch", "main")
    monkeypatch.setattr(version, "_cached_status", None)
    monkeypatch.setattr(
        version,
        "_git",
        lambda *args, **kwargs: (
            "https://github.com/hypertrial/hyperoptions.git"
            if args[:2] == ("remote", "get-url")
            else None
        ),
    )
    assert version.get_version_status().status == "offline"

    monkeypatch.setattr(version, "_cached_status", None)
    monkeypatch.setattr(version, "_branch", "codex/feature")
    assert version.get_version_status().status == "unverified_checkout"


def test_version_route_validates_frontend_sha(monkeypatch) -> None:
    monkeypatch.setattr(version, "_running_sha", "a" * 40)
    monkeypatch.setattr(version, "_branch", "main")
    monkeypatch.setattr(version, "_cached_status", None)
    monkeypatch.setattr(
        version,
        "_git",
        lambda *args, **kwargs: (
            "https://github.com/hypertrial/hyperoptions.git"
            if args[:2] == ("remote", "get-url")
            else f"{'a' * 40}\trefs/heads/main"
        ),
    )
    app = FastAPI()
    app.include_router(version.router)
    client = TestClient(app)
    assert client.get(f"/api/version?frontend_sha={'a' * 40}").json()["status"] == "current"
    assert client.get("/api/version?frontend_sha=bad").status_code == 422
