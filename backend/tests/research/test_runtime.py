"""Local process and job safety for the merged research engine."""

from __future__ import annotations

import asyncio
import stat
import threading

import pytest

from options_api.main import MAX_WRITE_BODY, create_app
from stocksweeper.config import Settings
from stocksweeper.pipeline.jobs import JobBusy, JobManager
from stocksweeper.storage import db


def test_data_lock_is_exclusive_and_private(tmp_path) -> None:
    with db.single_instance(tmp_path):
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / ".app.lock").stat().st_mode) == 0o600
        with pytest.raises(RuntimeError, match="in use"), db.single_instance(tmp_path):
            pass
    with db.single_instance(tmp_path):
        pass


def test_schema_initializes_once_per_database_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "results.duckdb"
    with db.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
    monkeypatch.setattr(db, "SCHEMA", "invalid SQL that must not run again")
    with db.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)


def test_queue_is_bounded_persistent_and_coalesces(tmp_path) -> None:
    manager = JobManager(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def blocking(_progress) -> None:
        started.set()
        assert release.wait(5)

    active = manager.submit("data", blocking, coalesce_key="refresh:IREN")
    assert started.wait(2)
    assert manager.submit("data", blocking, coalesce_key="refresh:IREN").id == active.id
    pending = [manager.submit("data", lambda _progress: None) for _ in range(4)]
    with pytest.raises(JobBusy, match="queue is full"):
        manager.submit("data", lambda _progress: None)
    assert manager.get(pending[0].id).state == "queued"

    manager.stop_accepting()
    release.set()
    assert manager.wait(5)
    assert manager.get(active.id).state == "succeeded"
    assert all(manager.get(job.id).state == "failed" for job in pending)
    restarted = JobManager(tmp_path)
    assert restarted.get(active.id).state == "succeeded"


def test_write_guard_rejects_before_read_and_caps_chunked_body(tmp_path) -> None:
    app = create_app(prefetch_universe=False, research_settings=Settings(data_dir=tmp_path))

    async def request(origin: bytes, chunks: list[bytes]) -> tuple[int, int]:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "method": "POST",
            "scheme": "http",
            "path": "/api/research/backtest/run",
            "raw_path": b"/api/research/backtest/run",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"127.0.0.1"),
                (b"origin", origin),
                (b"content-type", b"application/json"),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
        }
        events = []
        reads = 0

        async def receive():
            nonlocal reads
            item = chunks[reads]
            reads += 1
            return {"type": "http.request", "body": item, "more_body": reads < len(chunks)}

        async def send(event):
            events.append(event)

        await app(scope, receive, send)
        status = next(event["status"] for event in events if event["type"] == "http.response.start")
        return status, reads

    status, reads = asyncio.run(request(b"http://evil.example", [b"x" * 5_000_000]))
    assert (status, reads) == (403, 0)
    status, reads = asyncio.run(
        request(b"http://localhost:5173", [b"x" * (MAX_WRITE_BODY // 2)] * 3)
    )
    assert (status, reads) == (413, 3)
