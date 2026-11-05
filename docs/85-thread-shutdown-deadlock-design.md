# 창 닫기·작업 스레드 종료 교착 개선 설계

작성일: 2026-11-05 KST. 상태: **후속 구현·격리 검증 완료, 실사용 적용·배포 전**.
대상: U39. 최초 요청은 최신 Git 확보·설계이며, 후속 `구현과 테스트를 진행해`로 구현·격리 검증을 승인받았다.
실제 실행 결과와 미검증은 [86번 결과](86-thread-shutdown-deadlock-results.md)를 따른다.
기준: `origin/main` / `218908ef55f39193792298391a87f40a9417e025`, OfficeFlow 3.0.14.
빈 개발 폴더에 `https://github.com/spelld77/officeflow-v3.git`을 clone했다.

## 1. 문제와 증거의 경계

오늘 「Officeflow 응답 없음 원인 찾기」 조사에서 다음 경로를 확인했다.

```text
업무 보기 닫기 → TaskRecordsDialog.reject
  → AttachmentSourceController.shutdown → finish_thread
  → QProgressDialog 생성 → QObject 연결 잠금 대기
```

- 환경: Python 3.12.10, PySide6/Shiboken6 6.11.2, OfficeFlow Python 3.0.14.
- UI는 GIL을 가진 채 Qt mutex를, 작업 스레드는 같은 mutex를 가진 채 GIL을 기다렸다.
  표준 정지/문맥 수집/재개 절차로 다시 수집한 스택에서도 같은 잠금 주소가 관측됐다.
  전체 스레드를 동시에 정지한 원자적 스냅샷은 아니며 정확한 Python worker 인스턴스는 식별하지 않았다.
- 별도 합성 재현은 원본 종료 코드의 1,140번째 반복에서 멈췄다. 이때 대기 지점은
  진행창 생성 다음의 `timer.timeout.connect`였다. 재현의 native 잠금 주소 쌍까지 확인하지는 않았다.
- 새 Qt UI/연결 생성을 제외한 대조 시험은 10,000회 완료했다. 대조의 `wait(2000)`은 진단용이며
  화면 반응성·느린 파일 작업·자료 보존까지 검증한 제품 수정이 아니다.
- 당시 메모리 부족·파일 복사 정체를 뒷받침하는 증거는 없었다. 과거의 다른 종료 원인까지 설명하지 않는다.

원자료는 이전 조사 폴더의 [원인분석.md](<C:/Users/user/Documents/Codex/2026-11-05/new-chat/outputs/officeflow-diagnosis/원인분석.md>)에 있다.
이 링크는 해당 PC의 조사 자료다. 증거 원문·프로세스 메모리를 저장소에 복사하지 않는다.

최신 코드에도 `background.py:38`의 `finish_thread`가 진행창·이벤트 루프·타이머를 만들고
종료 중인 스레드에 새 신호를 연결한다. 호출은 **18개 파일의 22곳**이다.
동일 증상이 22곳 모두에서 실제 관측됐다는 뜻은 아니다.

## 2. 선택한 해결 방향

**닫기 요청 → 새 작업 차단·협조적 취소 → 원래 GUI 이벤트 루프로 복귀 → native 종료 확인 → 창 닫기**로 바꾼다.

| 대안 | 판단 |
| --- | --- |
| 진행창 생성만 제거 | 재현에서 멈춘 타이머 연결 등 공통 경로가 남으므로 제외 |
| 타임아웃 뒤 참조 해제 / 강제 terminate | 작업 중인 DB·파일·QObject 소유권을 잃으므로 제외 |
| 긴 동기 wait로 대체 | 화면·사용자 확인 요청을 막으므로 정상 종료 방식으로 제외 |
| processEvents / 중첩 QEventLoop 대기 | 닫기/완료 콜백 재진입을 만들므로 정상 경로에서 제거 |
| 시작 전에 종료 관찰을 준비하고 비동기 종료 | 채택. 작업·소유자 수명과 최종 DB 정리까지 함께 이전 |
| PySide 버전 변경 / Rust 전환 | 이번 변경에 묶지 않음. 런타임 문제 잔존 시 별도 재현으로 판단 |

