"""One durable local watchlist job at a time."""

from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from stocksweeper.storage.db import connect, rows

Progress = Callable[[float, str], None]
Worker = Callable[[Progress], str | None]
LOG = logging.getLogger(__name__)


class JobBusy(Exception):
    pass


class Job(BaseModel):
    id: str
    kind: str
    state: Literal["queued", "running", "succeeded", "failed"]
    progress: float = 0
    message: str = ""
    error: str | None = None
    run_id: str | None = None


class JobManager:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "results.duckdb"
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._queued: deque[tuple[str, Worker, str | None]] = deque()
        self._keys: dict[str, str] = {}
        self._closing = False
        with connect(self.path) as connection:
            connection.execute(
                """UPDATE jobs SET state = 'failed', message = 'interrupted',
                   error = 'app stopped before the job completed', updated_at = ?
                   WHERE state IN ('queued', 'running')""",
                [datetime.now(UTC)],
            )

    def get(self, job_id: str) -> Job | None:
        with connect(self.path) as connection:
            found = rows(
                connection,
                "SELECT id, kind, state, progress, message, error, run_id FROM jobs WHERE id = ?",
                [job_id],
            )
        return Job.model_validate(found[0]) if found else None

    def active(self, kind: str) -> Job | None:
        with connect(self.path) as connection:
            found = rows(
                connection,
                """SELECT id, kind, state, progress, message, error, run_id
                   FROM jobs WHERE kind = ? AND state IN ('queued', 'running')
                   ORDER BY CASE WHEN state = 'running' THEN 0 ELSE 1 END,
                            created_at DESC LIMIT 1""",
                [kind],
            )
        return Job.model_validate(found[0]) if found else None

    def submit(self, kind: str, worker: Worker, *, coalesce_key: str | None = None) -> Job:
        with self._lock:
            if self._closing:
                raise JobBusy("background jobs are shutting down")
            if coalesce_key is not None and coalesce_key in self._keys:
                existing = self.get(self._keys[coalesce_key])
                if existing is not None:
                    return existing
            if len(self._queued) >= 4:
                raise JobBusy("background job queue is full")
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, state="queued", message="queued")
            now = datetime.now(UTC)
            with connect(self.path) as connection:
                connection.execute(
                    """INSERT INTO jobs (id, kind, state, progress, message, error, run_id,
                       created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [job.id, kind, job.state, job.progress, job.message, None, None, now, now],
                )
            self._queued.append((job.id, worker, coalesce_key))
            if coalesce_key is not None:
                self._keys[coalesce_key] = job.id
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._drain, daemon=True)
                self._thread.start()
            return job

    def stop_accepting(self) -> None:
        with self._lock:
            self._closing = True
            pending = list(self._queued)
            self._queued.clear()
            for _, _, key in pending:
                if key is not None:
                    self._keys.pop(key, None)
        for job_id, _, _ in pending:
            self._update(
                job_id,
                state="failed",
                message="interrupted",
                error="app stopped before the job started",
            )

    def wait(self, timeout: float | None = None) -> bool:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._queued:
                    self._thread = None
                    return
                job_id, worker, key = self._queued.popleft()
            self._run(job_id, worker)
            if key is not None:
                with self._lock:
                    self._keys.pop(key, None)

    def _run(self, job_id: str, worker: Worker) -> None:

        def progress(fraction: float, message: str) -> None:
            self._update(job_id, progress=max(0.0, min(1.0, fraction)), message=message)

        try:
            self._update(job_id, state="running", message="running")
            result = worker(progress)
            self._update(job_id, state="succeeded", progress=1, message="done", run_id=result)
        except Exception:
            LOG.exception("background job %s failed", job_id)
            self._update(job_id, state="failed", error="background job failed", message="failed")

    def _update(self, job_id: str, **changes: object) -> None:
        changes["updated_at"] = datetime.now(UTC)
        assignments = ", ".join(f"{name} = ?" for name in changes)
        with connect(self.path) as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?", [*changes.values(), job_id]
            )
