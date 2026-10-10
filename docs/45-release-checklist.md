# OfficeFlow 3.0.9 릴리스 체크리스트

작성일: 2026-10-10. 오늘 완료한 운영 신뢰성·자료 조회의 새 1~3차를 마감/배포한다.
사용자가 EXE/설치파일/ZIP 재생성과 배포를 요청했다. 과거 문서의 미배포 표기는 작성 당시 기록이다.

## 범위와 호환

- 백업 실패 안내·자료 검사·종료/예외 진단(39/40번)
- 보관 자료·메모 검색·백업에서 선택 파일 외부 저장(41/42번)
- 달력/오늘 업무·원래 회차별 일정/알림 묶음·개별 시각 조회(43/44번)
- DB 0015·전체 백업 v3 유지. 3.0.8에서 스키마 변경 없음
- U17 폴더 바로가기 및 U16 외부 업무 묶음/경량화는 후속 계획만 기록; 이번 미포함
- 스마트 목록/관련 업무 추천은 보류, 업무 복사는 사용자 판단으로 제외

## 게이트

- [x] 버전·리소스·도움말·안내·릴리스 노트 3.0.9 일치
- [x] Ruff·mypy·diff 공백 검사
- [x] 전체 단위·통합·성능·UI 회귀 및 좁은 창/대표 배율·palette 검사
- [x] EXE 새 프로필 실행 / 독립 설치 등록의 재설치·제거·자료 보존
- [x] Python ZIP 새 추출·독립 runtime·진단·실행 / Windows ZIP 전체 구성·추출 실행
- [x] 사용자 DB/첨부/백업/로그 제외·모든 산출물 크기·SHA-256 검증
- [x] 커밋·원격 main/태그·GitHub 새 릴리스와 서버 파일 검증

## 결과

Windows / Python 3.12.4 / PySide6·Qt 6.11.2. 실제 사용자 프로필 대신 격리 프로필을 사용했다.

| 검사 | 최종 결과 |
| --- | --- |
| 전체 단위·통합·성능 | 709개 통과 / 179.67초 |
| 전체 UI 재검사 | 365개 통과 / 165.11초 |
| 합계 | 고유 검사 1,074개 통과 |
| Ruff / mypy | 통과 / 124개 소스 검사 |
| diff 공백 검사 | 통과 |

이번 배포 버전에서 다시 실행한 결과다. 3차 구현 시점과 중간 재실행은 합산하지 않는다.
최초 제한 실행은 Windows 파일 resolve/핸들의 샌드박스 접근 거부를 확인하고 중단했다.
권한 확보 후 격리 프로필 전체 검사를 통과했다. 첫 UI 실행의 364통과/1실패는
기존 버전 표기 검사에 남은 3.0.8 기대값 때문이었다. 기대값을 새 버전에 맞춘 최종 전체 UI는 통과했다.

Qt offscreen 자식 프로세스의 Windows 글꼴·밝은/어두운 palette·100/150/200%에서
새 검사/백업/검색/오늘/달력/알림 시각 창의 좁은 크기·배경·접근·페이지를 검사했다.
200% dark 자료 검사/백업 파일 창과 150% dark 달력 캡처를 직접 검토했다.
이는 실제 OS 설정을 live 변경한 검사와 다르다.

## 배포물 검증

- 이전 dist와 build/release는 Git 제외 `artifacts/release-before-3.0.9-dist`,
  `artifacts/release-before-3.0.9-build`에 보존한 뒤 재생성했다. 사용자 자료를 삭제하지 않았다.
- 빌드 로그 `artifacts/release-3.0.9-build.log`: EXE 새 프로필 smoke와 별도 VerificationBuild
  설치/재설치/재실행/제거·표식 유지 검증 통과. 실사용 설치 등록/자동 시작을 변경하지 않았다.
- Python ZIP은 새 폴더로 추출하고 검증 전용 runtime에 연결했다. 추출 소스 import·배포 메타데이터
  3.0.9 일치, 진단 8/8, smoke, 빈 업무/이력/중요 보관, DB0015 integrity/FK 검사 통과.
  실사용 `.venv`는 변경하지 않았으며 이번 ZIP에 오프라인 wheel은 포함하지 않는다.
