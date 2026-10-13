# OfficeFlow v3

OfficeFlow v3는 개인 업무, 기간 일정, 알림, 업무일지와 첨부파일을 한곳에서 관리하는 Windows 데스크톱 애플리케이션이다.

기존 OfficeFlow를 직접 개조하지 않고 새 구조로 개발했다. 이미 v3로 이전한 업무·업무일지·첨부파일은 그대로 사용하며, 현재 소스에서는 사용이 끝난 v2.6 가져오기 기능을 제거했다.

## 현재 상태

- 단계: Phase 8 완료
- 버전: OfficeFlow 3.0.11
- 구현 상태: 설치·업그레이드·제거 검증을 마친 Windows 데스크톱 앱
- 주요 개선: 첨부 종류 필터·작업본 출처·창 크기 기억·외부 폴더 연결·HTML/Excel 업무 보관·간편 시간 입력·캡처 이미지·썸네일
- 대상 플랫폼: Windows 10/11
- UI 기술: PySide6
- 데이터 저장소: SQLite
- 배포 형태: 단일 사용자용 설치 프로그램, 전체 EXE 폴더 ZIP 또는 Python 3.12 실행 모드

3.0.11은 DB0016·전체 백업 v3를 유지하며 3.0.10에서 추가 DB 이행은 없다.
기존 DB는 첫 실행에서 보호 사본을 만든 뒤 필요한 이행을 순서대로 적용한다.
0016 DB와 이 DB가 포함된 백업은 **3.0.10 이상**에서 사용한다. 구버전 복귀에는 업데이트 전 자료가 필요하다.

## 3.0.11 주요 개선

첨부 목록의 공간을 넓히고 `전체 파일 / 원본 / 작업본 / 일반`으로 필요한 종류만 확인한다.
필터 개수와 `전체 보기`를 표시하며 숨긴 파일은 저장·삭제 대상에 포함하지 않는다. 전체 수동 순서는 유지한다.
작업본의 직접 출처는 한 줄로, 전체 이름·생성 당시 출처·메모는 비차단 `파일 정보…` 창에서 확인한다.
`관련 파일 보기…`는 기존 보관함에서 선택 파일 계열의 현재 첨부를 보여준다.
`넓게 보기 / 이전 크기`는 같은 업무 보기 창을 조절하며 마지막 크기를 다음 업무 보기에서도 기억한다.
작은 창의 5행 잘림과 시스템 어두운 팔레트의 본문 가독성, 조회 중 선택 변경 경쟁을 보완했다.
알림 처리·첨부 내용·경로·보존·삭제 정책은 바꾸지 않는다.

## 3.0.10 주요 개선

