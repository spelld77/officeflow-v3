# 기술 아키텍처

## 1. 아키텍처 선택

OfficeFlow v3는 계층형 모듈러 모놀리스로 개발한다. 개인용 데스크톱 앱에 불필요한 네트워크 서비스나 마이크로서비스를 도입하지 않으면서, UI와 업무 규칙이 서로 직접 의존하지 않도록 한다.

```text
Presentation (PySide6)
        │ commands / view models
        ▼
Application (use cases, transactions)
        │ domain objects / ports
        ▼
Domain (task, schedule, recurrence, reminder rules)
        ▲
        │ repository implementations
Infrastructure (SQLite, files, tray, notifications, export)
```

의존성은 바깥 계층에서 안쪽 계층으로만 향한다. Domain 계층은 PySide6나 SQLite를 import하지 않는다.

## 2. 제안 디렉터리

```text
src/officeflow/
├─ main.py
├─ presentation/
│  ├─ main_window.py
│  ├─ views/
│  ├─ widgets/
│  ├─ viewmodels/
│  └─ resources/
├─ application/
│  ├─ commands/
│  ├─ queries/
│  ├─ services/
│  └─ dto/
├─ domain/
│  ├─ task.py
│  ├─ schedule.py
│  ├─ recurrence.py
│  ├─ reminder.py
│  └─ errors.py
├─ infrastructure/
│  ├─ database/
│  ├─ filesystem/
│  ├─ notifications/
│  ├─ exports/
│  └─ settings/
└─ bootstrap/
   ├─ container.py
   └─ paths.py
tests/
├─ unit/
├─ integration/
├─ ui/
└─ fixtures/
```

## 3. 주요 경계

### Presentation

- 사용자의 입력을 검증 가능한 명령으로 변환한다.
- DB 쿼리를 직접 호출하지 않는다.
- 목록은 `QAbstractListModel` 또는 `QAbstractTableModel`을 사용한다.
- 긴 작업은 워커에서 실행하고 결과만 UI 스레드로 전달한다.

### Application

- `CreateTask`, `UpdateTask`, `CompleteOccurrence`, `SearchTasks`, `ImportLegacyDatabase` 같은 사용 사례를 제공한다.
- 하나의 사용 사례가 하나의 트랜잭션 경계를 가진다.
- UI 메시지 대신 의미 있는 결과와 오류 코드를 반환한다.

### Domain

- 시작·종료 유효성, 상태 전이, 반복 발생, 지연 계산을 담당한다.
- 현재 시각은 직접 호출하지 않고 Clock 인터페이스로 주입해 테스트할 수 있게 한다.
- `지연`과 `진행 중`은 저장하지 않고 일정과 Clock으로 계산한다.

### Infrastructure

- SQLite repository와 스키마 마이그레이션
- 첨부파일 저장 및 경로 검증
- Windows 알림, 시스템 트레이, 단일 실행 잠금
- Excel·ICS 내보내기
- 설정, 로그, 백업

## 4. 동시성 규칙

- UI 스레드는 렌더링과 짧은 상태 변경만 담당한다.
- DB 연결을 스레드 간 공유하지 않는다. 작업 단위마다 연결 또는 세션을 얻는다.
- 알림 스케줄러는 다음 알림 시각을 계산하지만 UI 객체를 직접 조작하지 않는다.
- Phase 5B의 알림 서비스는 규칙 계산과 발송 이력 선점을 담당하고, Presentation 계층은
  반환된 알림 묶음만 표시한다.
- 첨부파일 복사, 대규모 내보내기, 데이터 이전은 취소 가능한 백그라운드 작업으로 실행한다.
- Phase 6B의 첨부 복사는 같은 관리 볼륨의 `.part` 파일에 기록한 뒤 원자적으로 교체한다.
  DB 저장은 파일 확정 후 수행하고 실패하면 확정 파일을 제거한다. 영구 삭제는 파일 격리,
  DB 연결 삭제, 격리 파일 제거 순서로 수행하며 DB 실패 시 원위치로 복구한다.
- 앱 종료 시 워커 종료, DB 세션 정리, 트레이 종료 순서를 보장한다.

## 5. 성능 설계

- 전체 업무를 한 번에 UI 위젯으로 만들지 않는다.
- 목록 쿼리는 페이지 단위로 읽고 Qt 모델의 `fetchMore` 패턴을 사용한다.
- 검색은 UI 디바운스와 SQL 인덱스를 함께 사용한다.
- 날짜 겹침 조회는 `starts_at < 조회종료 AND ends_at > 조회시작` 규칙을 사용한다.
- 상태, 시작·종료 시각, 중요도, 갱신 시각에 필요한 복합 인덱스를 둔다.
- 업무 제목·설명·결과와 업무일지 내용 검색은 SQLite FTS5 색인을 사용하며 DB 트리거로 원본과 동기화한다.

Phase 3A에서는 `TaskQuery`와 `TaskPage`를 애플리케이션 계층의 조회 계약으로 사용한다. 보기,
검색어, 상태, 중요도, 고정 여부, 그룹, 정렬, offset/limit을 하나의 불변 요청으로 전달하고
SQLite 저장소가 필터·정렬·개수 계산을 수행한다. 장기 누적 데이터에서도 본문 전체를 매번
훑지 않도록 Phase 8에서 FTS5를 도입했으며, 기존 레코드는 DB 마이그레이션 시 자동 색인한다.
오늘 화면과 업무일지 검색 결과는 각각 필요한 페이지 범위만 조회한다.

## 6. 운영 경로

소스 코드 위치와 사용자 데이터 위치를 분리한다.

```text
%LOCALAPPDATA%/OfficeFlow/
├─ data/officeflow.db
├─ attachments/
├─ backups/
├─ logs/
└─ settings.json
```

개발 및 테스트에서는 경로 제공자를 주입해 임시 디렉터리를 사용한다. 테스트가 실제 사용자 데이터에 접근해서는 안 된다.

## 7. 단일 실행과 알림

- 고정 TCP 포트 대신 `QLockFile`로 실행 소유권을 결정하고 `QLocalServer`로 기존 창에
  활성화·빠른 등록 메시지를 전달한다.
- 두 번째 실행 요청은 기존 창을 앞으로 가져오는 메시지를 보낸다.
- 알림 식별자는 `task_id + occurrence_start + reminder_id`로 구성해 중복을 방지한다.
- 앱 중단 중 발생한 알림은 설정된 유예 시간 안의 것만 한 번 복구한다.
- 실제 발송 이력은 고유한 `fire_key`로 영구 저장하며 다시 알림은 같은 이력의 상태와
  `snoozed_until`만 변경한다.
- Windows 전역 단축키는 `RegisterHotKey`/`UnregisterHotKey` 수명 주기를 앱과 함께 관리한다.
- 시작 시 실행은 현재 사용자 `Run` 레지스트리의 `OfficeFlow v3` 값만 변경하며 기존
  OfficeFlow 버전의 항목은 건드리지 않는다.

## 8. 오류 처리와 로그

- 예상 가능한 업무 오류와 시스템 오류를 구분한다.
- `except: pass`를 허용하지 않는다.
- 사용자에게는 해결 방법 중심 메시지를 표시하고 상세 스택은 회전 로그에 남긴다.
- DB 손상, 마이그레이션 실패, 첨부파일 누락은 별도 진단 이벤트로 기록한다.
- 민감 정보와 첨부파일 내용은 로그에 남기지 않는다.
