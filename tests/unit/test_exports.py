from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from icalendar import Calendar
from openpyxl import load_workbook  # type: ignore[import-untyped]

from officeflow.application.exporting import (
    CalendarExportOptions,
    CalendarExportScope,
    ExportCanceledError,
    ExportService,
)
from officeflow.application.tasks import TaskPage, TaskQuery
from officeflow.domain.enums import TaskPriority, TaskStatus
from officeflow.domain.task import Task
from officeflow.infrastructure.exports.calendar import ICalendarTaskExporter
from officeflow.infrastructure.exports.excel import ExcelTaskExporter


def _task(
    *,
    task_id: int,
    title: str,
    all_day: bool,
    starts_at: datetime,
    ends_at: datetime,
    recurrence_rule: str | None = None,
) -> Task:
    created = datetime(2026, 9, 1, tzinfo=UTC)
    return replace(
        Task.create(
        title=title,
        description="설명, 줄바꿈\n둘째 줄",
        priority=TaskPriority.IMPORTANT,
        all_day=all_day,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone="Asia/Seoul",
        recurrence_rule=recurrence_rule,
        now=created,
        ),
        id=task_id,
    )


def test_excel_export_preserves_typed_schedule_and_blocks_formula_injection(tmp_path) -> None:
    start = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
    task = _task(
        task_id=7,
        title="=HYPERLINK(\"https://invalid\")",
        all_day=False,
        starts_at=start,
        ends_at=start + timedelta(hours=2),
    )
    destination = tmp_path / "tasks.xlsx"

    ExcelTaskExporter().export((task,), destination)

    workbook = load_workbook(destination, data_only=False)
    try:
        sheet = workbook["업무"]
        assert sheet["B5"].value == task.title
        assert sheet["B5"].data_type == "s"
        assert isinstance(sheet["F5"].value, datetime)
        assert sheet.freeze_panes == "A5"
        assert "OfficeFlowTasks" in sheet.tables
        assert sheet.sheet_view.showGridLines is False
    finally:
        workbook.close()


def test_ics_export_preserves_multiday_end_and_recurrence(tmp_path) -> None:
    start = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
    multiday = _task(
        task_id=1,
        title="여러 날 일정",
        all_day=True,
        starts_at=start,
        ends_at=start + timedelta(days=3),
    )
    recurring = _task(
        task_id=2,
        title="매주 회의",
        all_day=False,
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        recurrence_rule="FREQ=WEEKLY;INTERVAL=1;BYDAY=TU",
    )
    destination = tmp_path / "schedule.ics"

    ICalendarTaskExporter().export((multiday, recurring), destination)
    payload = destination.read_bytes()

    assert b"\r\n" in payload
    text = payload.decode("utf-8")
    assert "DTSTART;VALUE=DATE:20260915" in text
    assert "DTEND;VALUE=DATE:20260918" in text
    assert "DTSTART;TZID=Asia/Seoul:20260915T000000" in text
    assert "DTEND;TZID=Asia/Seoul:20260915T010000" in text
    assert "RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=TU" in text
    assert text.count("BEGIN:VEVENT") == 2


@pytest.mark.parametrize("status", list(TaskStatus))
def test_ics_event_status_is_valid_and_business_status_is_preserved(tmp_path, status) -> None:
    start = datetime(2026, 10, 1, 0, tzinfo=UTC)
    task = replace(_task(task_id=31, title="내보낼 일정", all_day=False, starts_at=start,
                         ends_at=start + timedelta(hours=1)),
                   status=status, result_note="처리결과; 쉼표, 줄바꿈\n" + "긴 한글 결과 " * 30,
                   completed_at=start if status is TaskStatus.COMPLETED else None)
    path = tmp_path / "validated.ics"
    ICalendarTaskExporter().export((task,), path)
    payload = path.read_bytes()
    parsed = Calendar.from_ical(payload)
    events = parsed.walk("VEVENT")
    assert len(events) == 1
    event = events[0]
    assert not event.errors
    assert str(event["STATUS"]) in {"TENTATIVE", "CONFIRMED", "CANCELLED"}
    assert str(event["X-OFFICEFLOW-TASK-STATUS"]) == status.value
    assert str(event["UID"]) == "task-31@officeflow.local"
    assert event.decoded("DTSTART") == task.starts_at
    assert event.decoded("DTEND") == task.ends_at
    assert str(event["X-OFFICEFLOW-RESULT"]).replace("\r\n", "\n") == task.result_note
    description = str(event["DESCRIPTION"]).replace("\r\n", "\n")
    assert task.description in description
    if status is TaskStatus.COMPLETED:
        assert str(event["STATUS"]) == "CONFIRMED"
        assert "업무 상태: 완료" in description
        assert task.result_note in description
    assert all(len(line) <= 75 for line in payload.split(b"\r\n"))