외부 폴더 바로가기·HTML/Excel 업무 묶음 보관을 설치형·Windows ZIP·Python ZIP에 함께 포함한다.
업무 묶음은 OfficeFlow 없이 브라우저/Excel로 조회하는 읽기용 사본이며 복원용 전체 백업과 다르다.
내부 자료 정리/삭제·URL 연결은 추가하지 않았다.
새 업무 입력의 휠 오변경 방지·짧은 시간 입력·한 시간 증감·최소 1시간 뒤 정각 기본값과
알림에서 `업무 보기`도 반영했다. 입력·이미지·화면 정돈 자체는 추가 DB 이행을 요구하지 않는다.
알림 추가/수정도 같은 전체 시간 입력을 사용하며, 업무 우클릭의 같은 창 진입은 `업무 보기`로 통일했다.
새 업무/업무 수정의 `클립보드 이미지 추가`에서 캡처 사진을 이름·미리보기 확인 후 함께 저장한다.
기존 첨부 탭의 `클립보드 이미지`에서도 추가할 수 있다. `목록 / 썸네일` 전환으로
여러 사진을 열지 않고 구분하며, 선택한 보기 방식은 다음 업무/재시작에서 기억한다.
확대는 오른쪽 메뉴/더보기의 `이미지 확대…`, 더블클릭은 기존 외부 열기다.
PNG/JPEG/BMP를 지원하며 추가 DB 이행이나 의존성은 없다.
이미지 첨부 설계는 [51번](docs/51-clipboard-image-and-preview-design.md),
검증은 [52번](docs/52-clipboard-image-and-preview-results.md)에 기록한다.
썸네일 후속 설계는 [53번](docs/53-attachment-thumbnail-design.md), 구현·검증 결과는
[54번](docs/54-attachment-thumbnail-results.md)을 따른다. 원본의 고정 내용과 작업본의 저장 내용을
읽으며 현재 보이는 사진만 제한된 메모리 캐시에 둔다. 별도 캐시 파일을 만들지 않는다.
업무 기록·첨부·이력·설정·백업 화면의 긴 설명은 필요한 곳에서 펼쳐 확인한다.
빠른 등록에서 Enter는 선택 업무 수정창을 열지 않고 등록하며 숫자키패드 Enter도 지원한다.

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
- [기존 데이터 이전 (종료된 기능의 설계 기록)](docs/05-migration.md)
- [개발 및 품질 지침](docs/06-development-process.md)
- [개발 로드맵](docs/07-roadmap.md)
- [승인할 제품 결정](docs/08-product-decisions.md)
- [ADR-0001: 기술 스택](docs/adr/0001-desktop-stack.md)
- [ADR-0002: v2.6 데이터 변환 규칙 (역사 기록)](docs/adr/0002-legacy-v26-mapping.md)
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
- [3.0.2 릴리스 체크리스트](docs/12-release-checklist.md)
- [3.0.2 릴리스 노트](docs/release-notes-3.0.2.md)
- [첨부파일 검색 설계](docs/13-attachment-search-design.md)
- [첨부파일 검색 검증 보고서](docs/phase-reports/attachment-search.md)
- [3.0.3 릴리스 체크리스트](docs/14-release-checklist.md)
- [3.0.3 릴리스 노트](docs/release-notes-3.0.3.md)
- [매시 정기 알림 설계](docs/15-hourly-notification-design.md)
- [매시 알림 구현·검증 보고서](docs/phase-reports/hourly-notification.md)
- [3.0.4 릴리스 체크리스트](docs/16-release-checklist.md)
- [3.0.4 릴리스 노트](docs/release-notes-3.0.4.md)
- [3.0.5 릴리스 체크리스트](docs/17-release-checklist.md)
- [3.0.5 릴리스 노트](docs/release-notes-3.0.5.md)
- [캘린더 기록·첨부 중심 UX 설계](docs/18-calendar-records-design.md)
- [3.0.6 릴리스 체크리스트](docs/19-release-checklist.md)
- [3.0.6 릴리스 노트](docs/release-notes-3.0.6.md)
- [첨부 수동 순서 설계](docs/20-attachment-order-design.md)
- [한 업무의 여러 알림·요일 표시 설계](docs/21-multiple-reminders-weekday-design.md)
- [여러 알림·요일 표시 구현·검증 보고서](docs/phase-reports/multiple-reminders-weekday.md)
- [3.0.7 릴리스 체크리스트](docs/22-release-checklist.md)
- [3.0.7 릴리스 노트](docs/release-notes-3.0.7.md)
- [후속 개선 공통설계](docs/24-follow-up-common-design.md)
- [3차 구현·검증 보고](docs/36-follow-up-phase-3-results.md)
- [3.0.8 릴리스 체크리스트](docs/37-release-checklist.md)
- [3.0.8 릴리스 노트](docs/release-notes-3.0.8.md)
- [운영 신뢰성·자료 조회 공통설계](docs/38-reliability-common-design.md)
- [3.0.9 릴리스 체크리스트](docs/45-release-checklist.md)
- [3.0.9 릴리스 노트](docs/release-notes-3.0.9.md)
- [3.0.10 릴리스 체크리스트](docs/59-release-checklist.md)
- [3.0.10 릴리스 노트](docs/release-notes-3.0.10.md)
- [3.0.11 릴리스 체크리스트](docs/62-release-checklist.md)
- [3.0.11 릴리스 노트](docs/release-notes-3.0.11.md)
- [외부 폴더·업무 보관 공통설계](docs/46-folder-and-external-archive-common-design.md)
- [외부 폴더·업무 보관 상세설계](docs/47-folder-and-external-archive-detail.md)
- [외부 폴더·업무 보관 구현·검증](docs/48-folder-and-external-archive-results.md)
- [업무 입력·알림 연결 설계](docs/49-task-input-and-reminder-design.md)
- [업무 입력·알림 연결 구현·검증](docs/50-task-input-and-reminder-results.md)
- [화면 정돈 1~7번 상세설계](docs/55-ui-simplification-phase-1-design.md)
- [화면 정돈 1~7번 구현·검증](docs/56-ui-simplification-phase-1-results.md)
- [화면 정돈 8~14번 상세설계](docs/57-ui-simplification-phase-2-design.md)
- [화면 정돈 8~14번 구현·검증](docs/58-ui-simplification-phase-2-results.md)
- [첨부 공간·종류 필터·출처 상세설계](docs/60-attachment-space-and-source-design.md)
- [첨부 공간·종류 필터·출처 구현·검증](docs/61-attachment-space-and-source-results.md)
- [Phase 8 완료 보고서](docs/phase-reports/phase-8.md)

