from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from options_api import e2e_server
from stocksweeper.storage.db import connect


def test_fixture_lifespans_isolate_operator_jobs_and_clean_up(tmp_path, monkeypatch):
    operator = tmp_path / "operator"
    monkeypatch.setenv("STOCKSWEEPER_DATA_DIR", str(operator))
    database = operator / "results.duckdb"
    with connect(database) as connection:
        now = datetime.now(UTC)
        connection.execute(
            "INSERT INTO jobs(id,kind,state,progress,message,created_at,updated_at) "
            "VALUES ('savedjob','watch_refresh','queued',0,'queued',?,?)", [now, now]
        )

    async def exercise():
        directories = []
        for _ in range(2):
            async with e2e_server.app.router.lifespan_context(e2e_server.app):
                directory = e2e_server.app.state.settings.resolved_data_dir()
                assert directory != operator
                assert directory.exists()
                directories.append(directory)
                with connect(database) as connection:
                    assert connection.execute(
                        "SELECT state,message FROM jobs WHERE id='savedjob'"
                    ).fetchone() == ("queued", "queued")
            assert not directory.exists()
        assert directories[0] != directories[1]

    asyncio.run(exercise())


def test_fixture_waits_for_deferred_jobs_before_cleanup(monkeypatch):
    import threading

    app = e2e_server.create_fixture_app()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    waits = []

    async def exercise():
        async with app.router.lifespan_context(app):
            directory = app.state.settings.resolved_data_dir()

            def worker(progress):
                started.set()
                assert release.wait(5)
                assert directory.exists()
                (directory / "worker-finished").write_text("done")
                finished.set()

            app.state.jobs.submit("fixture_test", worker)
            assert await asyncio.to_thread(started.wait, 5)
            original_wait = app.state.jobs.wait

            loop = asyncio.get_running_loop()

            def wait(timeout=None):
                waits.append(timeout)
                if timeout is not None:
                    assert not finished.is_set()
                    loop.call_soon_threadsafe(loop.call_later, .05, release.set)
                    return False
                return original_wait()

            monkeypatch.setattr(app.state.jobs, "wait", wait)
        assert finished.is_set()
        assert not directory.exists()
        assert 2.0 in waits and None in waits

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_fixture_cleans_directory_after_startup_failure(monkeypatch):
    import pytest

    def fail_client():
        raise RuntimeError("fixture startup failed")

    monkeypatch.setattr(e2e_server, "create_mock_client", fail_client)
    app = e2e_server.create_fixture_app()

    async def exercise():
        with pytest.raises(RuntimeError, match="fixture startup failed"):
            async with app.router.lifespan_context(app):
                raise AssertionError("startup must fail before serving")
        assert not app.state.settings.resolved_data_dir().exists()

    asyncio.run(exercise())
