# OfficeFlow 3.0.18 릴리스 체크리스트

2026-11-11 사용자 `반영까지 해줘` 요청으로 첨부 드래그 상시 안내 제거·U42 정렬·U43 폴더 복구의
마감·버전·Python ZIP·검증·커밋/main/태그·GitHub 정식 릴리스를 진행한다.
기존 미커밋 변경 전체가 이번 요청의 개선이며 94~96번 결과를 따른다.

## 범위와 보존

- 기존 dist를 `artifacts/release-3.0.18/previous-3.0.17/dist`에 보존했다.
- Python ZIP/체크섬/manifest만 생성한다. EXE·설치본·Windows ZIP은 생성하지 않는다.
- 운영 DB/자료·실행 프로세스·설치/Python 실행 폴더는 교체하지 않는다.
- 합성 DB0017→0018·기존 NULL 연결 보존·전체 백업 v3와 추출본/helper 실행을 추가 검증한다.
- 전체 제품 회귀/대용량 반복은 추가하지 않는다. 전환 전 백업·구버전 완전 종료와 0018 구버전 차단을 안내한다.

## 검증·공개 결과

로컬 패키지 검증 완료. 원격 결과는 공개 완료 후 기록한다.

- 이전 기능 검사 근거: U43 고유 66개, 정렬 3개, 안내 제거 3개. 단일 전체 회귀로 합산하지 않는다.
- 버전/패키지 관련 9개 통과, Ruff·변경 Python 15개 format check·mypy 소스 167개 통과.
- Windows native 100/150/200% 각각 4개 통과(폴더 사용/취소 + 첨부 목록/썸네일 안내 제거).
  밝은 UI/어두운 주입 팔레트이며 실제 OS 테마 전환 검사는 아니다.
  200% 후보 경로 줄바꿈/버튼 및 첨부 안내 제거/하단 버튼 공간을 캡처로 직접 확인했다.
- ZIP payload 200개 소스 바이트/CRC 대조, 추출 패키지/메타데이터 3.0.18과 진단 8/8 확인.
- 빈 DB0018 실행·integrity/FK와 합성 기존 DB0017→0018 이행·스키마 보호 사본 생성 검증 통과.
- 업무/첨부/일지/이력 행, 첨부 파일 5개, 사용자 설정과 정상 종료·전체 백업 v3 검증 통과.
  정상 종료 때 저장되는 UI 상태 4필드는 기존 기대값으로 구분했다.
- 합성 기존 폴더 연결 1개의 여섯 필드를 그대로 보존하고 새 식별 정보가 NULL인지 대조했다.
- 추출 런타임의 실제 helper 프로세스로 폴더 식별/이름 변경/후보 찾기를 검증했다.
  콘솔 없는 pythonw 실행에서도 같은 helper 기능이 통과했다. helper는 앱/DB를 초기화하지 않는다.
- 검증 기록에 비ASCII 경로를 읽는 부분의 기본 cp949 오류가 있었고, UTF-8을 명시해 기록을 완료했다.
  실제 pythonw helper 동작은 처음부터 성공했으며 제품 코드 수정은 없었다.
- 시험 venv는 개발 site-packages를 읽기 참조한다. 최초 온라인 setup/별도 새 PC 설치 검사는 아니다.
- 실제 회사 공유 폴더/DRM·첨부 실제 탐색기 드롭·장시간 운영·전체 제품 회귀는 별도다.

증거: `artifacts/release-3.0.18/`의 packaging.xml, render-1/1.5/2.xml, captures-1/1.5/2,
python-verification.log, python-verification.json, final-assets.json. 운영 프로필은 사용하지 않았다.

## 최종 배포물

| 파일 | bytes | SHA-256 |
| --- | ---: | --- |

| OfficeFlow-Python-3.0.18.zip | 615,154 | `b2d85a8f901d14519f60628b0cbe72cb6b51145621aa714dd472a1fca28fb7a2` |
| OfficeFlow-Python-3.0.18.zip.sha256.txt | 96 | `923bcb79a5f9ca96ff64fa8ea41cbd4fccc81e189f5761a85aaba2d8a1268cbe` |
| release-manifest.json | 541 | `2977ae74b3849fa51c6c233cc30c0a6c165bf13660bb06bfbff9dce9f49a04b5` |
| SHA256SUMS.txt | 217 | `e6bfc0474b40f0230d954433d9cec5ca7f4ff42e8178316418eb1eb9d9987ed9` |

Python ZIP·개별 체크섬·SHA256SUMS·manifest 4개만 배포한다.