## 설치

3.0.11은 기존 기능과 첨부 공간·종류 필터·작업본 출처·창 크기 기억 개선을 포함한다.
업데이트 전 전체 백업을 만들고 OfficeFlow를 트레이까지 종료한다. 기존 업무·첨부를 자동 삭제하지 않는다.

3.0.9는 백업 실패의 지속 안내, 수동 자료 검사와 종료 진단, 보관 자료·메모 검색,
백업에서 선택 파일 저장, 달력/오늘 알림 묶음과 개별 시각 조회를 추가했다.
개별 알림 처리 규칙과 `처리 필요` 분류는 유지한다. 폴더 바로가기·외부 업무 묶음 보관은 3.0.10에 추가했다.

3.0.8은 후속 개선 1~3차를 포함한다. 첨부를 외부에 여러 개 저장하거나 원본·작업본·
보관본으로 관리하고 이름·메모를 붙일 수 있다. 업무의 실제 저장된 변경은 이력으로 남고,
중요 시점은 업무 정보·체크리스트·선택 파일을 함께 보관해 비교하고 다시 사용할 수 있다.
기존 첨부는 자동으로 원본 지정하거나 이동하지 않는다. 새 스키마는 0015, 전체 백업 형식은 v3이다.
**업데이트 전 전체 백업을 만들고, 새 DB·백업을 3.0.7 이하에서 열지 않는다.**

3.0.7에서는 첨부파일 수동 순서, 한 업무의 여러 알림과 오늘/달력 알림 미리보기를 추가했다.
날짜 입력칸 안에 요일을 표시하고, 알림창의 `이번 알림 확인`과 `업무 완료…`를 구분했다.
기존 DB는 보호 사본을 만든 후 첨부 순서 스키마 0011로 업그레이드한다. 기존 자료와 파일 경로는 보존하며,
업그레이드된 DB·백업은 3.0.6 이하에서 사용하지 않는다. `처리 필요` 분류 기준은 유지한다.

3.0.6에서는 알림 복구, 백업·복원·첨부 삭제의 데이터 보호, 반복 업무 완료 검색과
대량 내보내기를 정비했다. 캘린더 일정 더블클릭은 업무 기록을 열며, 하단에서 기록·첨부를
바로 확인한다. DB 스키마 변경은 없다. 새 백업은 형식 v2이며 **3.0.6 이상에서 복원**한다.
이전 형식 v1 백업도 계속 읽을 수 있다.

3.0.5에서는 사용이 끝난 v2.6 가져오기 메뉴와 변환 코드를 제거했다. 이미 이전한 데이터와
기존 백업은 그대로 사용하며, 3.0.4에서 DB 스키마 변경은 없다.

