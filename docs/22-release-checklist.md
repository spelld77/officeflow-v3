# OfficeFlow 3.0.7 릴리스 체크리스트

이 문서는 3.0.7 공개 당시 기록이다. 최신 배포는 [3.0.8 릴리스 체크리스트](37-release-checklist.md)를 따른다.

## 범위

- 첨부 수동 순서, 한 업무의 여러 알림, 오늘·달력 알림 미리보기, 입력칸 내부 요일과 알림 확인·업무 완료 구분을 함께 배포한다.
- `처리 필요`의 분류 기준은 그대로 유지한다.
- 첫 실행에서 기존 DB 보호 사본을 만든 뒤 0011로 업그레이드한다. 새 DB·백업은 3.0.7 이상에서 사용한다.
- 실제 사용자 DB·첨부·설치 등록·Windows 시작 설정은 변경하지 않고 격리 검증한다.

## 검증 및 공개

- [x] 기능 소스 커밋 `157cb46`: 첨부 순서·여러 알림·요일·알림 UX와 설계·테스트 반영
- [x] 버전·EXE 리소스·설치파일·도움말·변경 기록을 3.0.7로 정렬
- [x] 3.0.7 전체 회귀 검사 655개(79.40초), Ruff·mypy 79개 소스·diff 검사
- [x] Windows 125%·200% 네이티브 알림·기록·날짜 입력 검사 각 73개(15.97초·15.46초)
- [x] 기존 로컬 배포 산출물 보존 후 EXE·설치파일·Python ZIP 재생성
- [x] 격리 EXE 실행 및 설치·재설치·제거·데이터 보존 검증
- [x] 새 폴더의 Python ZIP 런타임 진단 8/8·실행과 빈 DB·0011 스키마·무결성 검증
- [x] 868개 산출물 파일의 버전·체크섬·사용자 자료 및 개발 캐시 제외 검증
- [x] main 커밋·push, 주석 태그 v3.0.7
- [x] GitHub 정식 릴리스와 5개 자산 공개
- [x] 공개 자산 상태·크기·SHA-256과 원격 커밋·태그 확인

## 공개 결과

- 기능 소스 커밋: `157cb46e6da9beb58039e1f79d634a69fc0cefc7` (릴리스 소스 커밋에 포함)
- 릴리스 소스 커밋: `fd71c8723bc0feee4bb4e92c754a47c6a2a08be1`
- 주석 태그: `v3.0.7` → 위 릴리스 소스 커밋; 태그 객체 `7f0f9f58ad4a0d046958ef65836a7ee93856bbab`
- 공개 주소: <https://github.com/spelld77/officeflow-v3/releases/tag/v3.0.7>
- 공개 시각: 2026-10-07 00:39:30 한국 / 2026-10-06 15:39:30 UTC
- GitHub 릴리스 ID: `404891611`; `draft=false`, `prerelease=false`, 최신 정식 릴리스 확인
- 모든 공개 자산의 상태가 `uploaded`이며 서버 크기·SHA-256이 로컬 검증본과 일치함

| 공개 자산 | 바이트 | SHA-256 |
| --- | ---: | --- |
| `OfficeFlow-3.0.7-Setup.exe` | 44,380,236 | `a1bb5104b16edb50e37d265ff79227467c0007749dbeadf02907e3724734a84c` |
| `OfficeFlow-Python-3.0.7.zip` | 261,695 | `c896f996b02a31cafcbe1bf6e2d01faf0faf61fbbbf5dacd538dab1d4a50c27a` |
| `OfficeFlow-Python-3.0.7.zip.sha256.txt` | 95 | `977544606eafab19a5ea463ec315a9cf642c99af5c91cd865a908c39ebc5b16e` |
| `SHA256SUMS.txt` | 104,266 | `adab68da175e5a67afcacd8737c7c4e061fbc31cb09c0ab9d2001fc208430ba1` |
| `release-manifest.json` | 165,789 | `42ea38ced40e78d800cd8952241ef0e48d4f403a24eafa1f18f58c6677262326` |

공개 완료 기록은 문서 후속 커밋으로 남기고 `v3.0.7` 태그는 배포 소스 커밋에 고정한다.
설치 파일과 Python ZIP은 Git 저장소가 아니라 릴리스 자산에 올린다. 이전 공개 버전은 변경하지 않는다.

## 배포 전 추가 안전 보완

첫 전체 검사 중 Qt 접근 위반으로 중단되어 공개를 보류하고 알림 미리보기의 스레드 경로를 점검했다.
분리 진단에서 결과 전달과 오류 전달이 작업 스레드에서 발생하는 것을 확인했으며,
새 회귀 검사에서 해당 두 조건이 실패하고 취소·종료 조건 두 개는 통과했다.
UI 스레드 소유 QObject의 queued slot으로 전달하고, 네이티브 스레드의 정리가 끝날 때까지
작업 객체를 유지하도록 종료 시 join을 추가했다. 초기 재현·관련 검사 35개가 통과했으며,
아래 최종 검증은 이 보완이 들어간 소스로 다시 실행한다. 최초 생성한 3.0.7 파일은 공개하지 않는다.

최종 소스의 스레드 회귀 검사 4개를 10회 반복해 모두 통과했다. 전체 655개 검사와 실제 Windows
두 배율의 각 73개 검사도 보완 후 다시 통과했으며, 보완 후 재생성한 파일만 공개 대상으로 검증했다.

## 격리 자료

- 이전 로컬 산출물: `artifacts/release-before-3.0.7`
- 전체 검사: `artifacts/release-3.0.7-tests-final`
- 실제 Windows: `artifacts/release-3.0.7-windows125-01`, `artifacts/release-3.0.7-windows200-01`
- 빌드 로그: `artifacts/release-3.0.7-build-final.log`
- Python ZIP 검증: `artifacts/release-3.0.7-extracted`
- 스레드 반복 검사: `artifacts/release-3.0.7-thread-stress-1`부터 `-10`
- 공개하지 않은 초기 빌드: `artifacts/release-3.0.7-pre-thread-fix`

이 자료는 Git·설치파일·공개 ZIP에 포함하지 않는다. 실사용 기본 데이터 폴더와 기존 공개 릴리스는 유지한다.
