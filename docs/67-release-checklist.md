# OfficeFlow 3.0.12 릴리스 체크리스트

작성일: 2026-10-15. 사용자 사용 확인 후 문서·ZIP·커밋·GitHub 공개 요청으로 마감한다.
상태: 로컬 문서·배포물 생성/검증 완료, 원격 게시 준비. 공개 완료 뒤 이 문서에 결과를 추가한다.

## 범위와 안전

- U24 첨부 드롭/버튼, U25 달력 미완료 우선, U27-1~4 첨부 이미지/입력, U28 업무 보기/첨부 클릭, U29 날짜 도움말.
- 달력 알림 선택 스크롤 및 업무 보기 생성 중 작은 설명/안내 창 깜빡임 보완.
- DB0016·전체 백업 v3 유지. 새 DB 이행·실사용 자료/실행 폴더 교체·U26 날짜 간편 이동·장기 자료 정리는 하지 않는다.
- 기존 생성 배포물을 별도 보존하고 3.0.12 EXE/설치본/Windows ZIP/Python ZIP/체크섬/manifest를 만든다.
- 이미 공개한 3.0.11 릴리스는 변경하지 않는다. 회사 보안 정책을 우회하지 않는다.

## 게이트

- [x] 사용자 사용 확인 및 문서/빌드/커밋/업로드 승인
- [x] 버전·리소스·도움말·README·CHANGELOG·릴리스 노트 정리
- [x] 전체 단위·통합·성능·UI, Ruff/mypy, 변경 파일 format
- [x] Windows 대표 화면 100/150/200%·밝은/어두운 팔레트 검증
- [x] EXE 새 프로필/기존 DB·파일·설정·백업 호환, 실제 packaged WebP codec 실행
- [x] 격리 설치·재설치·제거·자료 보존
- [x] Python ZIP 새 추출·격리 실행기·진단·smoke
- [x] Windows ZIP 파일/CRC/해시·추출 실행·배포 사용자 자료 제외
- [ ] 커밋·원격 main/태그·GitHub 공개 자산/다운로드 대조
- [x] 이번 작업 소유 임시 자료 정리

## 검증 결과

Windows 11 / Python 3.12.4 / PySide6·Qt 6.11.2. 작은 합성 자료와 격리 프로필을 사용한다.
최종 검사·패키지·공개 결과는 아래에 마감 시 기록한다. 원격 CI 성공으로 대신 보고하지 않는다.

| 검사 | 결과 | 기록 |
| --- | --- | --- |
| 단위·통합·성능·UI 전체 | 1,485 통과 / 428.43초, 실패·오류·skip 0 | `artifacts/release-3.0.12-all.xml` |
| Windows 대표 화면 | 100/150/200% 각 26 통과, 78 실행 / skip 0 | `artifacts/release-3.0.12-native-{1,1.5,2}.xml` |
| Ruff / mypy / 변경 format / diff | 통과 / source 143개 / Python 39개 / 통과 | 전체 source/tests 및 Python ZIP 검증기 |

Windows 재검사는 전체 검사와 중복이므로 고유 검사 1,485개에 더하지 않는다.
새 WebP smoke는 QImageWriter로 작은 합성 이미지를 메모리에 쓰고 QImage로 읽으며 사용자 파일을 사용하지 않는다.
검증 준비 중 Qt save의 runtime/stub format 표기 차이를 확인해 명시적 writer로 보완했고,
인코더/디코더 실패 검사와 최종 전체 검사에 포함했다. 이는 일반 실행/첨부 정책 변경이 아니다.

### 패키지와 호환

- 이전 `dist`, `build/release`는 `artifacts/release-before-3.0.12-dist`, `artifacts/release-before-3.0.12-build`에 보존했다.
- EXE 새 프로필 smoke와 격리 VerificationBuild의 설치·재설치·제거·자료 표식 보존 통과.
  실제 설치 등록/자동 시작은 변경하지 않았다. 기록은 `artifacts/release-3.0.12-build.log`다.
- manifest 874개 및 Windows ZIP 870개 파일의 크기·SHA-256/CRC를 1:1 대조했다.
  새 ZIP 추출 EXE를 clean PATH에서 실행해 빈 DB0016·integrity/FK 및 메모리 WebP codec 동작을 확인했다.