3.0.4부터 매시 알림이 추가되었다. 실제 실행마다 기본 활성화되며 트레이의
`매시 알림` 또는 `설정 → 매시 알림 설정...`에서 조정한다. 인사랑 기록은 직접 해야 하며
이 기능은 기록·출근 인증을 하지 않는다. 이번 실행에서 끄더라도 다음 실행에는 다시 켜진다.

[최신 릴리스](https://github.com/spelld77/officeflow-v3/releases/tag/v3.0.11)의
`OfficeFlow-3.0.11-Setup.exe`를 실행한다. 관리자 권한은 필요하지 않으며,
기본적으로 현재 사용자에게 설치된다. 기존 버전 위에 설치하면 업무 DB와 첨부파일을
유지한 채 프로그램 파일만 갱신한다.

설치본에는 앱 안에서 열 수 있는 한국어 도움말이 포함된다. 프로그램을 제거해도
`%LOCALAPPDATA%\OfficeFlow`의 사용자 데이터는 자동 삭제하지 않는다.
설치 없이 EXE로 실행하려면 `OfficeFlow-3.0.11-Windows.zip` 전체를 압축 해제하고
`OfficeFlow/OfficeFlow.exe`를 실행한다. `_internal`을 포함한 폴더 전체가 필요하다.
이 ZIP 방식도 기본 사용자 데이터 위치를 사용하며 USB 독립 데이터 모드는 아니다.

## Python 실행 모드

서명되지 않은 PyInstaller 실행 파일을 제한하지만 Python 실행은 허용된 PC에서는 별도의
Python 모드를 사용할 수 있다. Python 3.12 64비트가 설치된 상태에서 최초 한 번
`setup-officeflow-python.cmd`를 실행하고, 이후 `run-officeflow-python.cmd`를 더블클릭한다.

실사용 Python 모드는 설치형과 동일한 `%LOCALAPPDATA%\OfficeFlow` 데이터를 사용한다.
개발용 `run-officeflow-dev.cmd`는 격리된 `.local-data`를 사용하므로 혼동하지 않는다.
자세한 내용은 [Python 실행 안내](docs/11-python-runtime.md)를 참고한다.

업데이트하려면 기존 OfficeFlow를 완전히 종료하고 수동 백업을 만든 뒤 새 Python ZIP을
별도 폴더에 압축 해제한다. `setup-officeflow-python.cmd`를 한 번 실행한 후
`run-officeflow-python.cmd`로 시작한다. 동일한 사용자 데이터 폴더를 사용하므로 DB·첨부를
배포 폴더에 복사할 필요가 없다. 3.0.10에서 추가 DB 이행은 없으며 더 오래된 DB는 보호 사본 후 0016 스키마를 준비한다.
3.0.2 이하의 오래된 DB는 파일명 검색 색인도 함께 준비한다. 업데이트 전 전체 백업을 권장한다.

## 내보내기와 백업

- 사이드바의 `데이터`에서 현재 검색·필터 결과를 Excel로 내보낼 수 있다.
- 모든 원본 일정은 여러 날 종료일과 반복 규칙을 유지한 ICS로 내보낸다.
- 수동 백업은 데이터베이스, 첨부파일과 설정을 하나의 `.ofbackup` 파일로 보관한다.
- 복원 파일은 먼저 무결성을 검사하고 다음 실행 전에 적용한다. 적용 직전의 현재 데이터도 자동으로 별도 백업한다.
- 자동 백업 주기와 보관 개수는 `설정`에서 변경할 수 있다.

## 이미 이전한 데이터

v2.6에서 가져온 데이터도 일반 v3 업무와 같은 방식으로 조회·수정·검색·백업한다.
현재 소스의 가져오기 기능 제거는 DB 스키마를 변경하거나 데이터를 다시 변환하지 않는다.
기존 원본 ID와 가져오기 표식은 호환용 정보로 유지하며, 이미 저장된 변환 보고서·원본 안전 사본도 자동 삭제하지 않는다.
PC를 옮길 때는 현재 v3의 `.ofbackup` 백업과 복원을 사용한다.

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
