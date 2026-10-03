from __future__ import annotations

import os
import tempfile
import warnings
from collections.abc import Callable, Iterable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook  # type: ignore[import-untyped]
from openpyxl.cell import WriteOnlyCell  # type: ignore[import-untyped]
from openpyxl.styles import (  # type: ignore[import-untyped]
    Alignment,
    Border,
    Font,
    PatternFill,
    Side,
)
from openpyxl.worksheet.table import (  # type: ignore[import-untyped]
    Table,
    TableColumn,
    TableStyleInfo,
)

from officeflow.application.exporting import ExportCanceledError
from officeflow.domain.enums import TaskPriority, TaskStatus
from officeflow.domain.task import Task

_HEADERS = (
    "ID",
    "제목",
    "설명",
    "상태",
    "중요도",
    "시작",
    "종료",
    "종일",
    "반복 규칙",
    "고정",
    "완료 요약",
    "첨부",
    "생성일",
    "수정일",
)
_STATUS_LABELS = {
    TaskStatus.ACTIVE: "진행",
    TaskStatus.PENDING: "대기",
    TaskStatus.COMPLETED: "완료",
    TaskStatus.CANCELED: "취소",
    TaskStatus.ARCHIVED: "보관",
}
_PRIORITY_LABELS = {
    TaskPriority.NORMAL: "보통",
    TaskPriority.ATTENTION: "관심",
    TaskPriority.IMPORTANT: "중요",
    TaskPriority.URGENT: "긴급",
}


