# U36 메모리·창 자원 정리 상세설계

작성일: 2026-10-26. [80번 공통설계](80-resource-and-task-finding-common-design.md)의 1차 상세설계.
상태: **설계 완료, 실행 코드 수정/회귀 검증 전**. 관측 사실·실패한 시험·측정 한계는 [79번](79-memory-and-resource-review.md)을 따른다.

후속 구현 승인으로 소스에 반영했다. 최신 검사 결과는 [83번](83-resource-and-task-finding-results.md)을 따른다.
이하의 "설계 단계"는 최초 작성 시점의 기록이며 회사 PC 종료 원인/6시간 운영 확인을 뜻하지 않는다.

## 1. 명확한 정리 누락부터 수정

| 대상/현재 경로 | 계획하는 종료 처리 | 보호할 동작 |
| --- | --- | --- |
| MainWindow._clear_reminder_dialog | 종료한 창을 인자로 받아 동일성 확인 후 참조 해제·deleteLater | 미처리 알림이 있으면 기존처럼 닫지 않음. 새 알림창 포인터를 옛 finished가 지우지 않음 |
| _complete_selected_with_result | exec 반환 뒤 finally에서 명시적 삭제 예약 | 저장 실패 중에는 exec가 반환하지 않으므로 초안/저장 콜백을 유지 |
| 업무 우클릭 두 진입/오늘 알림 우클릭 | exec 종료·예외 모두 finally에서 메뉴 삭제 예약 | QAction callback이 읽을 값은 동기 실행 중만 사용, 후속 비동기 동작에는 ID/회차 값만 전달 |
| 첨부 이름/메모 입력 | exec 결과/저장 오류 처리 뒤 finally에서 삭제 예약 | 저장 실패의 입력 보존, 취소 시 파일/DB 불변 |
| 첨부 추가 진행창 | worker/native thread 종료 확인 후 참조 분리·신호 차단·닫기·deleteLater | 취소 신호가 이미 끝난 작업/새 worker를 다시 취소하지 않음 |
| ICS/업무 묶음 선택창 | exec 후 options를 값 객체로 확보하고 finally 삭제 | 옵션 취소·대상 선택 취소·내보내기 실패 모두 정리. options는 widget을 참조하지 않음 |

### 1.1 공통 구현 규칙

- `close/hide/accept`와 객체 삭제를 구분한다. 부모가 언젠가 삭제한다는 이유로 단기 창을 계속 남기지 않는다.
- 모든 QDialog에 일괄 `WA_DeleteOnClose`를 붙이지 않는다. 작업 중 종료 유예, 결과 읽기, 재사용 창의 계약이 다르다.
- 기존 정상 동작인 업무 보기/설정/이력의 keeper를 보존한다. 필요한 곳에 작은 종료 helper만 추가한다.
- terminal finished 후에만 signal/registry를 정리한다. 파일 worker 사용 중인 소유자를 먼저 지우지 않는다.
- Qt 객체는 GUI 스레드에서 정리한다. 늦은 callback은 `isValid`, shutdown 상태, generation, 객체 동일성으로 차단한다.
- `_reminder_dialog`는 `popup` 지역 변수/인자를 캡처한다. `self._reminder_dialog = None`을 무조건 실행하는 lambda를 피한다.
- progress 정리는 `_attachment_progress`를 지역 변수로 빼고 해당 요청이 현재 것인지 확인한다.
  native thread 종료/정리까지 worker wrapper를 유지하고 그 뒤 삭제한다. `terminate()`는 사용하지 않는다.
- 재사용하는 이미지 미리보기 1개처럼 의도적으로 유지하는 객체는 예외 목록에 문서화한다. 숨김 자체를 누수로 판정하지 않는다.

## 2. 숨김 상태의 불필요한 조회 줄이기

현행 `_view_timer`에는 보임 검사 조건이 있지만 `_check_reminders`가 별도로 화면 갱신을 호출한다.
이를 다음 세 역할로 분리한다.

1. **필수 알림:** 기존 30초/밀린 예약 1초 검사·놓친 알림 복구·매시 알림·팝업 동기화를 유지한다.
2. **실제 변경 통지:** 완료/재알림/업무 변경 시 영향 ID를 dirty로 표시하고 보이는 기록창의 실제 실행 상태만 갱신한다.
3. **조회용 화면:** 보이는 주 창의 현재 화면만 다시 읽고, 숨김/최소화에서는 dirty 사유만 합친다.

