# OfficeFlow 3.0.8 릴리스 체크리스트

작성일: 2026-10-09. 후속 개선 1~3차를 마감하고 설치형·Windows ZIP·Python ZIP을 공개한다.
과거 차수별 문서의 미배포 표기는 작성 당시 기록이며 이번 실제 공개 결과와 구분한다.

## 범위와 호환

- 1차 외부 사본 저장·편집 파일 관측·완료 알림 표시
- 2차 원본/작업본/보관본·이름/메모·보기 사본 보존/버리기·다중 선택/삭제와 안전한 정리
- 3차 업무 변경 이력·비교·중요 보관·보관 파일 활용 및 체크박스 가독성
- DB 0015 / 백업 v3. 기존 ID·경로·내용 보존, 보호 사본 후 이행
- 새 DB/백업은 3.0.8 이상. 이전 버전 사용 시 업데이트 전 전체 백업 필요
- 일정/알림 중복 표시·처리 필요 분류·외부 Git 연동은 보류

## 최종 게이트

- [x] 버전·Windows 리소스·도움말·실행 안내·릴리스 노트 3.0.8 일치
- [x] Ruff·mypy·공백 검사
- [x] unit/integration·UI·performance 전체 회귀
- [x] Qt 스레드/창 닫기 반복 검사와 100/150/200% 밝은/어두운 palette 대표 화면
- [x] EXE 새 프로필 실행 / 격리 설치·재설치·제거와 사용자 자료 보존
- [x] 새 Python ZIP 추출·모듈·진단·실행·빈 0015 DB
- [x] Windows ZIP 전체 폴더 구성·사용자 자료 제외·체크섬
- [x] 커밋·main/태그 업로드·GitHub 릴리스 공개와 6개 자산 검증

## 실제 검사 결과

Windows 11 / Python 3.12.4 / PySide6·Qt 6.11.2. 임시 프로필을 사용한다.

| 범위 | 최종 결과 |
| --- | --- |
| unit + integration | 657 통과 / 170.12초 |
| UI | 322 통과 / 176.61초 |
| performance | 5 통과 / 7.21초 |
| 합계 | 고유 검사 984개 통과 |
| Ruff / mypy | 통과 / 소스 114개 |
| diff 공백 검사 | 통과 |

UI 전체는 시험 위치·로그·faulthandler를 남기는 진단 실행으로 정상 종료했다.
알림 preview 스레드·이력·중요 보관 18개는 별도 프로세스 실행 5회 모두 통과했다
(32.06 / 26.95 / 23.08 / 23.20 / 22.96초). 그중 100회 창 열기/닫기 검사를 매회 포함한다.
반복·집중 검사를 위 고유 검사 합계에 중복 합산하지 않는다.
현재 검사에서 이전 요약 없는 중단은 재현되지 않았으며 원인 해결을 확정한 것은 아니다.

실제 Windows 숨김 renderer의 첨부/보관함/삭제 확인·페이지 이동 화면은
100%·150%·200%와 light/dark palette 6조합 모두 통과했다.
새 이력/보관/비교/중단 5화면은 Windows 글꼴을 사용하는 Qt offscreen renderer의 동일 6조합,
좁은 480×520 logical px에서 좌표·DPR·본문·체크/비활성 표시를 검사했다.
200% dark의 첨부 다중 선택과 중요 보관 체크 대표 PNG도 직접 확인했다.
이는 OS 설정 자체를 live 전환한 검사와 다르다.

빌드 로그: ignored `artifacts/release-3.0.8-build.log`.
이전 dist는 `artifacts/release-before-3.0.8`에 보존했다.
독립 설치 등록으로 설치 → 빈 프로필 실행 → 표식 생성 → 재설치 → 재실행 → 제거 순서에서
표식/데이터 유지와 EXE 제거를 확인했다. 실제 설치 등록/자동 시작은 건드리지 않았다.

새 Python ZIP은 `artifacts/release-3.0.8-extracted`에 풀어 별도 검증용 Python 환경에서
추출된 경로 import·배포 메타데이터 3.0.8·진단 8/8·smoke 실행·빈 업무/이력/보관·
0015 revision·SQLite integrity/foreign-key 검사를 통과했다. 오프라인 라이브러리는 포함하지 않는다.
개발 `.venv`는 새 소스에 계속 연결되어 있으며 실사용 Python 환경은 변경하지 않았다.

