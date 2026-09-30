from __future__ import annotations

import runpy
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "entry",
    [
        "OfficeFlow-Python-3.0.3/../outside.txt",
        "OfficeFlow-Python-3.0.3/user.db",
        "OfficeFlow-Python-3.0.3/.venv/config.txt",
        "OfficeFlow-Python-3.0.3/src/officeflow/__pycache__/main.pyc",
        "OfficeFlow-Python-3.0.3/src/officeflow.egg-info/PKG-INFO",
    ],
)
def test_python_package_verifier_rejects_unsafe_or_developer_files(
    tmp_path: Path, entry: str
) -> None:
    verifier = runpy.run_path(str(ROOT / "scripts/verify-python-package.py"))["verify"]
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(entry, "synthetic")
    destination = tmp_path / "extracted"
    with pytest.raises(ValueError, match="Unexpected archive entries"):
        verifier(archive, tmp_path / "unused-python.exe", destination, "3.0.3")
    assert not destination.exists()