목표는 확인된 종료 경로의 위험한 겹침을 없애는 것이다. 모든 PySide 내부 잠금 교착의 제거를
보장하지 않는다. 변경 후 같은 환경의 반복 시험을 구현 완료의 필수 조건으로 둔다.

## 3. 유지할 계약

- 사용자는 `run-officeflow-python.cmd`로 3.0.14를 사용 중이다. 배포용 cmd는
  `OFFICEFLOW_DATA_DIR`을 지워 운영 프로필을 사용하므로 개발/테스트에서는 실행하지 않는다.
  테스트 앱의 자식 환경에 소유한 격리 프로필 경로를 명시하고 `AppPaths.root`를 확인한 뒤
  초기화한다. DB·설정·첨부·백업·로그·lock 모두 격리하며 중복 실행 식별자도 경로에 따라 분리된다.
  운영 데이터/실행 환경·현재 프로세스는 변경하지 않는다. 폴더만 분리한 것을 데이터 격리로 간주하지 않는다.

- DB0017·백업 v3·파일 보존·취소/커밋 경계를 유지한다. 취소 뒤에도 이미 확정된 결과는 보존한다.
- 원본/작업본/보관본/보기 사본·복구 저널·외부 편집 파일을 종료 편의를 위해 삭제하지 않는다.
- 미저장 입력의 기존 확인/보존 규칙을 먼저 적용한다. 닫기 취소를 선택했다면 종료를 시작하지 않는다.
- 알림 확인과 업무/회차 완료의 의미, 원래 회차 식별자를 유지한다.
- QWidget 조작은 GUI 스레드에서, worker의 스레드 전용 세션/자원 정리는 해당 스레드에서 수행한다.
- 실행 중인 QThread·worker의 강한 참조와 상위 QObject 수명을 native 종료까지 유지한다.
- 닫기 요청/종료 대기 중 새 Qt 객체·타이머·연결을 추가하지 않고 대량 disconnect도 하지 않는다.
  연결·취소 핸들·완료 콜백은 작업 시작 전에 준비한다.
- `thread.finished → worker.deleteLater` 표준 패턴은 유지한다. 추적 QThread 자체의
  `finished → thread.deleteLater`는 native join 후 관리자가 삭제하도록 이전한다.
- 종료 진입 후 늦은 성공/실패 신호가 새 팝업·새 조회·버튼 재활성화를 만들지 않는다.
  결과 확정/자료 보존은 화면 갱신 차단과 별개로 처리한다.

[24번 보존 계약](24-follow-up-common-design.md), [38번 신뢰성 계약](38-reliability-common-design.md),
[80번 자원 공통설계](80-resource-and-task-finding-common-design.md),
[81번 수명 상세설계](81-resource-lifecycle-detail-design.md)의 보존 원칙을 유지한다.
구현 시 동기 shutdown 반환을 완료로 보던 호출을 명시적 요청/native 완료 계약으로 이전한다.

## 4. 공통 종료 기반

### 4.1 책임과 API

`presentation/background.py`의 `ShutdownScope`가 소유권과 종료 관찰을 함께 담당한다.
초안의 별도 coordinator는 화면별 scope로 합쳤다. 독립 대화상자도 단독으로 안전하게 종료하고,
생성자 인자와 전역 registry를 늘리지 않기 위해 기존 QObject 부모·자식 소유 관계를 사용한다.
각 owner 생성 시 scope·관찰 QTimer·완료 콜백을 준비하며 worker 시작 전에 등록/연결을 마친다.

- `scope.track(thread, worker, completed, cancel)`: 작업 시작 전에 등록한다.
  QThread별 항목으로 강한 참조와 완료/취소 콜백을 보관하며 각 controller의 기존 세대 토큰을 유지한다.
  thread 전용 finalizer는 해당 worker의 종료 연결에 미리 준비한다.
- `scope.request_stop()`: 멱등. 새 요청을 막고 pending 조회를 버리며 협조적 취소를 요청한다. 즉시 반환한다.
- `scope.is_stopped`와 생성 시 지정한 완료 콜백: 하위 scope까지 종료/정리가 끝나야 완료다.
  부모는 자식의 종료 API를 먼저 호출하며, scope도 남은 하위 참여자의 종료를 요청한다.
- scope는 기존 타이머로 완료를 확인하고 native join 후 소유자의 완료 콜백을 실행한다.
  닫기 시 새 `QTimer.singleShot`·lambda 연결·진행창을 생성하지 않는다.
