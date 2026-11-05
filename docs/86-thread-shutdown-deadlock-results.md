# U39 종료 교착 개선 구현·검증 결과

2026-11-05 KST. **구현·격리 검증 완료, 실사용 적용·배포·커밋·업로드 전**이다.
기준 소스는 `218908ef55f39193792298391a87f40a9417e025`, 설계는 [85번](85-thread-shutdown-deadlock-design.md)이다.

최종 고유 판정은 **1,693개 통과·1개 환경 조건 제외**다. 단일 전체 실행 완주 결과가 아니다.
전체 시도의 실패 가정을 수정하고 미실행 범위만 이어서 확인한 XML들을 testcase ID로 대조했다.
실패 실행의 부분 통과나 재실행을 중복 합산하지 않았다.

## 구현

- `finish_thread`와 정상 UI의 22개 동기 호출을 제거했다. 종료 중 진행창·타이머·새 연결을 만들지 않는다.
- owner 생성 시 `ShutdownScope`의 타이머/콜백을 준비하고, worker 시작 전에 등록한다.
  타이머가 native wait 성공을 기록한 뒤 GUI 루프로 복귀하고, 다음 tick에서 완료 콜백을 실행한다.
  이때까지 thread/worker/상위 창 수명을 보존한다. QObject.sender 조회·작업별 finished 관찰 연결은 없다.
- 일반 작업은 20ms, 종료 대기는 2ms이며 owner별 join 대기 예산은 5ms다.
  재사용 reader는 persistent 등록으로 유휴 관찰 타이머/반복 wait를 실행하지 않는다.
- `request_shutdown`은 자식·producer 중단/협조적 취소 후 즉시 반환한다. 창은 종료 대기 동안
  새 입력이 차단되며 원래 이벤트 루프로 이동/다시 그리기를 계속 처리한다. 업무 보기와 메인 창은
  기존 상태 영역에 마무리 중임을 표시한다.
- source/LatestJob/이미지의 pending 요청과 늦은 결과를 무효화한다. 미리보기는 native 종료 뒤 다시 열 때 재사용한다.
- 첨부 복사/검증·백업·내보내기·복구·문서 작업·이력·조회와 간접 owner를 같은 완료 계약으로 연결했다.
- 전체 native 종료/finalizer 뒤 cache/maintenance/engine을 정리하고, 성공할 때만 clean marker를 기록한다.
- 명시적 동기 `shutdown()`은 테스트 teardown·aboutToQuit·smoke 같은 마지막 안전망이다.
  새 UI/연결·중첩 루프·processEvents 없이 준비된 취소/quit/join을 사용한다.
- 기존 개발 의존성 `icalendar`가 개발 lock에서 누락돼 전체 검사 수집을 위해 `7.3.0`으로 보완했다.
  runtime lock·DB0017·백업 v3·앱 버전은 유지했다.

## 격리와 환경

- 사용자의 `run-officeflow-python.cmd`와 실행 중인 3.0.14는 실행/교체/종료하지 않았다.
- 개발용 `.venv`만 새로 만들었다. Python 3.12.10, PySide6/Shiboken6/Qt 6.11.2다.
- 테스트 프로세스에는 별도 `OFFICEFLOW_DATA_DIR`을 지정했다. DB/첨부/설정/백업은 합성 tmp_path다.
  배포용 cmd는 데이터 override를 비우므로 테스트에서 사용하지 않는다.
- 큰 기존 fixture의 commit I/O가 느린 실행은 중단하고 시스템 임시폴더의 소유 테스트 경로로 옮겼다.
  운영 프로필을 사용해 속도를 높이지 않았다.

## 확인한 결과

