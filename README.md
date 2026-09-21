# OfficeFlow v3

OfficeFlow v3는 개인 업무, 기간 일정, 알림, 업무일지와 첨부파일을 한곳에서 관리하는 Windows 데스크톱 애플리케이션이다.

기존 OfficeFlow v2.6을 직접 개조하지 않고 새 구조로 개발한다. 다만 기존 SQLite 데이터와 첨부파일은 공식 마이그레이션 절차를 통해 보존한다.

## 현재 상태

- 단계: Phase 8 완료
- 버전: OfficeFlow 3.0.1
- 구현 상태: 설치·업그레이드·제거 검증을 마친 Windows 데스크톱 앱
- 대상 플랫폼: Windows 10/11
- UI 기술: PySide6
- 데이터 저장소: SQLite
- 배포 형태: 단일 사용자용 설치 프로그램 또는 Python 3.12 실행 모드

## 제품 원칙

1. 오늘 할 일을 3초 안에 파악할 수 있어야 한다.
2. 업무가 많아져도 스크롤에 의존하지 않고 검색·필터·그룹으로 찾을 수 있어야 한다.
3. 하루 일정과 여러 날 일정을 동일한 흐름으로 등록하고 확인할 수 있어야 한다.
4. 알림은 놓치지 않되 중복되거나 과도하게 나타나지 않아야 한다.
5. 기존 사용자의 데이터는 검증 가능한 방법으로 이전되어야 한다.
6. 기능 추가가 다른 기능을 깨뜨리지 않도록 화면, 업무 규칙, 저장소를 분리한다.

## 설계 문서

- [제품 요구사항](docs/01-product-requirements.md)
- [UX 설계](docs/02-ux-spec.md)
- [기술 아키텍처](docs/03-architecture.md)
- [데이터 모델](docs/04-data-model.md)
- [기존 데이터 이전](docs/05-migration.md)
- [개발 및 품질 지침](docs/06-development-process.md)
- [개발 로드맵](docs/07-roadmap.md)
- [승인할 제품 결정](docs/08-product-decisions.md)
- [ADR-0001: 기술 스택](docs/adr/0001-desktop-stack.md)
- [ADR-0002: v2.6 데이터 변환 규칙](docs/adr/0002-legacy-v26-mapping.md)
- [Phase 1 완료 보고서](docs/phase-reports/phase-1.md)
- [Phase 2 완료 보고서](docs/phase-reports/phase-2.md)
- [Phase 3A 완료 보고서](docs/phase-reports/phase-3a.md)
- [Phase 3 완료 보고서](docs/phase-reports/phase-3.md)
- [Phase 4 완료 보고서](docs/phase-reports/phase-4.md)
- [Phase 5A 완료 보고서](docs/phase-reports/phase-5a.md)
- [Phase 5B 완료 보고서](docs/phase-reports/phase-5b.md)
- [Phase 5C 완료 보고서](docs/phase-reports/phase-5c.md)
- [Phase 6A 완료 보고서](docs/phase-reports/phase-6a.md)
- [Phase 6B 완료 보고서](docs/phase-reports/phase-6b.md)
- [Phase 6C 완료 보고서](docs/phase-reports/phase-6c.md)
- [Phase 7 완료 보고서](docs/phase-reports/phase-7.md)
- [사용자 안내](docs/09-user-guide.md)
- [3.0.0 릴리스 체크리스트](docs/10-release-checklist.md)
- [Python 실행 모드](docs/11-python-runtime.md)
- [3.0.1 릴리스 체크리스트](docs/12-release-checklist.md)
- [Phase 8 완료 보고서](docs/phase-reports/phase-8.md)

## 설치

`dist/installer/OfficeFlow-3.0.1-Setup.exe`를 실행한다. 관리자 권한은 필요하지 않으며,
기본적으로 현재 사용자에게 설치된다. 기존 버전 위에 설치하면 업무 DB와 첨부파일을
유지한 채 프로그램 파일만 갱신한다.

설치본에는 앱 안에서 열 수 있는 한국어 도움말이 포함된다. 프로그램을 제거해도
`%LOCALAPPDATA%\OfficeFlow`의 사용자 데이터는 자동 삭제하지 않는다.

## Python 실행 모드

서명되지 않은 PyInstaller 실행 파일을 제한하지만 Python 실행은 허용된 PC에서는 별도의
Python 모드를 사용할 수 있다. Python 3.12 64비트가 설치된 상태에서 최초 한 번
`setup-officeflow-python.cmd`를 실행하고, 이후 `run-officeflow-python.cmd`를 더블클릭한다.

실사용 Python 모드는 설치형과 동일한 `%LOCALAPPDATA%\OfficeFlow` 데이터를 사용한다.
개발용 `run-officeflow-dev.cmd`는 격리된 `.local-data`를 사용하므로 혼동하지 않는다.
자세한 내용은 [Python 실행 안내](docs/11-python-runtime.md)를 참고한다.

## 내보내기와 백업

- 사이드바의 `데이터`에서 현재 검색·필터 결과를 Excel로 내보낼 수 있다.
- 모든 원본 일정은 여러 날 종료일과 반복 규칙을 유지한 ICS로 내보낸다.
- 수동 백업은 데이터베이스, 첨부파일과 설정을 하나의 `.ofbackup` 파일로 보관한다.
- 복원 파일은 먼저 무결성을 검사하고 다음 실행 전에 적용한다. 적용 직전의 현재 데이터도 자동으로 별도 백업한다.
- 자동 백업 주기와 보관 개수는 `설정`에서 변경할 수 있다.

## v2.6 데이터 가져오기

1. OfficeFlow v2.6을 완전히 종료한다.
2. v3 사이드바의 `데이터`에서 `2.6 데이터 가져오기`를 선택한다.
3. 기존 `office_tasks.db`를 고른다. DB 옆의 `saved_files` 폴더는 자동으로 찾으며 다른
   위치라면 직접 지정한다.
4. `가져오기 전 검사`에서 항목 개수와 경고를 확인한 뒤 `안전하게 가져오기`를 누른다.
5. 완료 후 v3를 완전히 종료하고 다시 실행한다.

원본 v2.6 DB와 파일은 수정하지 않으며, v3의 기존 데이터도 유지된다. 원본 DB 안전 사본과
변환 보고서는 v3 데이터 폴더의 `backups`, `migration-reports`에 저장된다.

## 범위 기준

첫 정식 버전은 개인 PC에서 안정적으로 사용하는 데 집중한다. 팀 협업, 클라우드 동기화, 모바일 앱, 생성형 AI 기능은 초기 범위에 포함하지 않는다.

## 개발 실행

```powershell
.\scripts\bootstrap.ps1
.\scripts\run-dev.ps1
```

전체 품질 검사는 활성화된 가상환경에서 다음 명령으로 실행한다.

```powershell
.\scripts\quality.ps1
```

Windows 실행 파일과 설치 프로그램을 만들고 설치 수명주기를 검증하려면 다음을 실행한다.

```powershell
.\scripts\build-release.ps1
```

빌드 결과는 `dist`에 생성되며 `SHA256SUMS.txt`와 `release-manifest.json`을 함께 제공한다.
