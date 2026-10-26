# OfficeFlow 3.0.14 릴리스 체크리스트

작성일: 2026-10-26, 마감 2026-10-27 (Asia/Seoul). 상태: 배포물 검증 완료, 원격 공개 준비.

## 범위와 보호

U36 자원 정리/관측·U37 최근/고정 바로 찾기·U38 완료 월별 탐색(79~83번)을 포함한다.
DB0017 색인만 추가, 전체 백업 v3 유지. 실제 사용자 DB/첨부/설치·Python 실행 폴더는 변경하지 않는다.
기존 3.0.13 배포물과 공개 릴리스를 보존한다. 0017 DB/백업은 3.0.14 이상이 필요하다.

## 게이트

- [x] 사용자 배포·커밋·GitHub 승인, 원격 main과 최신 3.0.13 확인
- [x] 버전/리소스·안내·CHANGELOG·릴리스 노트 정리
- [x] 마지막 정적/패키지 관련 검사와 대표 Windows 검사
- [x] EXE/격리 설치·재설치·제거·Windows/Python ZIP·manifest/체크섬
- [x] 빈 프로필·0016 자료 업그레이드/파일·설정 보존·백업·추출 실행 대조
- [ ] 소스 커밋/태그·원격 업로드·초안 자산 대조·정식 공개
- [ ] 공개 다운로드/해시 대조·소유 임시 자료 정리

## 개발 검증 근거

83번의 분리 실행 XML/최신 게이트/Native 보고서 대조에서 고유 1,683개를 확인했다.
단일 전체 실행 통과 수로 주장하지 않는다. 캐시 상한/초기 시간 갱신 회귀 수정과 기존 긴 Qt 실행 대기를 구분한다.
회사 PC 종료 원인 확정·6시간 운영·OS 테마 실시간 전환은 미검증이다.
패키지·새 버전 검사는 아래에 실제 수행 결과를 기록한다. 수행 전 완료로 표시하지 않는다.

## 3.0.14 마감 검사

- 버전/배포 안내·DB 이행·완료 조회 관련 18개 통과 (`artifacts/release-3.0.14-focused.xml`).
- Ruff, mypy 소스 164개, 변경 버전/검증 파일 format, diff 공백 검사 통과.
- 앞선 고유 1,683개는 83번 개발 검사 결과다. 버전·문서·배포 검증기 변경 뒤의 위 검사와 구분하며 전체 재실행으로 표시하지 않는다.
- 실제 Windows 대표 화면의 좁은 창·기본 밝은/어두운 팔레트 주입을 100% 45개/19.48초, 150% 45개/19.63초,
  200% 45개/20.16초로 재검사했다 (`artifacts/release-3.0.14-native{1,150,200}.xml`).
  200% 완료 화면과 150% 최근 메뉴 캡처를 직접 열어 가독성을 확인했다. 실제 OS 테마 자체를 바꾼 검사가 아니다.

## 배포물과 자료 보존

- 기존 `dist`와 `build/release`를 `artifacts/release-before-3.0.14-dist`, `release-before-3.0.14-build`에 보존했다.
- EXE smoke와 VerificationBuild의 격리 설치/재설치/제거·데이터 표식 보존을 통과했다. 실제 OfficeFlow 설치 등록·자동 시작은 변경하지 않았다.
  기록: `artifacts/release-3.0.14-build.log`.
- manifest 875개 파일, Windows ZIP 871개 파일의 크기/해시/CRC를 대조했다. DB/백업/첨부/로그/bytecode는 배포에 포함하지 않는다.
  새 폴더의 추출 EXE를 clean PATH에서 실행하여 빈 DB0017·integrity/FK와 WebP codec을 확인했다.
- 보존한 실제 3.0.13 EXE로 합성 DB0016을 열 수 있음을 확인한 뒤 새 EXE로 0017을 준비했다.
  업무·첨부·보관본·체크리스트·일지·이력·중요시점 행/파일 해시·설정을 대조하고 새 보호 사본과 전체 백업 검증을 확인했다.
  기록: `artifacts/release-3.0.14-bundle-verification.log`.
- Python ZIP payload 196개가 현재 작업 파일과 SHA-256으로 일치한다. 새 시험용 Python 환경에서 추출 소스·메타데이터 3.0.14,
  진단 8/8·실제 smoke·빈 DB0017을 확인했다. 실행 라이브러리는 개발 site-packages를 읽기 참조했고 개발/실사용 설치는 바꾸지 않았다.
  최초 온라인 setup이나 실제 회사 환경 검증은 아니다. ZIP에는 Python/wheelhouse가 없으므로 최초 setup은 실행 라이브러리를 내려받아야 한다.
  기록: `artifacts/release-3.0.14-python-verification.log`.
- EXE 버전 리소스 3.0.14, 설치 리소스 3.0.14.0 확인. 전체 백업 v3 유지, 0017 DB/백업은 3.0.14 이상에서 연다.

| 배포 파일 | bytes | SHA-256 |
| --- | ---: | --- |
| OfficeFlow-3.0.14-Setup.exe | 45,172,380 | `0f38c4bccf8039d8216b84d7e6dd4417b9b270f47818644e7a1982522cbf29d8` |
| OfficeFlow-3.0.14-Windows.zip | 62,567,403 | `9ea3f6448550905377429be59eb33fe6d7aab8ea5b3804a2a6bf0c893cc2050e` |
| OfficeFlow-Python-3.0.14.zip | 599,121 | `1185e979f34c452644b05ea79c856a2358dd8b5e06a4db00748aeb4a80bc5b15` |
| OfficeFlow-Python-3.0.14.zip.sha256.txt | 96 | `7b0f08a8b8d08c714326b8af89509012394ceca3bfef18db8ae36b721c09eb2c` |
| SHA256SUMS.txt | 105,383 | `59b364eabb247547734215129303e6b0d77a23ecc9d9f412e1f2a7e56b5d5c5f` |
| release-manifest.json | 167,409 | `abb5da8032d0d302091e26a0e98edeef14b2921088e3cee52ef6b40d3e75d421` |
