from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_backend_package_description_matches_the_shipped_product() -> None:
    metadata = tomllib.loads((ROOT / "backend" / "pyproject.toml").read_text())
    assert metadata["project"]["description"] == (
        "Local Nasdaq covered-call and cash-secured-put options workstation"
    )


def _executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def test_dev_repairs_an_existing_incomplete_node_modules(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "scripts", tmp_path / "scripts")
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend" / "node_modules").mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"
    _executable(
        fake_bin / "npm",
        '#!/bin/sh\nprintf \'%s|npm %s\\n\' "$PWD" "$*" >> "$TEST_CALL_LOG"\n',
    )
    _executable(
        fake_bin / "uv",
        '#!/bin/sh\nprintf \'%s|uv %s\\n\' "$PWD" "$*" >> "$TEST_CALL_LOG"\n'
        '[ "${1:-}" = sync ] && exit 0\nexit 1\n',
    )
    for name in ("curl", "lsof", "pgrep"):
        _executable(fake_bin / name, "#!/bin/sh\nexit 1\n")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEST_CALL_LOG": str(log),
    }

    subprocess.run(
        ["sh", str(tmp_path / "scripts" / "dev")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    calls = log.read_text().splitlines()
    install = f"{tmp_path / 'frontend'}|npm install"
    backend_sync = f"{tmp_path / 'backend'}|uv sync --group dev"
    backend_start = (
        f"{tmp_path / 'backend'}|uv run uvicorn options_api.main:app "
        "--reload --no-access-log --host 127.0.0.1 --port 8000"
    )
    assert backend_sync in calls
    assert install in calls
    assert calls.index(install) < calls.index(backend_start)


def test_dev_cleanup_does_not_kill_a_later_port_owner(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "scripts", tmp_path / "scripts")
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    lsof_count = tmp_path / "lsof-count"
    unrelated = subprocess.Popen(["sleep", "30"])
    try:
        _executable(fake_bin / "npm", "#!/bin/sh\nexit 0\n")
        _executable(
            fake_bin / "uv",
            '#!/bin/sh\n[ "${1:-}" = sync ] && exit 0\nexit 1\n',
        )
        _executable(fake_bin / "curl", "#!/bin/sh\nexit 1\n")
        _executable(
            fake_bin / "lsof",
            "#!/bin/sh\n"
            "count=0\n"
            '[ ! -f "$TEST_LSOF_COUNT" ] || count=$(cat "$TEST_LSOF_COUNT")\n'
            "count=$((count + 1))\n"
            'printf \'%s\' "$count" > "$TEST_LSOF_COUNT"\n'
            '[ "$count" -le 2 ] || printf \'%s\\n\' "$TEST_UNRELATED_PID"\n'
            "exit 1\n",
        )
        env = {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TEST_LSOF_COUNT": str(lsof_count),
            "TEST_UNRELATED_PID": str(unrelated.pid),
        }

        subprocess.run(
            ["sh", str(tmp_path / "scripts" / "dev")],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert unrelated.poll() is None
        assert lsof_count.read_text() == "2"
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_dev_cleanup_signals_owned_descendants(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "scripts", tmp_path / "scripts")
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    term_log = tmp_path / "terminated.log"
    child = tmp_path / "child.py"
    child.write_text(
        "import signal, sys\n"
        "from pathlib import Path\n"
        "label, ready, log = sys.argv[1:]\n"
        "def stop(*_):\n"
        "    with Path(log).open('a') as stream:\n"
        "        stream.write(f'{label}-child\\n')\n"
        "    raise SystemExit\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "Path(ready).write_text('ready')\n"
        "signal.pause()\n"
    )
    service = fake_bin / "fake-service"
    _executable(
        service,
        "#!/bin/sh\n"
        "label=$1\n"
        'python3 "$TEST_CHILD" "$label" "$TEST_READY/$label" "$TEST_TERM_LOG" &\n'
        "child=$!\n"
        "trap 'wait \"$child\" 2>/dev/null || true; exit 0' TERM\n"
        'wait "$child"\n',
    )
    _executable(
        fake_bin / "uv",
        '#!/bin/sh\n[ "${1:-}" = sync ] && exit 0\nexec "$TEST_SERVICE" backend\n',
    )
    _executable(
        fake_bin / "npm",
        '#!/bin/sh\n[ "${1:-}" = install ] && exit 0\nexec "$TEST_SERVICE" frontend\n',
    )
    _executable(fake_bin / "curl", "#!/bin/sh\nexit 0\n")
    _executable(fake_bin / "lsof", "#!/bin/sh\nexit 1\n")
    ready = tmp_path / "ready"
    ready.mkdir()
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEST_CHILD": str(child),
        "TEST_READY": str(ready),
        "TEST_SERVICE": str(service),
        "TEST_TERM_LOG": str(term_log),
    }
    dev = subprocess.Popen(
        ["sh", str(tmp_path / "scripts" / "dev")],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(100):
            if (ready / "backend").exists() and (ready / "frontend").exists():
                break
            assert dev.poll() is None
            time.sleep(0.02)
        else:
            raise AssertionError("dev services did not start")

        dev.terminate()
        stdout, stderr = dev.communicate(timeout=8)

        assert dev.returncode == 0, stdout + stderr
        assert sorted(term_log.read_text().splitlines()) == [
            "backend-child",
            "frontend-child",
        ]
    finally:
        if dev.poll() is None:
            dev.kill()
            dev.wait(timeout=5)


def test_dev_cleanup_stops_a_launcher_before_its_process_group_exists_even_if_term_is_ignored(
    tmp_path: Path,
) -> None:
    shutil.copytree(ROOT / "scripts", tmp_path / "scripts")
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launcher_pid = tmp_path / "launcher.pid"
    term_log = tmp_path / "terminated.log"
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "import os, signal, sys\n"
        "from pathlib import Path\n"
        "pid_path, term_path = sys.argv[1:]\n"
        "Path(pid_path).write_text(str(os.getpid()))\n"
        "signal.signal(signal.SIGTERM, lambda *_: Path(term_path).write_text('term'))\n"
        "while True:\n"
        "    signal.pause()\n"
    )
    _executable(
        fake_bin / "uv",
        '#!/bin/sh\n[ "${1:-}" = sync ] && exit 0\nexit 1\n',
    )
    _executable(fake_bin / "npm", "#!/bin/sh\nexit 0\n")
    _executable(fake_bin / "curl", "#!/bin/sh\nexit 1\n")
    _executable(fake_bin / "lsof", "#!/bin/sh\nexit 1\n")
    _executable(
        fake_bin / "python3",
        "#!/bin/sh\n"
        'exec "$TEST_REAL_PYTHON" "$TEST_LAUNCHER" '
        '"$TEST_LAUNCHER_PID" "$TEST_TERM_LOG"\n',
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEST_LAUNCHER": str(launcher),
        "TEST_LAUNCHER_PID": str(launcher_pid),
        "TEST_REAL_PYTHON": sys.executable,
        "TEST_TERM_LOG": str(term_log),
    }
    dev = subprocess.Popen(
        ["sh", str(tmp_path / "scripts" / "dev")],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    launched_pid: int | None = None
    try:
        for _ in range(100):
            if launcher_pid.exists():
                launched_pid = int(launcher_pid.read_text())
                break
            assert dev.poll() is None
            time.sleep(0.02)
        else:
            raise AssertionError("backend launcher did not start")

        dev.terminate()
        stdout, stderr = dev.communicate(timeout=8)

        assert dev.returncode == 0, stdout + stderr
        assert term_log.read_text() == "term"
        with pytest.raises(ProcessLookupError):
            os.kill(launched_pid, 0)
    finally:
        if dev.poll() is None:
            dev.kill()
            dev.wait(timeout=5)
        if launched_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(launched_pid, 9)


def test_readme_test_block_runs_from_repository_root(tmp_path: Path) -> None:
    readme = (ROOT / "README.md").read_text()
    block = (
        readme.split("## Tests", 1)[1].split("```bash", 1)[1].split("```", 1)[0].strip()
    )
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"
    for name in ("uv", "npm", "npx"):
        _executable(
            fake_bin / name,
            f'#!/bin/sh\nprintf \'{name} %s\\n\' "$*" >> "$TEST_CALL_LOG"\n',
        )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEST_CALL_LOG": str(log),
    }

    result = subprocess.run(
        ["sh", "-c", block],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "uv run pytest -q",
        "npm test",
        "npx --no-install playwright test",
    ]


def test_verify_rejects_staged_conflict_markers(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "verify", tmp_path / "scripts" / "verify")
    _executable(tmp_path / "scripts" / "dev", "#!/bin/sh\nexit 0\n")
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "README.md").write_text("clean\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "README.md").write_text(
        "<<<<<<< ours\nleft\n=======\nright\n>>>>>>> theirs\n"
    )
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("uv", "npm", "npx"):
        _executable(fake_bin / name, "#!/bin/sh\nexit 0\n")
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["sh", str(tmp_path / "scripts" / "verify")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "leftover conflict marker" in result.stdout + result.stderr


def test_gitignore_excludes_supported_database_names_and_sidecars(
    tmp_path: Path,
) -> None:
    shutil.copy2(ROOT / ".gitignore", tmp_path / ".gitignore")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    names = (
        "paper.sqlite3",
        "paper.sqlite3-wal",
        "paper.sqlite3-v1-backup",
        "paper.db",
        "paper.db-shm",
        "paper.db-v1-backup",
        "options.duckdb",
        "options.duckdb-wal",
        "options.duckdb.wal",
    )
    for name in names:
        (tmp_path / name).touch()

    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    assert status.stdout == "?? .gitignore\n"


def test_verify_runs_backend_lint() -> None:
    verify = (ROOT / "scripts" / "verify").read_text()
    fast = (ROOT / "scripts" / "verify-fast").read_text()

    assert "uv run ruff check src tests scripts" in verify
    assert "ruff check" not in fast


def test_verify_runs_frontend_npm_audit() -> None:
    verify = (ROOT / "scripts" / "verify").read_text()
    fast = (ROOT / "scripts" / "verify-fast").read_text()

    assert "npm audit" in verify
    assert "npm test" in verify
    assert "npx --no-install playwright test" in verify
    assert "npm audit" not in fast


def test_readme_documents_local_chain_and_untouched_local_files() -> None:
    readme = (ROOT / "README.md").read_text()

    assert "Nasdaq-listed" in readme
    assert "fails closed" in readme
    assert "GET /api/tickers?q=&limit=" in readme
    assert "GET /api/covered-calls/{ticker}?moneyness=" in readme
    assert "GET /api/cash-secured-puts/{ticker}?moneyness=" in readme
    assert "GET /api/itm-calls/{ticker}" not in readme
    assert "net outlay = stock cost − premium" in readme
    assert "valid stock bid" in readme
    assert ".local/options.duckdb" in readme
    assert ".local/options.sqlite3" in readme
    assert "left untouched" in readme
    assert "APR (net) =" in readme
    assert "APR (stock) =" in readme
    assert "drop to breakeven = (current − effective cost) / current" in readme
    assert "effective cost = current − call bid" in readme
    assert "called P&L / sh = strike + call bid − current" in readme
    assert "combine with AND" in readme
    assert "`40` means 40%" in readme
    assert "does not clear Contracts" in readme
    assert "*_cents" in readme
    assert "*_pct_tenths" in readme
    assert "ROUND_HALF_UP" in readme
    assert "Show 250 more" in readme
    assert "scrolls horizontally when every column cannot fit" in readme
    assert "Wide desktops fit the table in the page" not in readme
    assert "npm run generate:api" in readme
    assert "npm run check:api" in readme
    assert "scripts/verify-fast" in readme
    assert "scripts/verify" in readme
    assert "npx --no-install playwright test" in readme
    assert "22s overall deadline" in readme
