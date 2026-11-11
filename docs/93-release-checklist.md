# OfficeFlow 3.0.17 릴리스 체크리스트

2026-11-11 사용자 `깃허브 반영까지 진행해` 요청으로 U41의 버전·Python ZIP·관련 패키지 검증·
커밋·main 반영/태그·GitHub 정식 릴리스를 진행한다.
[92번 구현 결과](92-attachment-drag-copy-results.md)와 [릴리스 노트](release-notes-3.0.17.md)를 따른다.

## 범위와 보존

- 기존 dist 4개를 `artifacts/release-3.0.17/previous-3.0.16/dist`에 먼저 보존했다.
- EXE·설치본·Windows ZIP은 생성하지 않는다. Windows 버전 메타데이터의 정합성만 갱신한다.
- 실제 사용자 실행 폴더·프로세스·DB/첨부/설정·DB0017/전체 백업 v3를 유지한다.
- 이전 고유 관련 81개를 근거로 패키지 관련 검사와 대표 렌더링을 추가한다. 전체 회귀/대용량 반복은 재실행하지 않는다.
- 실사용 전환 직전에 전체 백업·트레이 완전 종료 후 새 버전을 실행한다.

## 검증·공개 결과

로컬 패키지 검증 완료. 원격 공개 결과는 완료 후 별도 기록한다.

- 버전/패키지 관련 9개 통과(packaging.xml), Ruff·변경 Python 7개 format check·mypy 소스 166개 통과.
- 최종 ZIP payload 198개 소스 바이트/CRC 대조, 추출 패키지/메타데이터 버전 3.0.17 확인.
- 별도 시험 환경 진단 8/8, 빈 DB0017 실행·integrity/FK 검사 통과.
- 합성 기존 업무/첨부/일지/이력의 행·첨부 파일 5개·사용자 설정 보존, 정상 종료·전체 백업 v3 검증 통과.
  정상 종료 때 기존 계약대로 저장하는 UI 상태 4필드는 기대값으로 별도 대조했다.
- Windows native 150/200% 각각 준비 상태 렌더링 2개 통과. 기존 100% 캡처와 함께 보존했다.
  200% 어두운 주입 팔레트·썸네일 캡처에서 목록 위 안내가 읽히는 것을 직접 확인했다.
  작은 창 하단은 기존 내부 스크롤로 접근한다. 실제 OS 테마 전환 검사는 아니다.
- 시험 venv는 개발 site-packages를 읽기 참조한다. 최초 온라인 setup/새 PC 전체 설치 검사는 아니다.
- 실제 탐색기/바탕화면 드롭·회사 DRM/보안 환경·장시간/대용량·전체 제품 회귀는 미검증으로 유지한다.
  새 기능의 고유 관련 81개와 실패/중단 경위는 92번의 근거를 사용한다.

증거: `artifacts/release-3.0.17/`의 packaging.xml, render-1.5/2.xml, captures-100/1.5/2,
python-verification.log, python-verification.json, final-assets.json. 운영 프로필은 사용하지 않았다.

## 최종 배포물

| 파일 | bytes | SHA-256 |
| --- | ---: | --- |

| OfficeFlow-Python-3.0.17.zip | 609,417 | `09dc773e4a6c9d80fd8a2e4f1b31b9caec7c3203bfdeab1f28d0281c820e17a6` |
| OfficeFlow-Python-3.0.17.zip.sha256.txt | 96 | `d2ffac3a04073eec2b66c80c55255c87dc3cd08bc36b18af34ec0459d6c67cb3` |
| release-manifest.json | 541 | `bd517fdf49f64de51a2da807c30d8ca27a0312469ef0b146adac435dc22ee0ea` |
| SHA256SUMS.txt | 217 | `161c131bd9617a6adc9d411497fa616247571222f191c0d1cff8b07bb24b0572` |

최종 dist에는 Python ZIP·개별 체크섬·SHA256SUMS·manifest 4개만 있다.
