from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _dependency_name(specification: str) -> str:
    match = re.match(r"[A-Za-z0-9_.-]+", specification)
    assert match is not None
    return match.group(0).replace("_", "-").casefold()


def test_runtime_lock_contains_every_application_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        _dependency_name(item) for item in project["project"]["dependencies"]
    }
    locked = {
        _dependency_name(line)
        for line in (ROOT / "requirements-runtime.lock").read_text(
            encoding="utf-8"
        ).splitlines()
        if line and not line.startswith("#")
    }

    assert declared <= locked
    assert {"pytest", "ruff", "mypy", "pyinstaller"}.isdisjoint(locked)


def test_python_runner_uses_production_data_and_pythonw() -> None:
    runner = (ROOT / "run-officeflow-python.cmd").read_text(encoding="utf-8")
    development_runner = (ROOT / "run-officeflow-dev.cmd").read_text(
        encoding="utf-8"
    )

    assert 'set "OFFICEFLOW_DATA_DIR="' in runner
    assert "pythonw.exe" in runner
    assert "-m officeflow.main" in runner
    assert ".local-data" not in runner
    assert ".local-data" in development_runner


def test_user_cmd_files_keep_windows_line_endings() -> None:
    for name in (
        "setup-officeflow-python.cmd",
        "run-officeflow-python.cmd",
        "diagnose-officeflow-python.cmd",
    ):
        content = (ROOT / name).read_bytes()
        assert b"\r\n" in content
        assert content.count(b"\n") == content.count(b"\r\n")


def test_python_setup_supports_online_offline_and_isolated_smoke_test() -> None:
    setup = (ROOT / "setup-officeflow-python.cmd").read_text(encoding="utf-8")

    assert "py -3.12 -m venv" in setup
    assert "requirements-runtime.lock" in setup
    assert "--no-index" in setup
    assert 'if exist "wheelhouse\\*.whl"' in setup
    assert "--smoke-test" in setup
    assert "OfficeFlow-Python-Smoke" in setup
    assert "requirements.lock" not in setup


def test_python_package_builder_contains_required_user_files() -> None:
    builder = (ROOT / "scripts" / "build-python-package.ps1").read_text(
        encoding="utf-8"
    )

    for expected in (
        "setup-officeflow-python.cmd",
        "run-officeflow-python.cmd",
        "diagnose-officeflow-python.cmd",
        "requirements-runtime.lock",
        "Python-실행안내.md",
        "OfficeFlow-사용자안내.html",
        "SHA256",
        "Remove-Item -LiteralPath $packageRoot",
    ):
        assert expected in builder


def test_runtime_diagnostics_passes_in_isolated_data_directory(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(tmp_path)
    data_directory = tmp_path / "OfficeFlow"
    environment["OFFICEFLOW_DATA_DIR"] = str(data_directory)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "diagnose_python_runtime.py")],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "결과: 8/8개 통과" in result.stdout
    assert str(data_directory.resolve()) in result.stdout
    assert not (data_directory / ".python-runtime-write-test").exists()
