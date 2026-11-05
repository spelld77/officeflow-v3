# OfficeFlow 3.0.15 Python 릴리스 체크리스트

작성일: 2026-11-05 (Asia/Seoul). 상태: **Python ZIP 생성·격리 검증·main 반영·태그·정식 릴리스 공개·다운로드 검증 완료.**

## 최종 범위와 보호

U39(85~86번) 종료 교착 개선을 3.0.15로 마감한다.
최초 요청은 EXE/설치본/ZIP·커밋이었으나, 사용자는 보안프로그램의 OfficeFlow.exe 등 삭제를 알린 뒤
앞으로 **Python ZIP만 만들고 압축 해제해 사용하는 방식**을 유지하도록 지시했다.
EXE·설치본·EXE를 담은 Windows ZIP과 개인 PC 빌드 이관은 이번/향후 기본 범위에서 제외한다.
나중에 명시적으로 다시 요청받을 때만 해당 버전부터 Windows 배포물을 포함한다.

최종 배포물은 Python ZIP·개별 SHA-256·SHA256SUMS·manifest다.
GitHub 푸시/태그/릴리스 공개·실사용 프로그램 교체/실행은 포함하지 않는다.
사용 중인 Python 3.0.14와 운영 DB/설정/첨부/백업을 열거나 변경하지 않았다.
DB0017·전체 백업 v3·기존 데이터 위치를 유지하며 3.0.14에서 새 DB 이행은 없다.

## 게이트

- [x] 사용자 최종 Python 전용 배포·로컬 커밋 승인, codex/fix-thread-shutdown 브랜치
- [x] 버전/사용자 안내·CHANGELOG·릴리스 노트 정리
- [x] build-release.ps1 기본값을 Python 전용으로 변경, Windows 빌드는 명시적 옵션으로 분리
- [x] 버전/패키지/종료 관련 검사 20개, 빌드 기본값 변경 후 관련 4개 통과
- [x] Ruff·format·mypy 소스 164개·PowerShell 구문·diff 공백 검사
- [x] 최종 Python ZIP 196개 payload 파일과 작업 소스의 바이트/CRC 대조
- [x] 별도 Python 환경의 추출 패키지/메타데이터 3.0.15·진단 8/8·빈 프로필 실행
- [x] 합성 기존 DB0017의 행/파일/설정 보존·정상 종료·전체 백업 v3 검증
- [x] 최종 manifest/체크섬 대조·Windows 배포물 제거·소유 fixture 정리
- [x] 로컬 커밋 대상·본문 준비. 커밋 결과와 정확한 식별자는 이 브랜치의 Git 이력으로 확인

## 개발 검사 근거와 추가 검사

86번의 최종 합성 종료 3,000회(42.515초), 고유 회귀 1,693개 통과·1개 환경 제외를 사용한다.
분할 실행/실패 보완 기록을 유지하며 단일 전체 완주로 주장하지 않는다.
전체 회귀/3,000회를 다시 반복하지 않았다. 버전/배포/종료 관련 20개 통과 후
빌드 기본값을 바꿔 관련 4개만 재검사했다. Ruff·변경 파일 format·mypy 소스 164개 통과.
Windows 100/150/200% × 밝은/어두운 주입 팔레트의 기존 대표 캡처 6개와 최종 2조건 재확인을 사용한다.
이는 실제 OS 테마/DPI를 바꾼 검사가 아니다.

최종 ZIP payload 196개를 소스와 바이트로 비교하고 CRC를 확인했다.
새 시험용 Python 환경에 추출 패키지만 editable 설치해 소스/메타데이터 모두 3.0.15인지 확인했다.
진단 8/8·실제 smoke·빈 DB0017의 integrity/FK 검사를 통과했다.
실행 라이브러리는 개발 site-packages를 읽기 참조했으며 개발/실사용 설치를 교체하지 않았다.
최초 온라인 setup 검증은 아니며 ZIP에는 Python/wheelhouse가 없어 최초 setup에는 인터넷 연결이 필요하다.

합성 기존 0017 프로필에서 업무 1·첨부 3·자산 3·가족 1·일지 1·이력 1을 포함한
10개 업무 관련 테이블을 전후 대조했다. 첨부 영역 파일 5개의 해시와 사용자 설정을 보존했다.
창 위치·compact_list·현재 view_preferences는 정상 종료 때 기존 계약대로 저장되는 네 필드이며,
정확한 기대값과 나머지 모든 설정값을 구분해 대조했다. 정상 종료 표식 정리와 전체 백업 v3 검증도 통과했다.
기록은 artifacts/release-3.0.15/ 아래 focused.xml, python-build-tests.xml,
python-verification-final.log, python-verification.json, final-assets.json이다.

## Windows 배포 시도와 중단

