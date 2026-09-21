# OfficeFlow 3.0.1

OfficeFlow 3.0.1은 휴지통 정리 기능과 Python 실행 방식을 추가한 유지보수 릴리스다.

## 주요 변경

- 휴지통 업무를 확인 후 영구 삭제할 수 있다.
- 영구 삭제 시 체크리스트, 알림, 반복 발생 상태, 첨부 메타데이터와 저장 파일을 함께 정리한다.
- 연결된 업무일지는 삭제하지 않고 날짜별 기록으로 보존하며 삭제한 업무와의 연결만 해제한다.
- 서명되지 않은 설치형 실행 파일이 제한되는 환경을 위한 Python 3.12 실행 모드를 제공한다.
- Python형도 설치형과 동일한 `%LOCALAPPDATA%\OfficeFlow`의 DB, 첨부파일, 설정과 백업을 사용한다.
- 한국어 사용자 안내와 앱 내 도움말에 영구 삭제 및 Python 실행 절차를 추가했다.

## 설치형

`OfficeFlow-3.0.1-Setup.exe`를 실행한다. 관리자 권한은 필요하지 않다. 3.0.0 위에 설치하면
프로그램 파일만 갱신하며 `%LOCALAPPDATA%\OfficeFlow`의 사용자 데이터는 유지한다. 업데이트
전에는 트레이 메뉴에서 기존 OfficeFlow를 완전히 종료하고 데이터 화면에서 수동 백업을
만드는 것을 권장한다.

코드 서명이 적용되지 않은 개인 배포본이므로 Windows SmartScreen 또는 조직 보안 정책의
제한을 받을 수 있다.

## Python형

`OfficeFlow-Python-3.0.1.zip`을 별도 폴더에 압축 해제한다. Python 3.12 64비트를 설치한 뒤
최초 한 번 `setup-officeflow-python.cmd`를 실행하고, 이후 `run-officeflow-python.cmd`로
실행한다. 최초 구성에는 인터넷 연결이 필요하다. 문제가 있으면
`diagnose-officeflow-python.cmd`를 실행한다.

설치형과 Python형을 동시에 실행하지 않는다. 실행 방식을 바꿀 때는 기존 OfficeFlow를
트레이에서 완전히 종료한다.

## 검증

- 전체 자동 테스트와 코드 커버리지 81% 통과
- Ruff 및 mypy strict 검사 통과
- 설치, 업그레이드 재설치, 제거와 사용자 데이터 보존 검증 통과
- Python 3.12, 필수 라이브러리, 사용자 데이터 폴더, SQLite FTS5와 Qt 진단 8/8 통과
- 실행 파일·설치 파일·릴리스 명세 버전 3.0.1 일치

## SHA-256

- `OfficeFlow-3.0.1-Setup.exe`: `c376feade14e4c0c64865016e0a467c38616dc54da2fb905b0706201ecda71b8`
- `OfficeFlow-Python-3.0.1.zip`: `78056d8cd2e55eef5d5ae01f4b07e36aad0a73d1ed035629d5dbb6f5cb52bfe4`

전체 파일 해시는 릴리스의 `SHA256SUMS.txt`, 상세 빌드 정보는 `release-manifest.json`에서
확인할 수 있다.
