# OfficeFlow 3.0.2 릴리스 체크리스트

- 기준일: 2026-09-28
- 대상: Windows 10/11 x64
- 설치형: PyInstaller onedir + Inno Setup
- Python형: Python 3.12 전용 ZIP

## 자동 검증

- [x] Ruff 전체 통과
- [x] mypy strict 전체 통과
- [x] 전체 pytest 및 코드 커버리지 82% 통과
- [x] Windows 실행 파일과 설치 프로그램 재빌드
- [x] 깨끗한 설치·업그레이드 재설치·제거 검증
- [x] 설치·업그레이드·제거 중 사용자 데이터 보존 검증
- [x] 실행 파일과 설치 파일의 3.0.2 버전 일치
- [x] Python 실행 환경 구성과 독립 실행 점검
- [x] Python 배포 ZIP 필수 파일 및 체크섬 검증
- [x] SHA-256 목록과 JSON 릴리스 명세 생성

## 3.0.2 기능 회귀

- [x] 업무일지 전체 기간 검색과 선택 상세 유지
- [x] 비정상 종료 안내 후 미처리 알림 표시 순서
- [x] 캘린더 선택 날짜 목록 3개 표시·확대·분할선
- [x] 검색 상태 강조·결과 없음 안내·업무 메뉴 이동 시 자동 해제
- [x] 첨부파일 다중 선택·드래그앤드롭·일부 실패 계속 처리
- [x] 기존 업무·일정·알림·업무일지·첨부·백업 기능 회귀

## 릴리스 산출물

- `dist/installer/OfficeFlow-3.0.2-Setup.exe`
- `dist/python/OfficeFlow-Python-3.0.2.zip`
- `dist/python/OfficeFlow-Python-3.0.2.zip.sha256.txt`
- `dist/SHA256SUMS.txt`
- `dist/release-manifest.json`

코드 서명이 적용되지 않은 개인 배포이므로 설치형 실행 파일은 조직 보안 정책이나 Windows
SmartScreen의 제한을 받을 수 있다. 그런 환경에서 Python 실행이 허용되는 경우 Python형을
대안으로 제공한다.