873개 산출물의 크기·SHA-256을 manifest/체크섬과 대조했다. Windows ZIP 내부 869개 파일도
각각 대응하는 EXE 폴더 파일과 내용 해시를 대조했고 새 폴더로 풀어 clean PATH로 실행했다.
DB·백업·로그·개발 캐시 제외와 EXE 3.0.8 / 설치 리소스 3.0.8.0도 확인했다.
첫 리소스 검사에서는 Inno Setup의 문자열 뒤 공백 때문에 일치 검사가 실패했다.
실제 버전은 맞았으며 문자열 공백을 제외한 재검사 및 숫자 버전 확인으로 통과했다.

| 공개할 파일 | bytes |
| --- | ---: |
| OfficeFlow-3.0.8-Setup.exe | 44,766,890 |
| OfficeFlow-3.0.8-Windows.zip | 62,152,788 |
| OfficeFlow-Python-3.0.8.zip | 421,584 |
| OfficeFlow-Python-3.0.8.zip.sha256.txt | 95 |
| SHA256SUMS.txt | 105,035 |
| release-manifest.json | 166,919 |

## 실제 공개 결과

- 2026-10-09 22:08:50 KST에 [OfficeFlow 3.0.8](https://github.com/spelld77/officeflow-v3/releases/tag/v3.0.8)을 최신 정식 릴리스로 공개했다. draft/prerelease는 false다.
- 최종 소스 커밋 `500ea2830f33b91d8a0ceb6a216a3f6aca860ee8`, 태그 `v3.0.8`.
  1차 `dd7d5ac`·2차 `3be8a61`과 3차 최종 변경을 모두 포함하고 원격 main/태그를 확인했다.
- 게시 전 허용 경로 검사에서 코드의 `infrastructure/attachments`를 사용자 첨부 경로로 오인해 커밋이 중단됐다.
  다음 업로드가 중간 커밋의 태그를 먼저 생성한 것을 발견했다. 아직 릴리스가 없고 방금 만든 동일 태그임을
  확인한 뒤 조건부로 그 미공개 태그만 회수·재등록했다. 공개 태그는 위 최종 커밋을 가리키며 기존 공개 릴리스는 변경하지 않았다.
- 초안에 6개 파일을 업로드하고 각 파일의 GitHub 서버 digest SHA-256·크기·uploaded 상태를 로컬 파일과 대조한 후 공개했다.
- 공개 Python ZIP과 자체 체크섬을 `artifacts/release-3.0.8-download`로 내려받아 원래 ZIP과 동일한 해시임을 확인했다.
- GitHub Actions 실행 목록은 비어 있었다. 자동 원격 CI 통과로 보고하지 않으며 이번 품질 근거는 위 로컬 검증이다.
- 공개 결과 기록은 문서 전용 후속 커밋으로 main에 반영한다. 릴리스 태그/배포물은 검증된 소스 커밋에 유지한다.

주요 배포 SHA-256:

| 파일 | SHA-256 |
| --- | --- |
| Setup.exe | `ebf9009d2a51714105664754a2fa3995becf14e865bd40440d40696793b9780b` |
| Windows.zip | `9caf7925c9a4e380370fed4f658b65856372f061f2947906a755fc781d55a8c4` |
| Python.zip | `094bb6bd2b0b1d566bbb1aa06b3a0949959cd1354cea5219c985c130051cbf93` |

## 안전과 미검증 환경

실사용 `%LOCALAPPDATA%/OfficeFlow` 및 개발 `.local-data`를 검사에 사용하지 않는다.
기존 로컬 배포 폴더는 빌드 전에 별도 보존한다. 설치 검증은 독립 VerificationBuild 등록을 사용한다.
OS 설정 live 전환·업무 PC DRM/보안·실제 한글/Excel 편집기·장기 사용은 별도 확인 대상이다.

3차 검사 당시 UI 실행 한 번이 요약/stack 없이 중단된 기록은 [36번 보고서](36-follow-up-phase-3-results.md)에 남긴다.
전체 재실행 통과를 그 중단의 원인 해결로 간주하지 않고 최종 진단 로그와 반복 결과를 추가 기록한다.