- 최종 구현은 `QObject.sender()`나 QThread별 finished 관찰 연결을 사용하지 않는다.
  scope 타이머가 등록된 작업의 native wait를 확인하고, 결과 이벤트를 처리할 GUI turn을 돌려준 뒤
  다음 timer tick에서 완료 콜백을 실행한다. 종료 관찰 자체의 Qt 연결 잠금 경합을 줄인다.
- 완료 후 등록을 제거해 owner·worker·콜백 참조를 돌려준다. 대기를 영구 참조 누적으로 바꾸지 않는다.

명칭은 구현 시 기존 코드와 대조하되 계약은 유지한다. 현재 `shutdown()`의 반환 의미만 조용히
바꾸지 않는다. 호출을 `request_shutdown()`과 완료 관찰로 명시적으로 이전하고,
반환 직후 참조를 지우거나 DB를 닫던 코드도 함께 수정한다.

### 4.2 상태와 native 종료

```text
OPEN → STOP_REQUESTED → JOINING → STOPPED → 소유자 닫기/삭제
```

- 완료 콜백은 한 번만 실행한다. 두 번 닫기·Esc·부모 닫기·앱 종료는 같은 요청으로 합친다.
- 작업 세대별로 등록한다. 이전 완료가 새로운 작업의 `_thread`를 지우지 못하게 한다.
- `finished`나 `isRunning() == False`만으로 확정하지 않는다. coordinator가 `wait(1)`의
  성공을 확인한 뒤에만 QThread 삭제·wrapper 참조 해제를 허용한다.
- 일회성 작업의 관찰은 20ms, 닫기 중에는 기존 타이머를 2ms로 조정하며 owner별 join 대기 예산은 5ms다.
  미완료면 원래 이벤트 루프로 돌아간다. 이 예산에 실제 완료 후 UI 콜백 시간까지 포함된다고 보장하지 않는다.
- 재사용 reader는 `persistent=True`로 등록해 유휴 중 관찰 타이머/반복 wait를 실행하지 않는다.
  닫기 요청 때만 quit와 관찰을 시작한다.
- native wait 성공을 기록한 tick에서 즉시 UI 완료를 실행하지 않는다. worker 결과가 먼저
  처리될 수 있도록 원래 GUI 루프로 복귀한 뒤 다음 tick에서 정리한다. 이벤트 루프 종료 fallback은
  결과 UI 신호 전달을 기다리지 않고 직접 join한다.
- PySide `wait`가 GIL을 내놓아 worker Python 정리를 진행시키는지는 대상 런타임의 별도
  자식 프로세스로 검증한다. Qt C++ 문서만으로 바인딩 동작을 보증하지 않는다.
- 작업이 없는 scope는 즉시 완료 가능하다. 등록/연결/시작은 한 GUI turn에서 완료하고
  시작 전 등록과 실제 시작된 작업을 구분해 조기 완료 오인을 막는다.

종료 owner의 추가 작업과 새 UI 생성을 막는다. 다른 활성 화면의 메뉴/새 창 생성과 겹치는
시험도 포함한다. 여전히 교착이 나오면 해결 완료로 표시하지 않고 런타임 재현을 분리한다.

### 4.3 worker별 종료

| 종류 | 종료 요청 | 완료 전 유지할 것 |
| --- | --- | --- |
| 출처/샘플/LatestJob 등 재사용 reader | pending/결과 세대 무효화, quit 요청. 실행 중 조회 반환 대기 | lookup 서비스·worker·owner |
| 복사/내보내기/백업/삭제·복구 | 기존 Event 취소, 안전한 커밋/정리까지 진행 | DB·파일 게이트·영수증·확인 응답 객체 |
| 이미지/썸네일 | 대기 큐 제거, 활성 요청 협조적 취소, 새 decode 금지 | 활성 이미지/worker·클라이언트 정보 |
| QThread 하위 클래스의 일회성 run | 지원하는 작업만 취소, run 반환/native join 관찰 | worker·입력·결과/예외 |

