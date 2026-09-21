# OfficeFlow Python 실행 모드

Python 실행 모드는 서명되지 않은 PyInstaller 실행 파일을 제한하는 환경에서, 공식 Python
인터프리터로 OfficeFlow를 실행하기 위한 별도 배포 방식이다. 조직의 보안 정책을 우회하기
위한 기능은 아니며 해당 PC에서 Python 실행이 허용된 경우에만 사용한다.

## 준비 사항

- Windows 10 또는 Windows 11 64비트
- Python 3.12 64비트
- OfficeFlow 소스 폴더
- 최초 라이브러리 설치를 위한 인터넷 연결 또는 `wheelhouse`가 포함된 오프라인 배포본

Python 3.14가 함께 설치되어 있어도 괜찮지만 OfficeFlow 환경은 반드시 3.12로 만든다.

## 최초 1회 준비

1. `setup-officeflow-python.cmd`를 더블클릭한다.
2. Python 3.12 전용 `.venv`와 실행 라이브러리가 준비될 때까지 기다린다.
3. 마지막 실행 점검이 통과했는지 확인한다.
4. 이후에는 `run-officeflow-python.cmd`만 더블클릭한다.

PowerShell을 열거나 가상환경을 직접 활성화할 필요가 없다. 검은 콘솔창은 실행 준비가 끝난
뒤 닫히고 실제 프로그램은 `pythonw.exe`로 실행된다.

## 데이터 위치

실사용 Python 모드는 설치형 OfficeFlow와 동일한 다음 위치를 사용한다.

```text
%LOCALAPPDATA%\OfficeFlow
```

따라서 기존 설치판의 DB, 첨부파일, 백업과 설정을 그대로 인식한다. 설치형과 Python형을
동시에 실행하지 않는다. 단일 실행 보호 기능이 있지만 버전을 바꿀 때는 기존 프로그램을
먼저 완전히 종료하는 것이 안전하다.

`run-officeflow-dev.cmd`는 개발·시험용이며 프로젝트의 `.local-data`를 사용한다. 실사용
데이터가 보이지 않는 혼동을 피하려면 평소에는 반드시 `run-officeflow-python.cmd`를 쓴다.

## Windows 시작 시 실행

Python 모드로 OfficeFlow를 실행한 상태에서 설정의 `Windows 시작 시 실행`을 켠다. 그러면
현재 `.venv`의 `pythonw.exe -m officeflow.main --background` 명령이 등록된다. 소스 폴더나
`.venv`를 옮긴 경우 `setup-officeflow-python.cmd`를 다시 실행하고 시작 설정을 껐다 켠다.

설치형 OfficeFlow를 나중에 제거하면 제거 프로그램이 기존 자동 시작 항목도 지울 수 있다.
설치형을 먼저 제거한 뒤 Python 모드에서 자동 시작을 다시 켜는 순서가 안전하다.

## 진단

`diagnose-officeflow-python.cmd`를 실행하면 다음을 점검한다.

- Python 3.12 및 64비트 여부
- 전용 가상환경과 필수 라이브러리
- OfficeFlow 모듈
- `%LOCALAPPDATA%\OfficeFlow` 읽기·쓰기
- SQLite FTS5 검색 기능
- PySide6와 Qt 화면 구성요소

진단은 업무 내용이나 첨부파일 내용을 출력하지 않는다.

## 업데이트와 제거

업데이트할 때는 OfficeFlow를 완전히 종료하고 새 소스 파일로 교체한 다음
`setup-officeflow-python.cmd`를 다시 실행한다. 사용자 데이터는 소스 폴더와 분리되어 있어
유지된다.

Python 실행 환경만 제거하려면 자동 시작을 끄고 OfficeFlow를 종료한 뒤 소스 폴더와
`.venv`를 삭제한다. `%LOCALAPPDATA%\OfficeFlow`는 자동 삭제하지 않으며 데이터까지 지우려는
경우에만 별도로 삭제한다.

## 배포본 만들기

인터넷 연결이 가능한 개발 PC에서 다음 명령으로 오프라인 라이브러리를 준비할 수 있다.

```powershell
.\scripts\download-runtime-wheels.ps1
.\scripts\build-python-package.ps1 -RequireOfflineWheels
```

결과 ZIP은 `dist\python`에 생성된다. 오프라인 라이브러리를 포함하지 않아도 된다면 첫 번째
명령을 생략하고 `build-python-package.ps1`만 실행한다.