- 주 창이 숨겨져도 열린 업무 보기/알림창은 별개의 보이는 화면이다. 모두 숨겨진 것으로 취급하지 않는다.
- 업무 보기의 미저장 결과/일지는 다시 채우지 않는다. 실제 상태/헤더 갱신과 편집 버퍼를 분리한다.
- 트레이 복귀/활성화/시간대·날짜 변경 시 dirty 내용을 한 번 갱신하고 선택·스크롤·캘린더 날짜를 유지한다.
- 검색/필터/최근 메뉴처럼 사용자가 요청한 읽기는 즉시 허용한다. 재조회 실패는 이전 화면+오류 안내를 유지한다.
- dirty는 비트/작은 ID 집합이며 무한히 쌓지 않는다. ID 100개를 넘으면 `현재 화면 전체 dirty` 하나로 축약한다.

## 3. 내부 복구 점검의 유휴 비용

`FileMaintenance`의 경로 보호/저널/참조 확인/사용 중 자료 보존은 변경하지 않는다.
정리할 것이 없어도 `_internal_recovery_finished`가 1초 뒤 새 thread를 만드는 비용을 줄인다.

- 첫 필수 preflight와 시작 후 첫 tick은 기존 시점에 수행한다. 알림 시작을 일반 정리 완료까지 기다리게 하지 않는다.
- 연속 유휴 tick의 간격은 `1 → 2 → 5 → 15 → 30 → 60초`, 최대 60초로 둔다.
- 새 pending 파일 명령·복구 가능 작업/실제 진행이 있으면 1초로 돌아간다. 대기 중 wake 신호는 최신 1개로 합친다.
- 한 시간 뒤 재시도할 보호 작업이 있다는 이유만으로 매초 깨우지 않는다. 다음 실제 검사/재시도 시각을 반영한다.
- Application 쪽의 작은 `MaintenanceTickHint(report, progressed, next_due)`로 스케줄 힌트를 전달한다.
  긴 recovery callable/GUI가 storage private 상태를 직접 읽는 방식은 쓰지 않는다.
- 기존 job당 QThread 생성/안전 종료 모델을 우선 유지한다. 파일/SQLite 소유권 검토 없이 thread pool로 바꾸지 않는다.
- 실제 파일 관측/내용 해시의 배치 상한·예산·취소/보호는 기존 그대로다. 업무/첨부 전체를 메모리로 읽지 않는다.

## 4. 제한된 자원 관측

### 4.1 수집 정보

새 논리 경계 `infrastructure/windows/resources.py` → Application의 `ResourceSnapshot` → Presentation controller로 나눈다.

- 시각(UTC)/monotonic 경과, 실행 ID·버전·Python형/EXE형.
- **자기 프로세스만:** working set, private bytes, process handle/GDI/USER 개수. 자기 CPU 누적 시간은 선택적 숫자다.
- **시스템 합계만:** CommitTotal/CommitLimit·PhysicalAvailable/PhysicalTotal·Windows low-memory signal.
- 일반/압박/관측 실패 상태, 숨김 여부, 활성 작업 종류/개수, 이미지 LRU bytes/entries 등 작은 숫자.
- 업무 제목/설명/일지/파일명·경로/바이트/키 입력/계정/다른 프로세스 목록·문서 내용은 수집하지 않는다.

`GetPerformanceInfo`의 page 단위 값은 PageSize를 곱해 bytes로 해석한다.
각 ctypes API의 argtypes/restype·구조체 크기·실패 반환을 명시하고, Windows가 아닌 환경은 unsupported로 처리한다.
low-memory HANDLE은 프로세스당 최대 1개 생성하고 정상/오류/종료 경로에서 CloseHandle 한다.
이 API를 쓰는 것은 조직 보안 설정을 바꾸거나 관리자 권한을 요구하는 동작이 아니다. 거절되면 관측만 unavailable이다.

### 4.2 비용/기록

- GUI의 30초 timer는 요청만 보낸다. 숫자 읽기/로그 쓰기는 전용 controller가 소유한 worker 최대 1개로 처리한다.
  이 worker는 재사용하며 tick마다 새 thread를 만들지 않는다. GUI QObject/ORM/session을 worker로 보내지 않는다.