- 보존한 3.0.11 EXE로 격리 합성 프로필을 만들고 업무·원본/작업본·보관본·체크리스트·일지·이력·중요 시점·파일·설정을 준비했다.
  새 EXE 이후 행/파일 해시·백업/매시 설정·integrity/FK 보존과 전체 백업 검증 통과.
  기록은 `artifacts/release-3.0.12-bundle-verification.log`다. 3.0.11에서 새 DB 이행은 없다.
- Python ZIP을 새 경로에 풀고 테스트 전용 별도 Python 실행기에서 추출 소스/메타데이터 3.0.12·진단 8/8·smoke를 확인했다.
  빈 DB0016·integrity/FK도 통과. 실행 라이브러리는 개발 환경의 site-packages를 읽기 참조하며 개발/실사용 환경의 패키지는 교체하지 않았다.
  온라인 최초 setup 및 회사 PC 검증은 아니며 Python/오프라인 wheel은 ZIP에 없다.
  최종 UTF-8 기록은 `artifacts/release-3.0.12-python-verified.log`다.
- 검증 실행기 생성은 제한된 환경의 ensurepip 실패 후 해당 테스트 경로만 일반 권한으로 재준비했다.
  첫 Python ZIP 검사도 통과했으나 바깥 콘솔 인코딩이 맞지 않아 새 경로에서 UTF-8 기록으로 재검증했다.
- EXE 리소스 3.0.12·설치 리소스 3.0.12.0. 배포물에 사용자 DB·첨부·백업·로그·개발 bytecode/cache가 없다.
- Python ZIP의 소스·실행기·버전/안내 파일 174개를 현재 작업 파일과 SHA-256으로 대조했다. 패키지와 커밋 대상 코드가 일치한다.
- 대표 150% dark 업무 설명과 200% 긴 완성 이름 캡처를 직접 열어 글자/배경·한 줄 줄임을 확인했다.
  팔레트 주입을 실제 OS 테마/DPI live 전환과 혼동하지 않는다.

| 공개 예정 파일 | bytes | SHA-256 |
| --- | ---: | --- |
| OfficeFlow-3.0.12-Setup.exe | 45,030,469 | `949849ceea9fe125f76e4e73d776a8d86d9dd1e1508657d87d065815881a545a` |
| OfficeFlow-3.0.12-Windows.zip | 62,413,264 | `a49dd58080a4f4d1335831633d322ab2cfced8087d32dc7748d205fe6f732005` |
| OfficeFlow-Python-3.0.12.zip | 536,217 | `b060fb274a0b4157d7fb3a44c5621f9e5035909b00f2e9496694e79ae2535aef` |
| OfficeFlow-Python-3.0.12.zip.sha256.txt | 96 | `298775c3faada165c0de7dcd1195a9a8b769c51c8d71670167c477c394975534` |
| SHA256SUMS.txt | 105,208 | `0e8619c27a91535bb347acb8dcbbed544bef4411bd33d0ebafbf2c5056faae06` |
| release-manifest.json | 167,164 | `b89eec4c9ac0515e1cbcb785f846adbd91acd3621ec7299602110baaf2070003` |

### 임시 자료 정리

전체 테스트/새 추출/독립 실행기의 이번 작업 소유 경로 8개를 절대 경로와 reparse point 확인 후 정리했다.
보안 검사 fixture의 Junction 하나는 내부 연결 대상 확인 후 링크 자체만 먼저 제거했다.
논리 크기 합계 11,977,310,338 bytes이며 sparse 테스트 파일 때문에 실제 확보 공간으로 해석하지 않는다.
정리 대상 잔여 0개, XML·로그·소형 캡처·검증 코드·이전 배포 보존본·최종 dist는 유지했다.
실제 사용자 자료나 다른 작업의 폴더를 정리하지 않았다. 합성 fixture는 다시 만들 수 있다.

## 업데이트와 미검증 환경

전체 백업 후 트레이까지 종료한다. Python형은 새 ZIP을 별도 폴더에 풀고 setup을 한 번 실행한다.
동일한 기본/직접 지정 데이터 경로면 DB·첨부 복사는 필요 없다. 3.0.10/3.0.11과 DB0016·백업 v3가 같다.
구버전 복귀에는 업데이트 전 자료가 필요하며 실제 회사 DRM/문서 편집기·장기간 운영·OS 테마/DPI live 전환은 별도 확인이다.
현재 설치/실사용 Python 실행 폴더는 교체하지 않는다.