def test_ics_parser_preserves_all_day_range_and_repeat_rule(tmp_path) -> None:
    start = datetime(2026, 9, 14, 15, tzinfo=UTC)
    task = _task(task_id=32, title="기간 반복", all_day=True, starts_at=start,
                 ends_at=start + timedelta(days=3), recurrence_rule="FREQ=WEEKLY;COUNT=5")
    path = tmp_path / "range.ics"
    ICalendarTaskExporter().export((task,), path)
    event = Calendar.from_ical(path.read_bytes()).walk("VEVENT")[0]
    assert event.decoded("DTSTART") == date(2026, 9, 15)
    assert event.decoded("DTEND") == date(2026, 9, 18)
    assert event["RRULE"]["FREQ"] == ["WEEKLY"]
    assert event["RRULE"]["COUNT"] == [5]


def test_ics_cancel_after_serialization_preserves_existing_target(tmp_path) -> None:
    start = datetime(2026, 10, 1, 0, tzinfo=UTC)
    task = _task(task_id=33, title="기존 파일 보존", all_day=False, starts_at=start,
                 ends_at=start + timedelta(hours=1))
    destination = tmp_path / "existing.ics"
    destination.write_bytes(b"original content")
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        return calls == 2
    with pytest.raises(ExportCanceledError):
        ICalendarTaskExporter().export((task,), destination, cancel_requested=cancel)
    assert destination.read_bytes() == b"original content"
    assert list(tmp_path.glob("*.part.ics")) == []


def test_export_cancellation_leaves_no_partial_file(tmp_path) -> None:
    start = datetime(2026, 9, 15, tzinfo=UTC)
    task = _task(
        task_id=1,
        title="취소 확인",
        all_day=True,
        starts_at=start,
        ends_at=start + timedelta(days=1),
    )
    destination = tmp_path / "canceled.xlsx"

    try:
        ExcelTaskExporter().export((task,), destination, cancel_requested=lambda: True)
    except RuntimeError:
        pass
    else:
        raise AssertionError("취소된 내보내기가 성공으로 처리되었습니다.")

    assert not destination.exists()
    assert not tuple(tmp_path.glob("*.part.xlsx"))


def test_export_service_removes_page_limit_and_calendar_omits_unscheduled(tmp_path) -> None:
    start = datetime(2026, 9, 15, tzinfo=UTC)
    scheduled = _task(
        task_id=1,
        title="일정 업무",
        all_day=True,
        starts_at=start,
        ends_at=start + timedelta(days=1),
    )
    unscheduled = replace(scheduled, id=2, title="일정 없음", starts_at=None, ends_at=None)

    class TaskServiceStub:
        def __init__(self) -> None:
            self.queries: list[TaskQuery] = []

        def query(self, query: TaskQuery) -> TaskPage:
            self.queries.append(query)
            return TaskPage((scheduled, unscheduled), 2, query.offset, query.limit)

        def export_tasks(self, query, *, cancel_requested=None):
            yield from self.query(replace(query, offset=0, limit=500)).items

    class ExporterStub:
        def __init__(self) -> None:
            self.tasks: tuple[Task, ...] = ()

        def export(self, tasks, destination, *, cancel_requested=None):
            self.tasks = tasks
            return destination

        def export_stream(self, tasks, destination, *, cancel_requested=None, progress=None):
            self.tasks = tuple(tasks)
            return destination

    task_service = TaskServiceStub()
    excel = ExporterStub()
    calendar = ExporterStub()
    service = ExportService(task_service, excel, calendar)  # type: ignore[arg-type]

    service.export_excel(TaskQuery(search="일정", limit=1), tmp_path / "tasks.xlsx")
    service.export_calendar(tmp_path / "schedule.ics")

    assert task_service.queries[0].search == "일정"
    assert task_service.queries[0].limit == 500
    assert excel.tasks == (scheduled, unscheduled)
    assert calendar.tasks == (scheduled,)