- sample 요청은 실행 중 1개+대기 최신 1개다. sample 값은 최대 16개만 보관한다. 실패 traceback 객체는 보관하지 않는다.
- 추가 `logs/runtime-resources.log`는 1MiB·회전 2개, 정상은 60초 간격이며 전이/정상 시작·종료는 한 줄 추가한다.
  failure/error 상세는 예외 타입·코드 위치·짧은 고정 오류 코드만 기록한다.
- 같은 원인의 실패는 상태 전이/5분당 1회로 제한한다. 기록 실패를 기록하려다 무한 재귀하지 않는다.
- logger/low-memory 핸들/worker는 시작당 하나만 설치한다. 다중 build_application/test 호스트에서 중복 핸들러를 남기지 않는다.
- 기존 native-fault·Python exception hook·비정상 종료 marker는 보존한다. 이 숫자 로그가 crash dump의 대체물은 아니다.

## 5. 저메모리 대응 상태

임계값은 **설계 기본값**이며 운영 결과가 아니다. 사용자에게 새 설정 항목을 여러 개 추가하지 않는다.

| 전이 | 기준 |
| --- | --- |
| normal → pressure | 유효 sample 2회 연속 low-memory signal 또는 commit 사용률 92% 이상 |
| 즉시 pressure | 유효 sample의 commit 사용률 97% 이상 |
| pressure → normal | low signal 없음 + commit 85% 미만인 유효 sample 3회 연속 |
| 관측 unavailable | 숫자를 0으로 만들지 않고 unknown. 유효 상태의 짧은 유효기간은 아래 계약 적용 |

- 물리 RAM 사용률 하나나 OfficeFlow가 특정 MB를 넘었다는 이유만으로 업무를 닫지 않는다.
- 이전 압박 판정은 마지막 유효 sample 후 최대 120초만 유효하다. 이후 관측이 계속 불가하면 unknown으로
  전환하고 영구적인 작업 금지 상태를 만들지 않는다. 읽기 실패 자체는 알림/파일 명령을 중단할 근거가 아니다.
- pressure 때 재생성 가능한 이미지 LRU 목표를 32MiB → 8MiB로 줄인다. 현재 표시/미저장 clipboard 이미지,
  원본·작업본·수정 보기 사본·파일 사용 예약은 제거하지 않는다. 다른 참조가 있으면 OS 메모리가 즉시 줄지 않을 수 있다.
- 대기 중 자동 썸네일/숨김 화면 조회를 취소·합치고 새 자동 백업 시작만 보류한다. 알림·사용자 명시적 조회/저장은 유지한다.
- 진행 중인 백업/저장/복사/검증은 강제 정지하지 않는다. 기존 안전한 취소/마무리·영수증 경계를 따른다.
- 자동 백업 보류는 성공·실패로 위장하지 않고 실제 백업 시도를 시작하기 전의 **임시 대기 사유**로 분리한다.
  기존 BackupHealthStore의 성공/실패 이력과 최근 성공 시각을 덮어쓰지 않는다. 데이터 화면에 `메모리 여유 부족으로 대기`를 표시한다.
  recovery/unknown 전환 시 다음 백업 시점 검사를 깨워 1분 내 재판정한다. 여전히 압박이면 보류를 유지한다.
- 정상 복귀 후 LRU 상한만 되돌리고 전체 이미지를 미리 채우지 않는다. 캐시에서 빠진 항목의 재요청 표시는
  소유 화면의 다음 실제 요청 시 재설정하여 무한 decode/evict 반복을 막는다.
- 정상/관측 실패 자체에 modal 창을 띄우지 않는다. pressure 진입 때 작은 비차단 상태 안내 1회와
  기존 관리/자료 검사 진입을 제공한다. 반드시 종료 직전에 경고할 수 있다고 보장하지 않는다.

## 6. 변경 예상 위치

- `presentation/main_window.py`, `reminder_preview.py`, `record_dialog.py`, `data_dialog.py`: 확인된 종료 경로.
- `presentation/background.py`: 필요한 작은 안전 정리 helper. 전체 worker 아키텍처를 재작성하지 않는다.
- `infrastructure/attachments/maintenance.py`, Application recovery 결과: 유휴/다음 검사 힌트.
- 새 `application/resource_policy.py`, `infrastructure/windows/resources.py`, `presentation/resource_controller.py`:
  숫자 모델/정책·플랫폼 어댑터·소유자/GUI 신호. 실제 이름은 구현 시 기존 명칭 충돌 확인.
