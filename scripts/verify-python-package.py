from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import tomllib
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath


def verify(archive: Path, python: Path, destination: Path, expected: str) -> None:
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError("Use a NEW extraction directory")
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        bad = []
        for entry in entries:
            name = PurePosixPath(entry.filename.replace("\\", "/"))
            parts = name.parts
            if (
                name.is_absolute()
                or ".." in parts
                or any(":" in part for part in parts)
                or any(
                    part in {".venv", ".git", ".local-data", "__pycache__"}
                    or part.endswith(".egg-info")
                    for part in parts
                )
                or name.suffix.lower()
                in {".db", ".db-wal", ".db-shm", ".ofbackup", ".pyc", ".pyo", ".log"}
                or (len(parts) > 1 and parts[1] in {"attachments", "backups", "logs"})
            ):
                bad.append(entry.filename)
        if bad:
            raise ValueError(f"Unexpected archive entries: {bad}")
        if sum(entry.file_size for entry in entries) > 1024**3:
            raise ValueError("Archive exceeds verification size limit")
        roots = {PurePosixPath(entry.filename.replace("\\", "/")).parts[0] for entry in entries}
        if len(roots) != 1:
            raise ValueError("Expected one package root")
        root_name = roots.pop()
        if root_name != f"OfficeFlow-Python-{expected}":
            raise ValueError("Unexpected package version/root")
        names = {entry.filename.replace("\\", "/") for entry in entries}
        required = (
            "pyproject.toml",
            "requirements-runtime.lock",
            "setup-officeflow-python.cmd",
            "run-officeflow-python.cmd",
            "diagnose-officeflow-python.cmd",
            "scripts/diagnose_python_runtime.py",
            "packaging/windows/OfficeFlow-사용자안내.html",
            "src/officeflow/infrastructure/database/migrations/versions/0010_attachment_search.py",
            "src/officeflow/presentation/attachment_search_page.py",
        )
        if any(f"{root_name}/{name}" not in names for name in required):
            raise ValueError("Required runtime file missing")
        package.extractall(destination)
    root = destination / root_name
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    if project["project"]["version"] != expected:
        raise ValueError("Project metadata version differs")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["OFFICEFLOW_DATA_DIR"] = str(destination / "isolated-data")

    def run(*args: str) -> str:
        result = subprocess.run(
            [str(python.resolve()), *args],
            cwd=root,
            env=environment,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=180,
            check=True,
        )
        print(result.stdout, end="")
        return result.stdout

    run(
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-index",
        "--no-build-isolation",
        "--no-deps",
        "-e",
        str(root),
    )
    imported = json.loads(
        run(
            "-c",
            "import json,officeflow; from importlib.metadata import version; print(json.dumps([officeflow.__version__,version('officeflow'),officeflow.__file__]))",
        )
    )
    if imported[:2] != [expected, expected] or not Path(imported[2]).resolve().is_relative_to(root):
        raise ValueError("Import did not use the extracted package")
    run(str(root / "scripts/diagnose_python_runtime.py"))
    run("-m", "officeflow.main", "--smoke-test")
    database = destination / "isolated-data" / "data" / "officeflow.db"
    with closing(sqlite3.connect(database)) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0010_attachment_search"
        )
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM attachment_search").fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    print(
        f"Python package {expected}: archive safety, fresh runtime import, diagnostics and smoke test passed"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a Python release in an isolated existing runtime"
    )
    parser.add_argument("archive", type=Path)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    verify(args.archive, args.python, args.destination, args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