`quit()`만으로 실행 중인 파일/DB 함수를 중단할 수 있다고 가정하지 않는다.
worker 완료에서 thread-safe `thread.quit`로 가는 연결은 GUI 종료에 의존하지 않게
시작 전에 직접 연결을 검토/적용한다. UI 슬롯을 DirectConnection으로 바꾸지 않는다.
LatestJob sampler `close` 등 스레드 전용 finalizer는 해당 실행 스레드에서 정확히 한 번 수행한다.

## 5. 업무 보기 창 흐름

1. `reject`·`closeEvent`·상위 종료를 한 게이트로 합친다. 미저장 입력 확인 후 종료 상태부터 설정하고
   새 조회·드롭·파일 작업·지연 refresh를 막는다.
2. import/verification의 `_close_when_*`가 각자 창을 먼저 닫지 않게 창 전체 scope로 합친다.
   한 worker가 끝났다고 `QDialog.reject(self)`를 직접 호출하지 않는다.
3. 출처·샘플·폴더·문서 작업·내보내기·이미지/썸네일·첨부 복사·검증 등 해당 창의 모든 참여자에
   종료를 요청한다. 공유 coordinator는 해당 창의 클라이언트를 해제하고 앱 종료에서 전체를 정리한다.
4. 대기 중 `closeEvent`는 `event.ignore()`로, `reject`도 즉시 닫지 않고 반환한다.
   이동/다시 그리기는 유지하고 새 변경 작업은 막는다. 기존 상태 영역에
   `작업을 마무리한 뒤 닫습니다…`를 표시한다. 새 진행창은 만들지 않는다.
5. 늦은 신호는 세대/종료 상태를 검사한다. 확정된 저장·첨부 결과를 취소로 위장하거나 삭제하지 않는다.
6. 모든 참여자의 join/후처리 완료 후 한 번만 실제 닫기를 실행한다. 이후 기존 관리자 삭제/구독 해제를 한다.
   완료 전 owner의 `deleteLater`·부모 삭제를 금지한다.

`ImagePreviewDialog.done` 등 `done/accept` 경로도 같은 닫기 게이트를 쓴다.
상위가 자식에 close/reject를 요청한 사실을 자식 종료 완료로 취급하지 않는다.

## 6. 앱 전체 종료와 DB 정리

현재 `_quit_application`은 close 직후 `application.quit()`을 호출하고 `aboutToQuit`에서도
`window.shutdown()`을 호출한다. 창만 비동기로 바꾸면 worker보다 이벤트 루프/DB가 먼저 닫힐 수 있다.

1. 종료 메뉴·트레이 종료·메인 창 실제 닫기를 앱 종료 요청으로 합친다. 트레이 숨기기는 기존 동작을 유지한다.
2. 앱 종료 상태를 먼저 설정하고 자동 백업/알림 폴링/조회 등 새 생산자를 멈춘다.
   미처리 알림·다음 예약은 보존하며 임의로 완료 처리하지 않는다.
3. 창/컨트롤러/공유 이미지 coordinator의 종료 scope를 모아 취소·join을 시작한다.
   한 자식을 기다리기 전에 다른 자식에도 취소가 전달되게 요청 단계와 대기 단계를 분리한다.
4. 마지막 창이 사라져도 조기 quit하지 않게 `quitOnLastWindowClosed`를 제어한다.
   정상 요청에서는 전체 완료 전 `QApplication.quit()`을 호출하지 않는다.
5. 전체 native 종료와 worker finalizer 뒤 설정 저장 및
   `view_cache.close → maintenance.close → engine.dispose`를 한 번만 수행한다.
   이후 실제 창/트레이를 닫고 앱을 종료한다.
6. 정상 종료 표시는 정리 성공 시에만 기록한다. `main.py` 정리 finally에서
   `on_clean_shutdown()`을 무조건 호출하는 구조는 성공 경로로 옮긴다.
   실패는 내용 없는 오류 유형을 기록하고 정상 종료로 위장하지 않는다.

`aboutToQuit`는 정상 비동기 종료의 시작점 대신 최종 안전망으로 둔다.
우회 quit/OS 세션 종료 등 이벤트 루프 종료가 시작된 경우에는 협조적 취소/quit 뒤
**새 UI·Qt 연결·중첩 이벤트 루프·processEvents 없이** 등록 스레드를 join하는 보수적 fallback을 둔다.
이 예외 경로는 화면 반응성을 보장하지 않는다. worker가 GUI 질문에 묶이지 않게 기존 응답을
취소로 해제하고, quit와 스레드 finalizer가 GUI 큐를 기다리지 않게 시작 전에 준비해야 한다.
OS 강제 종료까지 정상 마무리를 보장하지 않는다. 미완료/실패 시 clean marker를 쓰지 않는다.