- `attachment_thumbnails.py`: 제한된 trim API/정상 재요청. 기존 cache 원장/디스크 사본 정리 코드는 변경하지 않는다.
- `bootstrap/diagnostics.py`, 기존 자료 검사 화면: 기록 경로와 수치 상태 연결. 신호가 아닌 worker에서 UI를 직접 조작하지 않는다.
- 사용자 도움말: 자원 기록 위치·회사 PC 기록 필요·한계·백업 대기 상태를 실제 구현 후 갱신한다.

## 7. 수용/회귀 검사

1. 각 확인된 누락 경로를 최소 40회·100회 반복하고 Qt 지연 삭제 후 예상 잔여 수가 기준값으로 돌아온다.
   취소/저장 실패/메뉴 항목 선택/정상 완료/부모 먼저 닫기/종료 중 늦은 signal도 검사한다.
2. 기존 영구 메뉴·의도된 재사용 창은 기준 개수만 남는다. weakref/native 객체/구독자/핸들을 따로 확인한다.
3. 창 해제 직후 새 알림이 발생하는 경쟁에서 새 창이 사라지지 않음, 알림 확인·재알림·업무/회차 완료 의미가 그대로임.
4. 첨부 복사·저장/검증 중 취소·창 닫기·앱 종료에서 원본·영수증·DB 무결성/미저장 초안이 보존됨.
5. 숨김 주 창의 반복 poll은 화면 SQL/미리보기 요청을 만들지 않지만 예약 알림·보이는 기록창 동기화는 작동함.
6. 유휴 간격 상한·새 작업 wake·지연 재시도·단일 worker를 fake clock으로 검사함.
7. API unsupported/권한 실패/이상값/핸들 생성 실패/로그 쓰기 실패/rotation/종료를 주입해 앱이 계속 동작함.
8. 압박 전이/히스테리시스/120초 만료·캐시 trim·백업 대기/복귀를 합성 sample로 검사함. 실제 RAM 고갈은 시키지 않음.
9. 작은 합성 자료로 별도 native Windows/Python 프로세스에서 **실제 show/close/exec 종료**·취소·장시간 유휴를 검증함.
   각 시험은 faulthandler·타임아웃·단계 로그를 먼저 준비한다. 이전 native 조기 종료 원인을 재확인하고 미해결이면 통과로 처리하지 않음.
10. warm-up 뒤 반복 block별 private/working set·GDI/USER/handles 추세를 기록한다. allocator 캐시 때문에
    RSS가 즉시 원복되지 않는 것과 실제 객체 누적을 구분한다. 생성만 한 시험의 MB를 native 운영 측정으로 재사용하지 않는다.
11. 장시간 native 6시간 유휴/주기 알림은 별도 승인된 검증 세션으로 수행하고, 단기 자동 검사와 수행 여부를 구분한다.
    시스템 자원 여유가 부족하면 대규모 반복을 중단하고 미검증으로 보고한다. 타 프로그램을 자동 실행해 압박하지 않는다.
12. 관련/전체 회귀, Ruff/mypy, UI 변경 화면의 작은 창·밝은/어두운 기본 팔레트·100/150/200%를 확인한다.

이번 설계 단계에서는 위 검사를 실행하거나 통과했다고 주장하지 않는다. 사용자 PC/실사용 자료는 검증에 쓰지 않는다.

## 8. 근거

- [Qt QDialog 수명/종료 API](https://doc.qt.io/qt-6/qdialog.html): 종료와 삭제의 구분을 검토하는 공식 문서.
- [Windows memory resource notification](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-creatememoryresourcenotification): 물리 메모리 압박 신호이며 commit 여유와 같은 지표가 아님.
- [Windows PERFORMANCE_INFORMATION](https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-performance_information): commit/물리 메모리 값의 단위/해석.

외부 문서는 API 근거이며 임계값·기능 완료·회사 PC 원인을 입증하지 않는다. DB0016/백업 v3는 이 차수에서 유지한다.