초기 기본 경로에 Inno Setup이 없어 공식 [Inno Setup 다운로드](https://jrsoftware.org/isdl.php)의
6.7.4 배포본을 작업 폴더에 받았다. Authenticode Valid·게시자 Pyrsys B.V.·SHA-256
`a549929319f9120716677c685535ca4671e4b937b5fdc87e2585f32d8eb30e8c`를 확인하고
portable 모드로만 준비했다. 시스템 설치나 보안 설정/예외 등록은 하지 않았다.

EXE/설치본/Windows ZIP 생성과 개별 EXE smoke, 당시 manifest 876개·Windows ZIP 872개 대조는 수행했다.
그러나 설치 수명주기는 완료하지 못했다. 첫 빌드는 샌드박스 임시 폴더 권한에서 실패해
소유 작업 폴더로 옮겼고, 격리 설치는 샌드박스에서 코드 4로 실패했다.
권한을 허용한 별도 VerificationBuild 설치는 성공했으나 설치된 EXE에서
`init_fs_encoding / No module named encodings` 오류가 났다.
검증기에 설치/재설치와 프로세스 stdout/stderr 로그를 보존하도록 보완했다.
사용자의 보안프로그램 삭제 제보 뒤 EXE 실행/생성을 중단했다.
탐지명/보안제품은 확인되지 않았으므로 오탐 여부나 모든 오류의 원인을 확정하지 않는다.
당시 dist 재대조에서는 876개 누락/변경이 0개였으며, 이후 사용자의 범위 변경에 따라
최종 dist와 소유 빌드/추출 폴더에서 해당 Windows 배포물 자체를 제거했다.
Windows 설치 검사를 통과한 릴리스로 표시하지 않는다.

Python 검증 보완도 기록한다. 임시 검증기의 최초 tests import 경로 누락을 바로잡았고,
설정 파일의 바이트 전체가 같아야 한다는 초기 가정은 정상 UI 상태 저장 때문에 실패했다.
제품 코드는 바꾸지 않고 네 가지 기존 UI 상태 저장을 정확히 검사하도록 고쳤으며 최종 검증을 통과했다.
중단/실패 기록과 Windows 시도 manifest는 artifacts에 보존했다.

## 최종 배포물

| 파일 | bytes | SHA-256 |
| --- | ---: | --- |
| OfficeFlow-Python-3.0.15.zip | 601,801 | `fd24d5e51e140e84dac35adf7e00d4594ce0f9f15b7c1b721bf11f60aedd0d21` |
| OfficeFlow-Python-3.0.15.zip.sha256.txt | 96 | `645d08153da914df8697ff7c234f3f684802a3c9531fdf0c2335a4d8ca4aefda` |
| SHA256SUMS.txt | 217 | `560aa0b6f29491d5878724fdf8f2e13c635e9f57d0babb8525276a4491e6f3b0` |
| release-manifest.json | 541 | `ca064da22eafe0b3f56bf84bf5b5ab31ac3ec1f65a59f003d856e0eaea60ae6e` |

manifest payload는 Python ZIP/개별 체크섬 2개이며 최종 dist에는 위 4개 파일만 있다.
이번 소유 fixture/빌드 도구 폴더 9개를 절대 경로·reparse 여부 확인 후 정리했고 대상 잔여는 0개다.
XML/로그/작은 PNG·검증기·audit JSON은 보존했다. GitHub 공개·회사 PC 장시간 사용·별도 native 자원 plateau·
실제 사용자 자료 확인은 미수행이다. 현재 3.0.14를 종료할 필요는 없으며 새 Python 버전으로 전환하기
직전에만 전체 백업을 만들고 트레이에서 완전히 종료한다.

## GitHub 공개 마감 (후속 요청)

로컬 커밋 후 사용자 요청으로 브랜치 푸시를 완료했고, 이어진 main 병합·릴리스 ZIP 업로드 요청에 따라
아래 공개 작업을 완료했다. 앞선 GitHub 제외 범위/미수행 표현은 로컬 마감 당시 상태다.

- 원격 main `218908ef55f39193792298391a87f40a9417e025`에서 검증한 소스
  `42b36f0adf0952aae3c05c6cf1509a95ae31c108`로 fast-forward 반영했다.
  main과 annotated tag v3.0.15를 atomic push했으며 태그 객체는 `b22b4e88d57a3eb52d8418528828dd6a2f4808aa`다.
- [OfficeFlow 3.0.15](https://github.com/spelld77/officeflow-v3/releases/tag/v3.0.15)를
  2026-11-05 15:51:54 KST(06:51:54 UTC)에 최신 정식 릴리스로 공개했다.
  release ID `434867546`, draft/prerelease=false, 업로드 자산은 최종 표의 Python 관련 4개다.
- 초안에서 이름·개수·uploaded 상태·크기·GitHub SHA-256 digest를 대조한 뒤 공개했다.
  자산 4개를 인증 없이 공개 URL로 다시 다운로드해 로컬 해시와 모두 일치함을 확인했다.
  비인증 API는 공유 IP 요청 제한이 있어 최신 릴리스 메타데이터는 기존 Git 인증으로 확인했다.
- 기존 v3.0.14 release ID `424397261`와 자산 6개를 보존했다. 실사용 실행 폴더/자료는 변경하지 않았다.
- 기록: artifacts/release-3.0.15의 remote-prepublication.json, github-assets-verified.json,
  github-published.json, publication-verification.json, public-verification.json.
  공개 다운로드용 소유 임시 폴더 1개를 안전 경로 확인 후 제거했다.

태그/배포 ZIP은 검증한 소스 커밋 `42b36f0`에 유지한다. 온라인 안내와 공개 결과는 문서 전용 후속
커밋으로 main에 기록하며 배포물을 다시 생성하지 않는다. GitHub가 자동 제공하는 source code ZIP과
실사용용 `OfficeFlow-Python-3.0.15.zip`을 구분해 릴리스 본문에 안내했다.