- manifest 873개 파일의 크기·SHA-256과 체크섬을 대조했다. Windows ZIP 869개 파일도
  실제 EXE 폴더와 1:1 내용 해시가 일치했고 새 폴더로 추출해 clean PATH/독립 프로필 실행을 통과했다.
- 기존 보존 EXE3.0.8로 만든 별도 DB에 합성 업무를 추가한 뒤 EXE3.0.9로 실행했다.
  업무 행·첨부 영역 표식·스키마0015·DB integrity/FK가 유지됐다. 운영 DB 검사가 아니다.
- EXE 리소스3.0.9/설치파일 리소스3.0.9.0 확인. ZIP에 DB·첨부·백업·로그·개발 캐시 없음.
- ZIP 구성 검사 자체의 디렉터리 이름 비교와 Windows 환경키 대소문자를 보완한 최종 검증을 통과했다.
  배포 파일 내용 손상으로 단정한 것은 아니며 최종 로그는 `artifacts/release-3.0.9-asset-verification.log`다.

| 공개 파일 | bytes |
| --- | ---: |
| OfficeFlow-3.0.9-Setup.exe | 44,841,961 |
| OfficeFlow-3.0.9-Windows.zip | 62,225,553 |
| OfficeFlow-Python-3.0.9.zip | 453,754 |
| OfficeFlow-Python-3.0.9.zip.sha256.txt | 95 |
| SHA256SUMS.txt | 105,035 |
| release-manifest.json | 166,919 |

주요 배포 SHA-256:

| 파일 | SHA-256 |
| --- | --- |
| Setup.exe | `fd524783f5800f8bc5df69de2f15e870299da7ea52f8eecf00596314f38bfcf7` |
| Windows.zip | `19311705826889a680d413ebd0a4f93dd00d65b82c21706cc3b3c3fabc4dc884` |
| Python.zip | `0ef94872b01989e264154467f62960cc849014cb9fc21c70dec96b5a08af9010` |

## 공개

- 2026-10-10 12:45:53 KST에 [OfficeFlow 3.0.9](https://github.com/spelld77/officeflow-v3/releases/tag/v3.0.9)를 최신 정식 릴리스로 공개했다.
  release ID `408611492`, draft/prerelease=false, 자산 6개다. 기존 3.0.8 릴리스는 변경하지 않았다.
- 검증된 소스 커밋 `4dd5abb39e92fe9d8ab163e9b9a75f5dab7c5a8d`와 태그 `v3.0.9`를
  원격 main/태그에 atomic push했다. annotated tag의 peeled commit이 위 소스 커밋과 일치함을 확인했다.
- 초안 상태에서 서버 자산 6개의 이름·uploaded 상태·크기·digest SHA-256을 로컬 검증 결과와
  자동 대조한 뒤 공개했다. 초안은 tag 조회 API에서 404여서 목록/릴리스 ID로 확인했다.
- 공개된 Python ZIP/자체 체크섬을 `artifacts/release-3.0.9-download`로 내려받아
  검증한 원본 ZIP과 일치함을 재확인했다. GitHub latest API에서도 v3.0.9와 공개 상태를 확인했다.
- 원격 CI 실행 결과로 보고하지 않는다. 이번 품질 근거는 위 로컬 전체 검사와 배포 검증이다.
- 공개 결과는 문서 전용 후속 커밋으로 main에 반영하며 태그/배포물은 검증된 소스 커밋에 유지한다.
  실사용 설치나 Python 실행 폴더의 교체는 하지 않았다. 다운로드 후 사용자가 업데이트한다.

## 안전과 미검증 환경

실사용 `%LOCALAPPDATA%/OfficeFlow`와 개발 `.local-data`를 검증에 사용하지 않는다.
기존 dist/build 결과는 재생성 전에 별도 보존한다. 설치 검증은 별도 VerificationBuild 등록을 사용한다.
Qt 밝은/어두운 palette와 scale 검사는 OS 테마/DPI를 live 전환한 검사와 다르다.
회사 PC 보안/DRM, 실제 문서 편집기, 장기간 실시간 알림은 별도 사용 확인 대상이다.
44번 보고서에 기록한 한 번의 기존 UI 검사 대기는 원인 해결이 확정되지 않았다.
