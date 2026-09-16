# 기존 데이터 이전 설계

## 1. 원칙

마이그레이션은 원본 v2.6 DB와 첨부파일을 읽기 전용으로 취급한다. 새 DB에 모든 데이터를 쓴 뒤 검증이 성공해야만 새 앱이 해당 DB를 사용한다.

## 2. 이전 절차

1. 사용자가 기존 `office_tasks.db`를 선택한다.
2. 앱이 DB 파일과 연결된 첨부파일 경로를 검사한다.
3. SQLite 온라인 백업으로 원본 DB의 안전 사본을 만든다.
4. 현재 v3 DB·첨부·설정을 임시 영역에 복제한다.
5. 임시 사본에 업무, 업무일지, 메모, 첨부파일 순으로 합친다.
6. 개수, 외래키, 날짜, 파일 존재 여부를 검증한다.
7. 사용자에게 변환 결과와 경고를 보여준다.
8. 성공 시 검증된 `.ofbackup`을 예약하고 다음 실행의 DB 연결 전에 원자적으로 교체한다.
9. 실패 시 임시 결과만 격리하고 원본은 그대로 둔다.

## 3. 필드 매핑 초안

| v2.6 | v3 | 규칙 |
|---|---|---|
| tasks.id | tasks.legacy_id | 원본 추적용 |
| content | title + description | 첫 줄을 제목, 전체 원문을 설명으로 보존 |
| target_date | starts_at / ends_at | 해당 날짜의 단일 일정으로 변환 |
| alarm_time | reminders | 유효한 시각이면 시작 기준 알림 생성 |
| frequency=Once | recurrence_rule=NULL | 단일 일정 |
| frequency=Daily | recurrence_rule | 매일 반복으로 변환 |
| frequency=Weekly | recurrence_rule | week_day를 주간 반복으로 변환 |
| status | status | active, pending, done을 새 enum으로 매핑 |
| deadline | description | 종료일로 추정하지 않고 `기존 마감일` 표식과 함께 원문 보존 |
| category | priority | 보통/관심/중요/긴급 매핑 |
| result_memo | result_note | 원문 보존 |
| completed_at | completed_at | 파싱 실패 시 원문을 오류 보고서에 남김 |
| history_logs | work_logs | task_id 연결 여부 검증 |
| attachments.saved_path | attachments.relative_path | 파일 복사 후 새 상대 경로 저장 |
| memos.id=1 | notes | 전역 메모로 변환 |

## 4. 애매한 데이터 처리

- 날짜 또는 시간이 비어 있거나 잘못된 업무는 삭제하지 않고 `일정 없음`으로 가져온다.
- `content` 첫 줄이 지나치게 길면 제목 길이만 제한하고 전체 원문은 설명에 남긴다.
- 존재하지 않는 첨부파일은 레코드를 유지하고 `missing_at`을 기록한다.
- 중복 첨부파일은 체크섬을 계산하되 사용자 승인 없이 하나로 합치지 않는다.
- 알 수 없는 상태나 중요도는 기본값으로 변환하면서 원래 값을 보고서에 남긴다.
- `file_path`가 파일이면 첨부로 복사하고, 폴더 또는 누락 경로이면 설명과 보고서에 보존한다.
- v2.6 옆에 WAL 파일이 있으면 실행 중 변경 가능성이 있으므로 가져오기를 중단한다.

## 5. 검증 보고서

보고서는 최소한 다음 항목을 포함한다.

- 원본 및 변환 DB 경로
- 시작·종료 시각과 앱 버전
- 테이블별 원본 수, 성공 수, 경고 수, 실패 수
- 연결되지 않은 업무일지 수
- 누락된 첨부파일 수
- 파싱하지 못한 날짜와 시간 목록
- 원본 DB SHA-256 체크섬

구현 보고서는 사용자 데이터 폴더의 `migration-reports/v26-import-*.json`에 저장한다.

## 6. 마이그레이션 테스트

- 빈 DB
- 각 과거 컬럼 버전의 DB
- 1회, 매일, 매주 반복 업무가 섞인 DB
- 완료·대기·활성 업무가 섞인 DB
- 잘못된 날짜와 NULL이 포함된 DB
- 첨부파일이 모두 있는 경우와 일부 누락된 경우
- 한글, 이모지, 긴 내용이 포함된 DB
- 5,000개 이상 업무가 있는 대용량 DB

실제 사용자 DB는 자동화 테스트 fixture로 커밋하지 않는다. 개인정보를 제거한 구조 복제본을 사용한다.
