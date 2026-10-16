# OfficeFlow 3.0.13 릴리스 체크리스트

작성일: 2026-10-17 (Asia/Seoul). 상태: **문서·검증·EXE/설치본/ZIP·커밋/태그 업로드·최신 정식 릴리스 공개 완료.**

## 범위와 안전

68~77번의 완료 입력 수정·U30 입력 주의·U31 하루 시간표/전체 알림·U32 메뉴 정돈·
U33 빠른 등록 가시성·U34 탭 위치·U35 결과 저장 안내·U26 날짜 이동과 후속 잘림/안내 보완을 포함한다.
DB0016·백업 v3 유지, 실제 사용자 자료/설치/Python 실행 폴더를 교체하지 않는다.
기존 배포물과 3.0.12 공개 릴리스를 보존한다. 반복 회차 이동 예외의 ICS 미반영을 안내한다.

## 게이트

- [x] 사용자 마감/배포/GitHub 업로드 승인 및 최신 원격 3.0.12 확인
- [x] 버전·리소스·사용 안내·README·CHANGELOG·릴리스 노트 정리
- [x] 전체 검사·Ruff/mypy·변경 파일 format·diff
- [x] Windows 대표 화면 100/150/200%, 밝은/어두운 팔레트·좁은 창 검증
- [x] EXE·격리 설치/재설치/제거·Windows/Python ZIP·체크섬/manifest
- [x] 새 프로필·기존 자료/파일/설정·백업 호환·추출 실행·ZIP 소스 대조
- [x] 소스 커밋/태그·원격 업로드·GitHub 자산 해시 대조·정식 공개
- [x] 공개 다운로드 검증·소유 임시 fixture 정리

## 검증 결과

Windows 11 / Python 3.12.4 / PySide6·Qt 6.11.2. 사용자 자료가 아닌 합성 자료와 격리 프로필로 검사했다.
최종 단일 전체 검사 **1,650개 통과 / 460.48초**, 실패·오류·skip 0.
기록: `artifacts/release-3.0.13-all.xml`. 이전 개발 검사/분리 실행으로 대체하지 않았다.
성능 검사의 `record_property`와 JUnit xunit2 호환 경고 1개가 있으며 앱 실행 오류가 아니다.

- Ruff 통과, mypy 소스 154개 통과, 변경 Python 44개 format 통과, diff 공백 검사 통과.
- 실제 Windows 100/150/200% 각 80개 통과(11.86/11.68/12.23초). `artifacts/release-3.0.13-native-{1,1.5,2}.xml`.
  작은 창의 날짜 입력·저장/취소·달력 버튼·종일 두 줄·날짜 이동·테마를 확인했다.
  100% 주 창/날짜 주의와 200% 어두운 기본 팔레트의 하루보기, 150% 이동 창 캡처를 직접 열어 확인했다.
  OS 설정을 바꾼 검사가 아니라 밝은/어두운 앱 기본 팔레트 주입이다. 배율 재검사를 고유 전체 검사 수에 더하지 않는다.
- 전체 검사 뒤 덮인 100% 캡처는 실제 Windows에서 11개 재실행으로 복구했다. `artifacts/release-3.0.13-capture.xml`.

### 임시 자료 정리

이번 전체/배율/캡처/패키지/격리 Python 검증 소유 폴더 8개를 절대 경로와 reparse point 확인 후 정리했다.
보안 검사 Junction 하나는 동일 fixture 내부 대상 확인 후 링크 자체만 먼저 제거했다.
논리 크기 12,024,023,784 bytes이며 sparse 파일이 포함되므로 실제 확보 공간과 같지 않다. 정리 대상 잔여 0개.
합성 fixture는 검사로 다시 만들 수 있으며 XML·로그·작은 캡처·검증 코드·이전 배포물·최종 dist는 보존했다.
실제 사용자 자료와 다른 작업의 임시 폴더는 정리하지 않았다.

### 패키지와 호환

- 이전 `dist`, `build/release`를 `artifacts/release-before-3.0.13-dist`, `artifacts/release-before-3.0.13-build`에 보존했다.
- EXE smoke 및 VerificationBuild의 격리 설치·재설치·제거·자료 표식 보존 통과.
  실제 설치 등록/자동 시작은 변경하지 않았다. 기록: `artifacts/release-3.0.13-build.log`.
- manifest 874개, Windows ZIP 870개 파일을 크기/SHA-256/CRC로 대조했다. DB·첨부·백업·로그·개발 bytecode/cache를 포함하지 않는다.
  새로 추출한 EXE를 clean PATH에서 실행해 빈 DB0016·integrity/FK 및 실제 메모리 WebP 인코딩/디코딩을 확인했다.