class ExcelTaskExporter:
    def export(
        self,
        tasks: tuple[Task, ...],
        destination: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        return self.export_stream(tasks, destination, cancel_requested=cancel_requested)

    def export_stream(
        self,
        tasks: Iterable[Task],
        destination: Path,
        *,
        cancel_requested: Callable[[], bool] | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> Path:
        target = destination.resolve()
        if target.suffix.casefold() != ".xlsx":
            raise ValueError("Excel 내보내기 파일은 .xlsx 확장자여야 합니다.")
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{target.stem}-", suffix=".part.xlsx", dir=target.parent
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            workbook, count = self._build_workbook(tasks, cancel_requested, progress)
            try:
                workbook.save(temporary)
                self._verify(temporary, count, cancel_requested)
            finally:
                for sheet in workbook.worksheets:
                    with suppress(FileNotFoundError):
                        if not sheet.closed:
                            sheet.close()
                        sheet._writer.cleanup()
                workbook.close()
            if cancel_requested is not None and cancel_requested():
                raise ExportCanceledError("Excel 내보내기를 취소했습니다.")
            os.replace(temporary, target)
            return target
        finally:
            temporary.unlink(missing_ok=True)

    def _build_workbook(
        self,
        tasks: Iterable[Task],
        cancel_requested: Callable[[], bool] | None,
        progress: Callable[[int], None] | None = None,
    ) -> tuple[Workbook, int]:
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("업무")
        sheet.sheet_view.showGridLines = False
        sheet.sheet_properties.tabColor = "274C77"
        widths = (9, 30, 42, 10, 10, 19, 19, 9, 30, 9, 42, 9, 19, 19)
        for index, width in enumerate(widths, start=1):
            sheet.column_dimensions[chr(64 + index)].width = width
        sheet.sheet_format.defaultRowHeight = 34
        sheet.row_dimensions[1].height = 24
        sheet.row_dimensions[4].height = 24
        sheet.freeze_panes = "A5"
        sheet.print_title_rows = "4:4"
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        title = WriteOnlyCell(sheet, "OfficeFlow 업무 목록")
        title.font = Font(name="Arial", size=14, bold=True, color="1F2937")
        sheet.append([title])
        sheet.append(["업무 목록 · 시작/종료 시각은 각 업무의 현지 시각"])
        sheet.append([])
        header_fill = PatternFill("solid", fgColor="274C77")
        header_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        separator = Side(style="thin", color="FFFFFF")
        headers = []
        for header in _HEADERS:
            cell = WriteOnlyCell(sheet, header)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = Border(right=separator)
            headers.append(cell)
        sheet.append(headers)
        count = 0
        iterator = iter(tasks)
        font = Font(name="Arial", size=10, color="1F2937")
        try:
            for task in iterator:
                if cancel_requested is not None and cancel_requested():
                    raise ExportCanceledError("Excel 내보내기를 취소했습니다.")
                if count >= 1_048_572:
                    raise ValueError("Excel 한 시트에 저장 가능한 업무 개수를 초과했습니다.")
                cells = []
                for column, value in enumerate(self._task_values(task), start=1):
                    cell = WriteOnlyCell(sheet, value)
                    if isinstance(value, str):
                        cell.data_type = "s"
                    cell.font = font
                    cell.alignment = Alignment(vertical="top", wrap_text=column in {2, 3, 9, 11})
                    if column in {6, 7, 13, 14} and isinstance(value, datetime):
                        cell.number_format = (
                            "yyyy-mm-dd"
                            if task.all_day and column in {6, 7}
                            else "yyyy-mm-dd hh:mm"
                        )
                    cells.append(cell)
                sheet.append(cells)
                count += 1
                if progress is not None and count % 100 == 0:
                    progress(count)
        except BaseException:
            # write-only worksheets own XML temp files even before Workbook.save.
            sheet.close()
            sheet._writer.cleanup()
            workbook.close()
            raise
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
        last_row = 4 + count
        if count:
            table = Table(
                displayName="OfficeFlowTasks",
                ref=f"A4:N{last_row}",
                tableColumns=[
                    TableColumn(id=index, name=header) for index, header in enumerate(_HEADERS, 1)
                ],
            )
            table.tableStyleInfo = TableStyleInfo(
                name="TableStyleMedium2",
                showFirstColumn=False,
                showLastColumn=False,
                showRowStripes=True,
                showColumnStripes=False,
            )
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore", message="In write-only mode you must add table columns manually"
                )
                sheet.add_table(table)
        sheet.auto_filter.ref = f"A4:N{last_row}"
        if progress is not None:
            progress(count)
        return workbook, count

    @staticmethod
    def _task_values(task: Task) -> tuple[object, ...]:
        zone = ZoneInfo(task.timezone)

        def local(value: datetime | None) -> datetime | None:
            return value.astimezone(zone).replace(tzinfo=None) if value is not None else None

        return (
            task.id,
            task.title,
            task.description,
            _STATUS_LABELS[task.status],
            _PRIORITY_LABELS[task.priority],
            local(task.starts_at),
            local(task.ends_at),
            "예" if task.all_day else "아니오",
            task.recurrence_rule or "",
            "예" if task.is_pinned else "아니오",
            task.result_note,
            "있음" if task.has_attachments else "없음",
            local(task.created_at),
            local(task.updated_at),
        )

    @staticmethod
    def _verify(
        path: Path, expected_rows: int, cancel_requested: Callable[[], bool] | None = None
    ) -> None:
        workbook = load_workbook(path, read_only=True, data_only=False)
        try:
            if workbook.sheetnames != ["업무"]:
                raise OSError("Excel 파일의 시트 구성이 올바르지 않습니다.")
            sheet = workbook["업무"]
            rows = sheet.iter_rows(values_only=True)
            for _ in range(3):
                next(rows, ())
            actual_headers = tuple(next(rows, ()))
            if actual_headers != _HEADERS:
                raise OSError("Excel 파일의 열 구성이 올바르지 않습니다.")
            actual_rows = 0
            for _row in rows:
                if cancel_requested is not None and cancel_requested():
                    raise ExportCanceledError("Excel 검증을 취소했습니다.")
                actual_rows += 1
            if actual_rows != expected_rows:
                raise OSError("Excel 파일의 업무 개수 검증에 실패했습니다.")
        finally:
            workbook.close()
