from __future__ import annotations

import os
import platform
import sqlite3
import sys
from collections.abc import Callable
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

REQUIRED_DISTRIBUTIONS = (
    "alembic",
    "openpyxl",
    "platformdirs",
    "PySide6",
    "python-dateutil",
    "SQLAlchemy",
    "tzdata",
)


def _check(label: str, action: Callable[[], Any]) -> bool:
    try:
        detail = action()
    except Exception as error:
        print(f"[FAIL] {label}: {error}")
        return False
    suffix = f": {detail}" if detail not in (None, "") else ""
    print(f"[ OK ] {label}{suffix}")
    return True


def _python_version() -> str:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError(f"Python 3.12가 필요합니다. 현재 {platform.python_version()}")
    return platform.python_version()


def _architecture() -> str:
    bits = platform.architecture()[0]
    if bits != "64bit":
        raise RuntimeError(f"64비트 Python이 필요합니다. 현재 {bits}")
    return bits


def _virtual_environment() -> str:
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("가상환경이 아닙니다.")
    return str(Path(sys.prefix).resolve())


def _distribution_versions() -> str:
    installed: list[str] = []
    for distribution in REQUIRED_DISTRIBUTIONS:
        try:
            installed.append(f"{distribution} {version(distribution)}")
        except PackageNotFoundError as error:
            raise RuntimeError(f"{distribution} 패키지가 없습니다.") from error
    return ", ".join(installed)


def _officeflow_import() -> str:
    package = import_module("officeflow")
    return str(getattr(package, "__version__", "버전 정보 없음"))


def _data_directory() -> str:
    from officeflow.bootstrap.paths import AppPaths

    paths = AppPaths.discover()
    override = os.environ.get("OFFICEFLOW_DATA_DIR")
    if override is not None and paths.root != Path(override).expanduser().resolve():
        raise RuntimeError("지정된 데이터 경로를 사용하지 못했습니다.")
    if override is None and paths.root.name != "OfficeFlow":
        raise RuntimeError(f"예상하지 못한 실사용 데이터 경로입니다: {paths.root}")
    paths.root.mkdir(parents=True, exist_ok=True)
    probe = paths.root / ".python-runtime-write-test"
    try:
        probe.write_text("officeflow", encoding="utf-8")
        if probe.read_text(encoding="utf-8") != "officeflow":
            raise OSError("쓰기 결과를 다시 읽지 못했습니다.")
    finally:
        probe.unlink(missing_ok=True)
    return str(paths.root)


def _sqlite_features() -> str:
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
        connection.execute("INSERT INTO probe(content) VALUES ('officeflow')")
        count = connection.execute(
            "SELECT COUNT(*) FROM probe WHERE probe MATCH 'officeflow'"
        ).fetchone()
    if count is None or count[0] != 1:
        raise RuntimeError("SQLite FTS5 검색 점검에 실패했습니다.")
    return f"SQLite {sqlite3.sqlite_version}, FTS5"


def _qt_runtime() -> str:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import qVersion
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    app.processEvents()
    return f"Qt {qVersion()}"


def main() -> int:
    print("OfficeFlow Python 실행 환경 진단")
    print(f"실행 파일: {Path(sys.executable).resolve()}")
    print()
    checks = (
        _check("Python 버전", _python_version),
        _check("Python 아키텍처", _architecture),
        _check("전용 가상환경", _virtual_environment),
        _check("실행 라이브러리", _distribution_versions),
        _check("OfficeFlow 불러오기", _officeflow_import),
        _check("실사용 데이터 폴더 읽기·쓰기", _data_directory),
        _check("SQLite 검색 기능", _sqlite_features),
        _check("Qt 화면 구성요소", _qt_runtime),
    )
    print()
    print(f"결과: {sum(checks)}/{len(checks)}개 통과")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