- 보존한 3.0.12 EXE의 격리 프로필에 합성 업무·원본/작업본·보관본·체크리스트·일지·이력·중요 시점·설정을 준비했다.
  새 EXE 실행 후 모든 대상 행/파일 해시/설정·integrity/FK 보존 및 전체 백업 검증 통과.
  기록: `artifacts/release-3.0.13-bundle-verification.log`. DB0016·전체 백업 v3 유지.
- Python ZIP을 새 경로에 풀어 테스트 전용 Python에서 추출 소스/메타데이터 3.0.13·진단 8/8·smoke·빈 DB0016을 확인했다.
  실행 라이브러리만 개발 site-packages를 읽기 참조하고 실사용/개발 패키지는 교체하지 않았다.
  Python ZIP의 새 런타임 파일을 포함한 payload 184개는 현재 작업 파일과 SHA-256으로 일치한다.
  기록: `artifacts/release-3.0.13-python-verification.log`. 온라인 최초 setup 또는 실제 회사 환경 검증은 아니며 Python/wheel은 ZIP에 없다.
- EXE 버전 리소스 3.0.13, 설치 리소스 3.0.13.0 확인.

| 배포 파일 | bytes | SHA-256 |
| --- | ---: | --- |
| OfficeFlow-3.0.13-Setup.exe | 45,116,631 | `a09dfc1fed741e598464395b42bf0e44654df2e95fe5623e3856709bebe4b546` |
| OfficeFlow-3.0.13-Windows.zip | 62,507,068 | `964da273d5d3c605501ceaa5ed3fc13f5aa2af36959c1083ba352919cb297dde` |
| OfficeFlow-Python-3.0.13.zip | 573,521 | `f2badd7e60c98a6d692d8a8047905bf1279dd50c8509612b90616df468b895c9` |
| OfficeFlow-Python-3.0.13.zip.sha256.txt | 96 | `d9979d39219c054bafacb22d4d5ca6903e991a0e56b159d3ae09bd058347957d` |
| SHA256SUMS.txt | 105,208 | `33c1bbcd1e79ef2b7b6cfc953216755d3d71dddd979d4c14972ef3c6cb95af70` |
| release-manifest.json | 167,164 | `dc829143f9895f2d7028c168869ee5db1e4f89472b9af72b56f34853d30c9a2b` |

## GitHub 공개 결과

- 2026-10-17 00:18:28 KST(2026-10-16 15:18:28 UTC)에
  [OfficeFlow 3.0.13](https://github.com/spelld77/officeflow-v3/releases/tag/v3.0.13)을 최신 정식 릴리스로 공개했다.
  release ID `415100370`, draft/prerelease=false, 직접 업로드한 자산 6개다.
- 소스 커밋 `4c6087cb3dda00cbde9c631a2f7fc21e43e8f49a`와 annotated tag `v3.0.13`을 원격 main/태그에 atomic push했다.
  태그 객체 `deb7d5303b5b6d68ff5c7eca18789045dd400fc4`, peeled commit은 소스 커밋과 같다.
- 초안 목록의 정확한 릴리스 ID로 6개 자산의 고유 이름·uploaded 상태·크기·GitHub SHA-256 digest를 대조한 후 공개했다.
  GitHub 초안은 태그 endpoint에서 404를 반환해 초안 목록/ID 조회를 사용했다. 공개 후 태그/latest endpoint 모두 정상 확인했다.
- 공개 Python ZIP과 체크섬을 실제 내려받아 로컬 배포물과 SHA-256을 대조했다.
  검증용 다운로드 중복 폴더(573,617 bytes)는 경로/파일/연결 여부 확인 후 정리했으며 최종 dist는 유지했다.
  GitHub에서 다시 받을 수 있다. 앞선 격리 fixture 8개와 합해 이번 소유 임시 폴더 9개를 정리했다.
- 기존 3.0.12 release ID `412913464`와 자산 6개는 유지했다. 기존 공개 릴리스/실사용 실행 폴더를 변경하지 않았다.
- 공개 결과는 문서 전용 후속 커밋으로 main에 반영한다. 태그·배포물은 검증한 소스 커밋에 유지한다.

## 업데이트와 미검증 환경

먼저 전체 백업 후 트레이까지 종료한다. Python형은 새 ZIP을 별도 폴더에 풀고 setup을 한 번 실행한다.
동일한 기본/직접 지정 데이터 경로면 DB·첨부 복사는 필요 없다. 3.0.10~3.0.12와 DB0016·백업 v3가 같다.
반복 회차 이동 예외는 현재 ICS에 포함하지 않는다. 탭 위치는 실행 중에만 제한적으로 복원한다.
실제 회사 DRM/문서 편집기·장기간 운영·OS 테마/DPI 실시간 전환은 별도 확인이다.
실사용 설치/Python 실행 폴더는 교체하지 않는다.