DB 준비 창은 이미 생성한 UI와 연결로 native join까지 닫힘 완료를 보류한다.
종료 뒤 `finish_thread`로 새 보조 진행창을 만들지 않는다. 기존 시작용 `QDialog.exec()`와
종료를 기다리기 위한 새 중첩 루프는 구분한다.

## 7. 직접 호출 22곳 이전 목록

줄 번호는 기준 커밋이다. 각 단위는 호출 삭제뿐 아니라 시작/완료 연결·부모 닫기·테스트의 동기 가정도 이전한다.

| 파일 (`src/officeflow/presentation/`) | 현재 줄 | 핵심 |
| --- | --- | --- |
| `attachment_browser.py` | 161 | 출처 reader, late result 무효화 |
| `attachment_export.py` | 337 | 확인 응답 취소·사본 보존·QThread 조기 삭제 이전 |
| `attachment_recovery.py` | 151 | 복구/동의창과 부모 수명 |
| `attachment_search_page.py` | 551 | 검색 QThread와 새 검색 차단 |
| `attachment_thumbnails.py` | 423 | 공유 coordinator·활성 decode |
| `backup_files_dialog.py` | 417 | worker 종료 후 백업 session 닫기 |
| `database_upgrade.py` | 64 | 준비 UI·native join·예외 전달 |
| `data_dialog.py` | 1403 | 내보내기/검사 자식·복원/백업 보존 |
| `data_inspection.py` | 171 | 취소·join·결과 UI 차단 |
| `day_controller.py` | 90 | 하루 조회·pending 취소 |
| `document_actions.py` | 420 | 보조창 보유·정상 완료의 wait/새 버튼 연결도 검토 |
| `folder_links.py` | 281 | 폴더 조회 중 부모 닫기 |
| `image_attachments.py` | 346, 472 | 미리보기 done·이미지 가져오기 |
| `latest_job.py` | 98 | 최근/완료/자원 sampler 등 간접 사용자·finalizer |
| `main_window.py` | 4135, 4146 | 자동 백업/내부 점검·전체 완료 barrier |
| `record_dialog.py` | 2171, 2560, 2570 | 샘플·검증·첨부 복사·창 전체 게이트 |
| `reminder_preview.py` | 246 | 미리보기와 알림/달력 상위 수명 |
| `task_history_dialog.py` | 210 | 이력 조회·보조창 수명 |

추가로 `main.py` 종료 연결/DB 정리와 상위 창 관리자의 삭제를 이전한다.
직접 호출이 없는 `.shutdown()` 위임자, finished 슬롯의 무기한 wait,
`thread.finished → thread.deleteLater`도 같은 native 완료 계약으로 점검한다.
정상 UI의 `finish_thread` 호출은 최종 0곳이어야 한다. 예외 동기 join fallback은 이름/허용 위치를
분리해 정상 UI에서 재사용하지 않는다.

## 8. 구현 순서와 검증

1. 합성 자식 프로세스 회귀 하네스·상태/참조 보존 검사 작성.
2. 공통 coordinator/scope와 업무 보기의 전체 참여자 종료 연결.
3. 22개 직접 호출·간접 소유자·앱 quit/DB/clean marker 이전.
4. 관련 회귀·동일 런타임 반복·Windows 화면 검증 후 결과 문서 작성.

1~2만 끝난 상태는 중간 단계다. 나머지 동기 종료를 섞은 채 U39 완료로 표시하지 않는다.
수정량은 공통 함수 몇 줄보다 크다. 이전 대화의 100~300줄 추정은 이 전체 이전 범위의 보장치가 아니다.

