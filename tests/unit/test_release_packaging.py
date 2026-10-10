from __future__ import annotations

import tomllib
from pathlib import Path

from officeflow import __version__
from officeflow.presentation.help import HELP_FILENAME, user_help_path

ROOT = Path(__file__).resolve().parents[2]


def test_release_version_is_consistent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version_info = (ROOT / "packaging/windows/version_info.txt").read_text(encoding="utf-8")
    installer = (ROOT / "packaging/windows/OfficeFlow.iss").read_text(encoding="utf-8")

    assert __version__ == "3.0.10"
    assert project["project"]["version"] == __version__
    assert f"FileVersion', '{__version__}'" in version_info
    assert f"ProductVersion', '{__version__}'" in version_info
    assert f'#define AppVersion "{__version__}"' in installer


def test_installer_is_per_user_upgrade_safe_and_preserves_data() -> None:
    installer = (ROOT / "packaging/windows/OfficeFlow.iss").read_text(encoding="utf-8")

    assert "AppId={{824ED7F8-1C70-4B62-A862-130A68B37C35}" in installer
    assert "PrivilegesRequired=lowest" in installer
    assert "DefaultDirName={localappdata}\\Programs\\OfficeFlow" in installer
    assert 'Name: "startup"' in installer
    assert "--remove-startup" in installer
    assert "[UninstallDelete]" not in installer


def test_release_build_verifies_package_and_installer_lifecycle() -> None:
    build_script = (ROOT / "scripts/build-release.ps1").read_text(encoding="utf-8")
    verify_script = (ROOT / "scripts/verify-installer.ps1").read_text(encoding="utf-8")

    assert "--smoke-test" in build_script
    assert "verify-installer.ps1" in build_script
    assert "SHA256SUMS.txt" in build_script
    assert "build-python-package.ps1" in build_script
    assert "OfficeFlow-$version-Windows.zip" in build_script
    assert "Compress-Archive -LiteralPath $appDir" in build_script
    assert "database\\migrations" in build_script
    assert 'Filter "icu*.dll"' in build_script
    assert '-Filter "__pycache__"' in build_script
    assert "$resolvedAppDir + [IO.Path]::DirectorySeparatorChar" in build_script
    assert verify_script.count("--smoke-test") == 2
    assert "preserve-through-upgrade.txt" in verify_script
    assert 'PATH = "$env:SystemRoot\\System32;$env:SystemRoot"' in verify_script
    assert '"/DVerificationBuild=1"' in build_script
    assert "OfficeFlow Verification" in verify_script
    installer = (ROOT / "packaging/windows/OfficeFlow.iss").read_text(encoding="utf-8")
    assert "#ifdef VerificationBuild" in installer
    assert installer.count("#ifndef VerificationBuild") == 2


def test_user_help_is_available_in_development_tree() -> None:
    help_path = user_help_path()

    assert help_path.name == HELP_FILENAME
    assert help_path.is_file()
    content = help_path.read_text(encoding="utf-8")
    assert "2.6 데이터 가져오기" not in content
    assert 'href="#migration"' not in content
    assert "놓친 알림 확인 범위란?" in content
    assert "오래된 수동 백업 정리" in content
    assert "ICS 내보내기는 현재 목록, 선택 업무 또는 기간" in content
    assert "아래 목록에 최소 3개 일정이 보이고" in content
    assert "목록 확대" in content
    assert "업무가 삭제된 것이 아니라는 안내" in content
    assert "이전 검색어가 자동으로 지워집니다" in content
    assert "파일 선택 (여러 개)" in content
    assert "파일 여러 개를 첨부 목록으로 끌어놓어" in content
    assert "Python 실행 모드" in content
    assert "setup-officeflow-python.cmd" in content
    assert "%LOCALAPPDATA%\\OfficeFlow" in content
    assert f"OfficeFlow {__version__} 사용자 안내" in content
    assert "0016" in content
    assert "공개 이후 개발 소스" not in content
    assert "배포물은 아직 재생성하지 않았습니다" not in content
    assert "개발 소스에 반영되어" not in content
    assert "첨부파일 찾기" in content
    assert "다음 배포 예정" not in content
