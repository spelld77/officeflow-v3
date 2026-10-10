"""Calendar titles stay readable before secondary attachment information."""

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtGui import QFont, QFontDatabase

from officeflow.presentation.month_calendar import (
    CalendarPage,
    build_calendar_segments,
    calendar_segment_text,
)
from officeflow.presentation.theme import LIGHT_STYLESHEET
from tests.unit.test_month_calendar import _task


@pytest.mark.parametrize("width", [700, 1120])
def test_calendar_attachment_title_layout(qtbot, qapp, width):
    previous_font = QFont(qapp.font())
    try:
        if os.environ.get("OFFICEFLOW_RENDER_FONTS") == "1":
            for name in ("malgun.ttf", "malgunbd.ttf"):
                assert QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / name)) >= 0
            qapp.setFont(QFont("Malgun Gothic", 9))
        first = _task("계약 검토", datetime(2026, 10, 9, 1, tzinfo=UTC), None)
        first = replace(first, task=replace(first.task, has_attachments=True))
        long = _task(
            "정보시스템 점검 결과 보고와 첨부자료 확인",
            datetime(2026, 10, 10, 1, tzinfo=UTC),
            None,
            2,
        )
        long = replace(long, task=replace(long.task, has_attachments=True))
        span = _task(
            "기간 업무 검토",
            datetime(2026, 10, 12, tzinfo=UTC),
            datetime(2026, 10, 16, tzinfo=UTC),
            3,
        )
        span = replace(span, task=replace(span.task, has_attachments=True))
        page = CalendarPage(timezone="Asia/Seoul")
        qtbot.addWidget(page)
        page.setStyleSheet(LIGHT_STYLESHEET)
        page.calendar.set_month(2026, 10)
        page.calendar.set_selected_date(first.starts_at.date())
        page.set_tasks((first, long, span))
        page.resize(width, 760)
        page.show()
        qtbot.wait(40)
        page.calendar.grab()
        assert page.width() == width
        assert page.calendar._task_hits
        assert page.day_list.count() == 1
        row = page.day_list.item(0).text()
        assert row.index(first.title) < row.index("첨부")
        segments = build_calendar_segments(
            (first, long, span), page.calendar.visible_date_range[0], "Asia/Seoul"
        )
        for segment in segments:
            label = calendar_segment_text(segment)
            assert label.index(segment.task.title) < label.rindex("첨부")
        output = Path("artifacts/calendar-title-captures")
        output.mkdir(parents=True, exist_ok=True)
        assert page.grab().save(str(output / f"calendar-title-{width}.png"))
    finally:
        qapp.setFont(previous_font)