| 검사 | 결과 | 증거 |
| --- | --- | --- |
| 최종 출처 조회 직후 닫기 반복 | 3,000회, 42.515초, exit 0, 잔여 controller 0 | `.test-tmp/shutdown-stress.jsonl` |
| 종료 핵심·백그라운드·하루 조회 | 15개 통과 | `.test-tmp/targeted-final.xml` |
| Qt 팔레트 변경 2건 뒤 복원 시작 | 3개 통과 | `.test-tmp/palette-restore.xml` |
| 최종 타이머 관찰·닫기·첨부 열기 | 20개 통과 | `.test-tmp/polling-final.xml` |
| 이력/보관 창 100회 수명 및 해당 회귀 | 최종 타이머 방식에서 통과 | `full-suite-continuation.xml`의 해당 testcase |
| 짧은 경로의 백업/복원 | 1개 통과 | `.test-tmp/restore-short-path.xml` |
| 전체 범위 최종 고유 판정 | 1,693개 통과·1개 조건 제외, 분할 확인 | `artifacts/u39-shutdown/validation-summary.json` |
| Windows Qt 종료 상태 화면 | 100/150/200% × 밝음/어두운 주입 팔레트, 6개 캡처 성공 | `artifacts/u39-shutdown/closing-*.png` |
| 정적 검사 | Ruff, 변경 주요 파일 format check, mypy 통과 | mypy 소스 164개 |

최초 전체 실행 중 코드 검토로 이력/보관 창의 자식 finished 콜백이 부모 조회를 다시 시작할 수 있음을
새 2개 회귀로 확인했다. 각 override에서 `_closed`를 먼저 설정하고 해당 회귀를 통과했다.
최초 전체는 1,692개를 수집했지만 종료 관찰의 sender() 대기로 중단했다. 이 대기 지점을 수정한 뒤
시작한 전체 범위는 새 2개를 포함한 1,694개다. 첫 전체 시도의 부분 통과 수는 완주로 간주하지 않는다.

전체 범위의 증거는 다음과 같이 합쳤다. 원본 XML과 로그는 `artifacts/u39-shutdown/`에도 보존했다.

| 실행 | 실제 결과 | 후속 처리 |
| --- | --- | --- |
| `full-suite-final.xml` | 863 통과·5 실패, 실패 한도에서 중단 | 826개 미실행·비동기 가정 수정 1개만 이어서 실행 |
| `full-suite-continuation.xml` | 822 통과·4 실패·1 조건 제외, 867 deselect | 중복 통과 범위 재실행 없음 |
| `four-assertions-fixed.xml` | 4 통과 | 고정 날짜 fixture 3건·목록 사용 준비 대기 1건 보완 |
| `async-close-assertions-fixed.xml` | 5 통과 | 첨부·중첩 창·백업의 닫기 완료 대기 보완, 기존 통과 1건과 겹침 |

XML의 `classname + name`으로 중복을 제거하고 보완 후 결과를 우선하면 고유 1,694개에
통과 1,693·제외 1·실패/오류 0이다. 제외는 `test_native_resource_counts_plateau`이며
offscreen 전체 실행에서 별도 native Windows 실행을 요구한다. 화면 캡처와 OS GDI/USER plateau 측정은 다르다.
첫 실패/보완 이유가 있는 검사 외에 통과 범위를 다시 전체 실행하지 않았다.

Windows 캡처는 실제 `windows` Qt 플랫폼의 합성 창이며 활성화를 요청하지 않았다.
밝음/어두움은 테스트 프로세스에 주입한 팔레트다. OS 테마·배율 설정이나 다른 앱을 변경하지 않았다.
100% 밝음과 200% 어두움 캡처를 직접 열어 기존 밝은 배경·종료 문구·비활성 상태의 잘림/대비를 확인했다.
최종 타이머 방식에서 이 두 조건만 다시 캡처했으며 앞선 대응 이미지와 해시가 동일하다.
창 내부 캡처이며 모든 실제 모니터 배치/작업 영역에서의 물리적 창 접근성 검증으로 일반화하지 않는다.

## 실패·중단과 보완

- 최초 작은 회귀의 결과 순서 검사는 receiver 없는 queued lambda를 사용해 값이 전달되지 않았다.
  GUI QObject의 @Slot receiver로 바꿔 실제 전달 계약을 검사했고 통과했다.
- 기존 verification 응답성 검사는 동기 shutdown 중 timer가 동작한다는 예전 가정으로 실패했다.
  정상 UI의 request_shutdown/명시적 완료 대기로 바꿔 통과했다. 동기 fallback에 화면 반응성을 약속하지 않는다.
- 긴 연결 검사와 자원 검사 파일에서 복원 시작 검사 중 native access violation이 각각 발생했다.
  복원 검사 단독은 통과했다. pytest-qt는 close 직후 무조건 deleteLater하며 processEvents만으로
  지연 삭제가 완료되지 않는 점을 확인했다. fixture는 native join 뒤 삭제하고 테스트 사이 DeferredDelete를
  처리하도록 보완했다. 보완 후 팔레트 2건+복원 1건 연결 및 최종 이어진 실행의 해당 파일은 통과했다.
  Python 스택만으로 native 충돌의 DLL/잠금 원인을 확정하지 않는다.
