# OfficeFlow 3.0.4 릴리스 체크리스트

- 기준일: 2026-10-01
- 대상: Windows 10/11 x64, Python 3.12 실행 모드
- 포함: 매시 알림 구현 및 사용자 테스트 후 UI 정돈
- 데이터: 기존 경로 유지, 3.0.3에서 DB 스키마 변경 없음

## 검증·게시

- [x] 사용자 개발 실행 테스트에서 정상 동작 확인
- [x] UI 정돈 후 384개 전체 회귀 테스트 통과
- [x] 3.0.4 버전·품질 검사 (384개 통과, 커버리지 84%, Ruff·mypy 통과)
- [x] Windows 실행 파일·설치본 재빌드 및 격리 설치/재설치/제거 검증
- [x] Python ZIP의 신규 격리 런타임·진단·smoke 및 사용자 데이터 제외 검증 (진단 8/8, 실제 시작 경로의 매시 창·다음 구간·종료 점검)
- [x] 배포 파일 SHA-256·명세·버전 확인 (888개 파일, 앱/설치본 3.0.4)
- [ ] 소스 커밋·main 업로드
- [ ] v3.0.4 태그·GitHub 릴리스 및 5개 자산 게시
- [ ] GitHub 자산 크기·SHA-256과 로컬 검증본 일치 확인

## 산출물

- `dist/installer/OfficeFlow-3.0.4-Setup.exe`
- `dist/python/OfficeFlow-Python-3.0.4.zip`
- `dist/python/OfficeFlow-Python-3.0.4.zip.sha256.txt`
- `dist/SHA256SUMS.txt`
- `dist/release-manifest.json`

검증 데이터는 프로젝트의 `build`/`artifacts`만 사용한다. 실제 DB·첨부·실사용 설치 등록·자동 시작은 변경하지 않는다.
기존 로컬 3.0.3 생성 파일은 `artifacts/release-before-3.0.4`에 보관했다.
Windows 포커스/클릭은 사용자 개발 실행 확인 범위이며 회사 PC·다중 모니터·절전 등 모든 환경을 인증하지 않는다.
자동 ON 동작과 제한, Python 업데이트 방법은 도움말과 릴리스 노트에 안내한다.
