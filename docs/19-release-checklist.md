# OfficeFlow 3.0.6 릴리스 체크리스트

## 범위

- 전체 정비 A1–A8·B1–B4와 캘린더 기록·첨부 중심 UX를 함께 배포한다.
- DB 스키마와 기본 사용자 데이터 경로는 유지한다. 새 백업 형식 v2는 3.0.6 이상에서 복원한다.
- 실제 사용자 DB·첨부파일·설치 등록·Windows 시작 설정은 건드리지 않고 격리 검증한다.

## 검증 및 배포

- [x] 버전·EXE 리소스·설치파일·도움말·변경 기록을 3.0.6으로 정렬
- [x] 전체 567개 테스트 통과(209.01초, 커버리지 87%), Ruff·mypy 74개 소스·diff 검사
- [x] 실제 Windows 100%·125% 배율에서 각각 47개 UI 검사 통과(25.94초·25.70초)
- [x] 이전 로컬 빌드 산출물 보존 후 EXE·설치파일·Python ZIP 재생성
- [x] 격리 EXE 실행 및 설치·재설치·제거·데이터 보존 검증
- [x] 새 폴더에 Python ZIP을 풀어 격리 런타임 진단 8/8·실행 검증
- [x] 산출물 버전·크기·867개 파일 체크섬·사용자 자료 제외 검증; EXE의 개발 캐시 제외 보완 및 배포 검사 9개 통과
- [x] main 커밋·push 및 v3.0.6 태그
- [x] GitHub 릴리스와 5개 자산 공개(최신 정식 릴리스)
- [x] 공개 자산 상태·크기·SHA-256 및 원격 커밋 확인

## 공개 결과

- 소스 커밋: `0e1855e94e21c4db34ebd5323511c8d92fbc0866`
- 주석 태그: `v3.0.6` → 위 소스 커밋; 태그 객체 `05da502a99d869b92e6f410fab83c6c2efe100d4`
- 공개 주소: <https://github.com/spelld77/officeflow-v3/releases/tag/v3.0.6>
- 공개 시각: 2026-10-03 16:00:12 한국 / 2026-10-03 07:00:12 UTC
- GitHub 릴리스 ID: `402377149`; `draft=false`, `prerelease=false`, 최신 릴리스 확인
- 모든 자산의 GitHub 상태는 `uploaded`이며 서버 크기와 SHA-256이 로컬과 일치함

| 공개 자산 | 바이트 | SHA-256 |
| --- | ---: | --- |
| `OfficeFlow-3.0.6-Setup.exe` | 44,296,157 | `2459c57501de54a67ffd84dc24c9f6c76f0926d34770e9de4a8ea794abc67795` |
| `OfficeFlow-Python-3.0.6.zip` | 228,412 | `bec8faeae084b8ee4b0807ea954327264756eacd8d43e2061cde7d1753d5cf4a` |
| `OfficeFlow-Python-3.0.6.zip.sha256.txt` | 95 | `7885d3008f8116b9d761cd8f062899550a49f7b1b38f56a3d5243082eb35dac3` |
| `SHA256SUMS.txt` | 104,093 | `558466bb55c777dcb00fda465429b5aaf627805b2bc4741a11a06e485c94272b` |
| `release-manifest.json` | 165,545 | `cba4b5b10a5418263f8244c230ead81c7571748e8054b8671b21cd47a2f32b30` |

공개 완료 기록은 문서 후속 커밋으로 남기고 `v3.0.6` 태그는 배포 소스 커밋에 고정한다.

## 격리 검증 자료

- 전체 검사: `artifacts/release-3.0.6-tests-01`
- 실제 Windows: `artifacts/release-3.0.6-windows-100-01`, `artifacts/release-3.0.6-windows-125-01`
- 최종 배포 검사: `artifacts/release-3.0.6-packaging-final-01`
- 최종 빌드 로그: `artifacts/release-3.0.6-build-final.log`
- Python ZIP 검증: `artifacts/release-3.0.6-extracted-final`; 개발 환경이 아닌 기존 격리 런타임의 의존성을 재사용하고 새 소스 경로·버전·빈 DB를 확인
- 이전 3.0.5 로컬 산출물: `artifacts/release-before-3.0.6`

이 자료는 Git과 공개 ZIP에 포함하지 않는다. 사용자의 실제 DB·첨부와 기존 공개 릴리스도 변경하지 않는다.
전체 커버리지 검사 중 대용량 Excel 메모리 검사가 30초 진단 스택을 출력했으나 이후 정상 완료·통과했다.
