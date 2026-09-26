from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_check_wheel_uses_the_project_interpreter() -> None:
    text = (ROOT / "scripts" / "check-wheel").read_text()
    assert "uv run python" in text
    assert not any(line.startswith("python3 ") for line in text.splitlines())


def test_verify_installs_chromium_before_playwright() -> None:
    text = (ROOT / "scripts" / "verify").read_text()
    install = text.index("npx --no-install playwright install chromium")
    run = text.index("npx --no-install playwright test")
    assert install < run
