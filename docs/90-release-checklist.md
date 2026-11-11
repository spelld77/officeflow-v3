# OfficeFlow 3.0.16 릴리스 체크리스트

2026-11-11 사용자 `깃허브에 반영하는단계까지 진행해` 요청으로 U40 마감·버전·Python ZIP·커밋·
main 반영·태그·GitHub 릴리스 업로드를 진행한다. 기존 사용자 지시대로 Python 전용이며 EXE는 만들지 않는다.
구현/관련 검증은 [89번](89-calendar-refresh-stability-results.md), 안내는
[릴리스 노트](release-notes-3.0.16.md)를 따른다. DB0017·전체 백업 v3를 유지한다.

## 범위와 보존

- 기존 dist 4개를 `artifacts/release-3.0.16/previous-3.0.15/dist`에 복사해 보존한다.
- 실제 사용자 실행 폴더·프로세스·DB/첨부/설정을 변경하지 않는다.
- 추가 검사는 버전/패키지와 추출 패키지 진단·합성 기존 자료 보존을 중심으로 수행한다.
  전체 회귀나 200회 반복을 다시 실행하지 않는다.
- 실사용 전환 직전에 전체 백업·트레이 완전 종료 후 새 버전을 실행한다.

## 진행 상태

로컬 패키지 검증 완료. 원격 공개 결과는 후속 기록한다.

- 버전/패키지 고유 9개 통과: 최초 8개 통과/도움말 버전 검사 1개 실패 후 안내를 3.0.16으로 수정해 해당 1개 통과.
- Ruff·format check, mypy 소스 164개, diff 공백 검사 통과.
- 최종 ZIP 196개 payload의 소스 바이트/CRC 대조 통과. 운영 DB/로그/venv는 포함하지 않는다.
- 추출 패키지/배포 메타데이터 3.0.16, 별도 환경 진단 8/8, 빈 DB0017 smoke/integrity/FK 통과.
- 합성 기존 업무/첨부/일지/이력의 행, 첨부 파일 5개와 사용자 설정 보존, 정상 종료·전체 백업 v3 검증 통과.
- 기존 계약상 정상 종료 때 저장하는 UI 상태 4필드는 기대값을 별도로 대조했다.
- 최초 격리 venv 생성은 기본 임시 폴더에서 ensurepip 실패. 작업 소유 TEMP/TMP를 명시해 새 경로에서 성공했다.
- 개발 site-packages를 읽기 참조하는 새 시험 venv로 검증했다. 최초 온라인 setup/별도 새 PC 설치 검사는 아니다.
- 회사 PC 장시간 사용·실제 Excel/보안 제품 인과·원래 native 잠금 소유자는 미확인이다.

증거는 `artifacts/release-3.0.16/`의 packaging.xml, help-final.xml,
python-verification-final.log, python-verification.json, final-assets.json에 보존한다.

## 최종 배포물

| 파일 | bytes | SHA-256 |
| --- | ---: | --- |

| OfficeFlow-Python-3.0.16.zip | 603,440 | `e31a2426f15da247b4c0687a47c31e6932db244be878a43080d36babc879167a` |
| OfficeFlow-Python-3.0.16.zip.sha256.txt | 96 | `b798ac834efd5379760356ebfde7b313d5ec1ec57cd9bf8a9d399d184212ce18` |
| release-manifest.json | 541 | `42f5ccfc84d0878f120ae22d9b0a45020249ca546912e851106bc4af1c476f08` |
| SHA256SUMS.txt | 217 | `b25e759bcc07a2254f95770add32eb6458a1cff01a0a118da82b04ad88fdf40d` |

Python ZIP·개별 체크섬·SHA256SUMS·manifest 4개만 배포한다. Windows 버전 메타데이터는
정합성만 갱신했으며 EXE/설치본/Windows ZIP은 생성하지 않았다.
