# 데이터 모델

## 1. 시간 표현 규칙

- DB의 시각 값은 UTC ISO 8601 또는 UTC 기준 datetime으로 저장한다.
- 사용자가 날짜만 입력한 종일 일정도 내부적으로 반열린 구간 `[start, end)`으로 저장한다.
- 화면의 종료일은 포함 날짜다. 예를 들어 9월 14일~17일 종일 일정은 내부적으로 14일 00:00부터 18일 00:00 직전까지다.
- 사용자 시간대는 설정에 저장하며 기본값은 시스템 시간대다.
- 날짜 겹침 조건은 `task.starts_at < range_end AND task.ends_at > range_start`다.

## 2. 핵심 테이블

### tasks

| 컬럼 | 설명 |
|---|---|
| id | 내부 기본키 |
| legacy_id | v2.6 원본 업무 ID, 신규 업무는 NULL |
| title | 목록에 표시할 짧은 제목 |
| description | 상세 내용 |
| status | active, pending, completed, canceled, archived |
| priority | normal, attention, important, urgent |
| is_pinned | 상단 고정 여부 |
| all_day | 종일 일정 여부 |
| starts_at | 시작 시각, 일정 없음은 NULL |
| ends_at | 배타적 종료 시각, 단일 시점 업무는 NULL 가능 |
| timezone | 일정 해석 시간대 |
| recurrence_rule | 반복 규칙, 반복 없음은 NULL |
| recurrence_until | 반복 종료 범위 최적화용 값 |
| result_note | 비반복 업무의 결과 메모 |
| completed_at | 비반복 업무 완료 시각 |
| created_at / updated_at | 생성·수정 시각 |
| deleted_at | 소프트 삭제 시각 |

제약 조건:

- 제목은 공백을 제외하고 비어 있을 수 없다.
- `ends_at`이 있으면 `starts_at`도 있어야 한다.
- `ends_at`은 `starts_at`보다 늦어야 한다.
- 허용된 상태와 중요도 외의 값은 저장하지 않는다.

### task_occurrences

반복 업무의 특정 발생 건에 대한 예외와 완료 상태를 저장한다.

| 컬럼 | 설명 |
|---|---|
| id | 기본키 |
| task_id | 반복 원본 업무 |
| occurrence_start | 원래 발생 시작 시각 |
| occurrence_end | 원래 발생 종료 시각 |
| effective_start / effective_end | 해당 발생 건만 이동했을 때의 시각 |
| status | pending, completed, skipped, canceled |
| completed_at | 완료 시각 |
| result_note | 발생 건의 결과 메모 |

`task_id + occurrence_start`는 유일해야 한다.

### reminders

| 컬럼 | 설명 |
|---|---|
| id | 기본키 |
| task_id | 대상 업무 |
| relation | start, end, absolute |
| offset_minutes | 시작·종료 기준 상대 시간 |
| absolute_at | 절대 알림 시각 |
| enabled | 활성 여부 |
| last_fired_key | 마지막 발송 중복 방지 키 |

### checklist_items

- id, task_id, content, is_done, position, completed_at

### attachments

- id, task_id, original_name, stored_name, relative_path, size_bytes, checksum, created_at, missing_at
- DB에는 앱 데이터 디렉터리 기준 상대 경로만 저장한다.
- 실제 파일 삭제와 연결 해제는 분리한다.

### work_logs

- id, task_id, occurrence_id, log_date, content, result, priority_snapshot, created_at, updated_at
- 업무가 수정된 뒤에도 당시 기록을 보존하기 위해 제목과 중요도의 필요한 스냅샷을 둘 수 있다.

### notes

- id, note_date, content, updated_at
- 기존 단일 메모는 전역 메모 한 건으로 이전한다.

### app_settings

- key, value_json, updated_at
- 비밀번호와 토큰은 저장하지 않는다. 향후 필요하면 Windows 자격 증명 저장소를 사용한다.

## 3. 인덱스 초안

- `tasks(status, deleted_at, starts_at)`
- `tasks(status, priority, updated_at)`
- `tasks(starts_at, ends_at)`
- `tasks(is_pinned, status, updated_at)`
- `task_occurrences(task_id, occurrence_start)` unique
- `work_logs(log_date, task_id)`
- `attachments(task_id)`
- `reminders(enabled, absolute_at)`

인덱스는 실제 5,000개 이상 기준 데이터의 쿼리 계획을 확인한 뒤 확정한다.

## 4. 반복 일정 규칙

- 반복 원본과 각 발생 건을 구분한다.
- 단일 발생 건 완료는 원본 반복 규칙을 변경하지 않는다.
- `이번 일정만`, `이후 일정`, `전체 반복` 편집을 구분한다.
- 반복 규칙 저장 형식은 iCalendar RRULE 호환 문자열을 사용한다.
- UI가 지원하지 않는 복잡한 RRULE을 가져온 경우 원문을 보존하고 제한 사항을 알린다.

## 5. 상태 전이

```text
active ──> pending ──> active
   │          │
   ├──────────┴──> completed
   ├─────────────> canceled
   └─────────────> archived

completed/canceled ──> archived
```

완료 또는 취소를 되돌리는 기능은 명시적 복원 동작으로 제공하고 이력을 남긴다.