def test_calendar_export_options_filter_recurrence_completion_and_unscheduled(
    tmp_path,
) -> None:
    start = datetime(2026, 9, 15, tzinfo=UTC)
    active = _task(
        task_id=1,
        title="출장 회의",
        all_day=False,
        starts_at=start,
        ends_at=start + timedelta(hours=1),
    )
    recurring = replace(
        active,
        id=2,
        title="매주 회의",
        recurrence_rule="FREQ=WEEKLY;INTERVAL=1;BYDAY=TU",
    )
    completed = replace(
        active,
        id=3,
        title="완료 일정",
        status=TaskStatus.COMPLETED,
        completed_at=start,
    )
    unscheduled = replace(active, id=4, title="일정 없음", starts_at=None, ends_at=None)

    class TaskServiceStub:
        def query(self, query: TaskQuery) -> TaskPage:
            return TaskPage(
                (active, recurring, completed, unscheduled),
                4,
                query.offset,
                query.limit,
            )

        def get_many(self, task_ids: tuple[int, ...]) -> dict[int, Task]:
            tasks = {task.id: task for task in (active, recurring, completed) if task.id}
            return {task_id: tasks[task_id] for task_id in task_ids if task_id in tasks}

    class ExporterStub:
        def __init__(self) -> None:
            self.tasks: tuple[Task, ...] = ()

        def export(self, tasks, destination, *, cancel_requested=None):
            self.tasks = tasks
            return destination

    calendar = ExporterStub()
    service = ExportService(  # type: ignore[arg-type]
        TaskServiceStub(),
        ExporterStub(),
        calendar,
    )
    options = CalendarExportOptions(
        scope=CalendarExportScope.CURRENT_LIST,
        query=TaskQuery(),
        include_recurring=False,
        include_completed=False,
    )

    result = service.export_calendar(tmp_path / "trip.ics", options=options)

    assert calendar.tasks == (active,)
    assert result.exported_count == 1
    assert result.excluded_recurring_count == 1
    assert result.excluded_completed_count == 1
    assert result.excluded_unscheduled_count == 1


def test_calendar_export_supports_selected_tasks_and_inclusive_date_range(tmp_path) -> None:
    start = datetime(2026, 9, 15, tzinfo=UTC)
    first = _task(
        task_id=1,
        title="선택 일정",
        all_day=True,
        starts_at=start,
        ends_at=start + timedelta(days=2),
    )
    second = replace(first, id=2, title="다른 일정")

    class TaskServiceStub:
        def __init__(self) -> None:
            self.range: tuple[date, date] | None = None

        def get_many(self, task_ids: tuple[int, ...]) -> dict[int, Task]:
            tasks = {1: first, 2: second}
            return {task_id: tasks[task_id] for task_id in task_ids if task_id in tasks}

        def calendar_range(self, date_from: date, date_to: date) -> tuple[Task, ...]:
            self.range = (date_from, date_to)
            return (first, second)

    class ExporterStub:
        def __init__(self) -> None:
            self.tasks: tuple[Task, ...] = ()

        def export(self, tasks, destination, *, cancel_requested=None):
            self.tasks = tasks
            return destination

    task_service = TaskServiceStub()
    calendar = ExporterStub()
    service = ExportService(  # type: ignore[arg-type]
        task_service,
        ExporterStub(),
        calendar,
    )
    service.export_calendar(
        tmp_path / "selected.ics",
        options=CalendarExportOptions(
            scope=CalendarExportScope.SELECTED_TASKS,
            selected_task_ids=(2,),
        ),
    )
    assert calendar.tasks == (second,)

    service.export_calendar(
        tmp_path / "range.ics",
        options=CalendarExportOptions(
            scope=CalendarExportScope.DATE_RANGE,
            date_from=date(2026, 9, 15),
            date_to=date(2026, 9, 20),
        ),
    )
    assert task_service.range == (date(2026, 9, 15), date(2026, 9, 21))