| 필수 검사 | 수용 기준 |
| --- | --- |
| 출처 조회 직후 닫기 | 사용자 테스트량 조정에 따라 Python 3.12.10/PySide6 6.11.2에서 수정본 3,000회 × 1개 별도 프로세스 완료 |
| 늦은 native 정리 | finished 뒤 정리를 지연해도 join 전 owner/thread/worker/DB 참조 유지 |
| 빠른 완료/중복 요청 | 요청 전/도중 완료·두 번 닫기·Esc/X·부모/앱 동시 종료에서 정확히 한 번 정리 |
| 반응성 | 시작 전 준비한 10ms heartbeat가 합성 0.5초 지연 중 지속, 정상 시험의 최장 간격 200ms 이내 목표 |
| 저장/복사 취소 | 기존 rollback/복구 경계 유지, 확정 자료/영수증 보존, 성공을 취소로 위장하지 않음 |
| 확인 응답 중 종료 | 내보내기 등 GUI 응답 대기를 취소로 풀고 정상/fallback 종료 |
| 앱 종료 순서 | 모든 worker/finalizer 종료 전 cache/maintenance/engine/clean marker 처리 없음 |
| 정리 실패 | clean marker 미기록·중복 dispose 방지 |
| 수명/누적 | 100회 후 scope·타이머·스레드·창·구독 참조 수가 기준으로 복귀 |
| 다른 화면 동시 작업 | worker 정리와 결과/메뉴/새 창 요청이 겹치는 합성 반복 완료 |
| 종료 진입점 | 트레이·마지막 창·직접 quit·DB 준비·smoke의 DB 수명/native join 유지 |
| 정적 점검 | 정상 UI 종료에 새 진행창/루프/타이머/연결·무기한 wait·processEvents·terminate 없음 |

하네스는 외부 부모에서 제한시간·진행 기록을 관리한다. 교착 시 GUI 타이머도 멎을 수 있어
동일 프로세스 pytest timeout에만 의존하지 않는다. 강제 종료는 소유한 합성 자식에만 적용한다.
실사용 앱/자료는 건드리지 않는다. 반복 성공은 회귀 증거이며 교착 불가능성의 증명은 아니다.

기존 `tests/ui/test_background_operations.py`의 즉시 shutdown 가정은 명시적 완료 대기로 바꾼다.
`test_attachment_space.py`, `test_attachment_verification.py`, `test_attachment_export_ui.py`,
`test_document_lifecycle_ui.py`, `test_attachment_thumbnails.py`, `test_image_attachments.py`,
`test_data_dialog.py`, `test_reminder_preview_threading.py`, `test_resources_and_task_finding.py`를
중심으로 관련 회귀를 수행한다. 공통 수명 이전 뒤 전체 검사·ruff·mypy를 수행한다.
실행 수·실패/중단·미검증은 결과 문서에 분리한다.

Windows native에서는 닫기/앱 종료 중 기존 안내·이동/다시 그리기 및
100/150/200% 배율·좁은 창·시스템 밝음/어두움에서 밝은 스타일을 확인한다.
offscreen 성공을 native 화면/회사 PC 장시간 사용 검증으로 대신하지 않는다.

## 9. 참고와 이번 작업 결과

- [Qt QThread](https://doc.qt.io/qt-6.11/qthread.html): finished 뒤에도 정리가 이어질 수 있어 wait 성공 확인이 필요하다.
- [Qt Threads and QObjects](https://doc.qt.io/qt-6/threads-qobject.html): GUI 경계와 QObject 소유 스레드의 지연 삭제 계약.
- [Qt QCoreApplication](https://doc.qt.io/qt-6.11/qcoreapplication.html): 이벤트 루프/앱 종료 경계.

위 문서는 일반 Qt 계약이다. 특정 PySide6 잠금 역전의 수정 보증이 아니다.
coordinator·닫기 상태·검증 예산은 오늘 증거와 현재 코드에 근거한 이번 제품 설계다.

최초에는 clone과 설계만 수행했고, 후속 승인으로 실행/테스트 코드를 반영했다.
기존 동기 `shutdown()`은 명시적 teardown/aboutToQuit fallback으로 유지하며 정상 UI는
`request_shutdown()`을 사용한다. 테스트 도구의 강제 deleteLater 전에도 native join을 끝내도록 한다.
전체 검사는 한 번 수행하고 마지막 국소 보완은 해당 회귀만 확인한다. 통과 검사를 이유 없이 반복하지 않는다.
실제 결과는 86번에서 확인한다. 실사용 자료/실행 폴더·DB·버전·배포물·커밋·업로드는 변경하지 않는다.
