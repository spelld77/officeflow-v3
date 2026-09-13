# OfficeFlow v3

OfficeFlow v3는 개인 업무, 기간 일정, 알림, 업무일지와 첨부파일을 한곳에서 관리하는 Windows 데스크톱 애플리케이션이다.

기존 OfficeFlow v2.6을 직접 개조하지 않고 새 구조로 개발한다. 다만 기존 SQLite 데이터와 첨부파일은 공식 마이그레이션 절차를 통해 보존한다.

## 현재 상태

- 단계: Phase 3 완료, Phase 4 준비
- 구현 상태: 업무 CRUD, 기간 일정 입력, 대량 업무 그룹·필터·지연 로딩이 연결된 데스크톱 앱
- 대상 플랫폼: Windows 10/11
- UI 기술: PySide6
- 데이터 저장소: SQLite
- 배포 형태: 단일 사용자용 설치 프로그램

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
- [Phase 1 완료 보고서](docs/phase-reports/phase-1.md)
- [Phase 2 완료 보고서](docs/phase-reports/phase-2.md)
- [Phase 3A 완료 보고서](docs/phase-reports/phase-3a.md)
- [Phase 3 완료 보고서](docs/phase-reports/phase-3.md)

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