- 마지막 자식 닫기 회귀 2개는 보완 전 실제로 새 조회를 시작해 실패했다. 부모의 종료 상태 설정을
  자식 정리보다 먼저 하도록 두 override를 보완했다.
- 최초 전체 검사에서 `ShutdownScope._observe_finished → QObject.sender()`의 대기를 faulthandler로 포착했다.
  이에 sender 조회/작업별 관찰 연결을 제거하고 native join 뒤 GUI turn을 거치는 타이머 관찰로 교체했다.
  초기 3,000회 성공(9.641초)은 이전 관찰 방식 결과이며, 실제 대기 수정 뒤 최종 3,000회를 다시 실행해 통과했다.
  통과 검사를 이유 없이 반복한 것이 아니라 확인된 결함으로 종료 기반을 수정한 뒤 재검증했다.
- 백업 복원 검사 한 건은 긴 임시 경로의 보기 cache 저널 생성에서 FileNotFoundError가 발생했다.
  종료/보존 코드를 바꾸지 않고 짧은 새 임시 경로로 옮겨 통과했다. Windows 설정을 변경하지 않았다.
- 100회 이력 창 검사는 reject 직후 QThread가 즉시 사라진다는 기존 동기 가정으로 실패했다.
  명시적 종료 완료와 DeferredDelete 처리를 기다리도록 바꿔 실제 잔여 객체/구독자 해제를 검사했다.
- 전체 범위에서 남은 실패 9건은 고정 2026-10-01 fixture와 현재 최근 한 달 필터의 불일치 3건,
  목록 표시 직후 작업까지 끝났다고 보는 가정 1건, 동기 shutdown/close 완료를 가정한 5건이다.
  fixture 날짜를 명시하고 비동기 완료를 기다리도록 보완했다. 실제 DB 정리 전 worker 완료와
  화면 heartbeat·자식/부모 해제 검증은 유지했다. 제품 처리 로직을 바꿔 검사를 우회하지 않았다.
- 창 캡처 스크립트의 최초 파일 직접 실행은 tests 패키지를 찾지 못했다. 모듈 실행으로 바꿔 6개를 완료했다.

중단/충돌 실행의 부분 통과 수를 완주 검사 결과나 고유 검사 수에 더하지 않는다.

## 한계와 인계

로컬 브랜치는 `codex/fix-thread-shutdown`이다. 커밋·푸시하지 않았다.
소유 합성 fixture 디렉터리 18개와 작업용 변경 스크립트를 경로 확인 후 정리했다.
XML·로그·PNG·고유 판정 JSON·전체 실행 입력/최종 소스 hash manifest는 `artifacts/u39-shutdown/`에 보존했다.
개발 가상환경은 유지한다. 전체 수집 뒤 제품 변경은 업무 보기의 기존 상태 영역에 닫기 안내를 넣은 것뿐이며
해당 닫기 회귀로 확인했다. 그 외 보완은 테스트의 날짜/비동기 완료 가정이다.

실사용 DB를 연 검증·회사 PC 장시간 사용·실제 OS 테마 변경·설치본/EXE/ZIP은 이번에 수행하지 않았다.
별도 native GDI/USER/handle plateau 검사는 이번 마감 범위에서 수행하지 않았다.
반복 성공은 오늘 확인된 종료 경로의 회귀 증거이며 모든 Qt/PySide native 오류가 사라진다는 보증은 아니다.
사용자 사용 확인·실사용 적용·배포·커밋·업로드는 다음 단계다.

## 후속 로컬 배포 마감 승인 (2026-11-05)

후속 사용자 요청으로 3.0.15 배포물 생성·격리 검증·소스 커밋을 진행했다.
이후 지시로 EXE/설치본/Windows ZIP은 중단하고 압축 해제해 쓰는 Python ZIP만 마감한다.
이 문서의 미배포/미커밋 표현은 개발 검증 당시 상태다. 최신 마감 상태는
[87번](87-release-checklist.md)을 따른다. 실사용 폴더/자료와 GitHub 공개는 별도다.
