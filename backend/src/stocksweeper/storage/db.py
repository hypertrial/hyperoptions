"""DuckDB connection helper. One process-wide lock keeps the file writer exclusive."""

from __future__ import annotations

import fcntl
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

import duckdb

SCHEMA = Path(__file__).with_name("schema.sql").read_text()
DB_LOCK = threading.Lock()
_INITIALIZED: dict[Path, int] = {}


class DataDirectoryLock:
    def __init__(self, handle: TextIO) -> None:
        self.handle = handle
        self.deferred = False
        self._guard = threading.Lock()

    def defer_until(self, wait: Callable[[], object]) -> None:
        """Keep the lock while a worker outlives HTTP shutdown."""
        self.deferred = True

        def release_after_worker() -> None:
            try:
                wait()
            finally:
                self.close()

        threading.Thread(target=release_after_worker, daemon=True).start()

    def close(self) -> None:
        with self._guard:
            if self.handle.closed:
                return
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()


@contextmanager
def single_instance(data_dir: Path) -> Iterator[DataDirectoryLock]:
    """Keep one app process attached to the local watchlist data directory."""
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_dir.chmod(0o700)
    descriptor = os.open(data_dir / ".app.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    handle: TextIO = os.fdopen(descriptor, "a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"watchlist data directory is in use: {data_dir}") from exc
        lock = DataDirectoryLock(handle)
        yield lock
    finally:
        if "lock" in locals():
            if not lock.deferred:
                lock.close()
        else:
            handle.close()


@contextmanager
def connect(path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with DB_LOCK:
        connection = duckdb.connect(str(path))
        try:
            inode = path.stat().st_ino
            if _INITIALIZED.get(path) != inode:
                connection.execute(SCHEMA)
                _INITIALIZED[path] = inode
            yield connection
        finally:
            connection.close()


def rows(
    connection: duckdb.DuckDBPyConnection, sql: str, params: list[object] | None = None
) -> list[dict[str, object]]:
    cursor = connection.execute(sql, params or [])
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
