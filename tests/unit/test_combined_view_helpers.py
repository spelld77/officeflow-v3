import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from officeflow.domain.attachment import renamed_display_name
from officeflow.domain.enums import TaskStatus
from officeflow.domain.task import Task
from officeflow.infrastructure.settings.store import AppSettings, JsonSettingsStore
from officeflow.presentation.schedule_help import schedule_help

NOW = datetime(2026, 10, 14, 3, tzinfo=UTC)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("report", "report.docx"),
        ("report.docx", "report.docx"),
        ("report.DOCX", "report.docx"),
        ("report.v2.0", "report.v2.0.docx"),
        ("report.pdf", "report.pdf.docx"),
        ("한글 이름", "한글 이름.docx"),
    ],
)
def test_rename_stem_or_whole_filename(text, expected):
    assert renamed_display_name(text, ".docx") == expected


@pytest.mark.parametrize(
    "text", ["", ".docx", "r.docx.docx", "r.DOCX.docx", "r ", "r.", "../r", "CON", "r?", "x" * 500]
)
def test_rename_invalid_draft_is_rejected(text):
    with pytest.raises(ValueError):
        renamed_display_name(text, ".docx")


def test_extensionless_file_cannot_change_its_type():
    assert renamed_display_name("新이름", "") == "新이름"
    with pytest.raises(ValueError):
        renamed_display_name("new.txt", "")


@pytest.mark.parametrize(
    "start,end,expected",
    [
        (timedelta(days=6), None, "시작일은 6일 후"),
        (timedelta(hours=1), None, "오늘 시작 예정입니다"),
        (-timedelta(hours=1), None, "오늘 시작 예정 시각이 지났습니다"),
        (-timedelta(days=3), None, "시작일로부터 3일"),
        (-timedelta(days=2), timedelta(days=2), "종료일은 2일 후"),
        (-timedelta(hours=2), timedelta(hours=1), "오늘 종료 예정"),
        (-timedelta(hours=2), -timedelta(hours=1), "종료 예정 시각이 지났습니다"),
        (-timedelta(days=4), -timedelta(days=2), "종료일로부터 2일"),
    ],
)
def test_schedule_day_not_elapsed_hours(start, end, expected):
    task = Task.create(
        title="test", starts_at=NOW + start, ends_at=NOW + end if end else None, now=NOW
    )
    assert expected in schedule_help(task, NOW)
    assert "(수)" in schedule_help(task, NOW) or "2026-10" in schedule_help(task, NOW)
    assert "마감 초과" not in schedule_help(task, NOW)


def test_all_day_uses_last_included_date_and_live_clock():
    start = datetime(2026, 10, 13, 15, tzinfo=UTC)
    task = Task.create(
        title="종일", all_day=True, starts_at=start, ends_at=start + timedelta(days=1), now=NOW
    )
    assert "오늘 종료 예정" in schedule_help(task, NOW)
    assert "종료일로부터 1일" in schedule_help(task, start + timedelta(days=1))
    assert "2026-10-15" not in schedule_help(task, NOW)


@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.CANCELED, TaskStatus.ARCHIVED])
def test_closed_tasks_do_not_demand_future_work(status):
    task = Task.create(title="closed", starts_at=NOW + timedelta(days=10), now=NOW)
    text = schedule_help(replace(task, status=status), NOW)
    assert "상태:" in text and "일 후" not in text


def test_unknown_occurrence_and_unscheduled_are_not_guessed():
    task = Task.create(title="unknown", starts_at=NOW - timedelta(days=90), now=NOW)
    assert "회차 일정 확인 필요" in schedule_help(task, NOW, confirmed=False)
    assert "지났습니다" not in schedule_help(task, NOW, confirmed=False)
    assert schedule_help(replace(task, starts_at=None), NOW) == "일정 없음"
    with pytest.raises(ValueError):
        schedule_help(task, NOW.replace(tzinfo=None))


def test_schedule_year_leap_and_timezone_boundary():
    task = Task.create(title="date", starts_at=datetime(2028, 2, 29, 1, tzinfo=UTC), now=NOW)
    assert "시작일은 1일 후" in schedule_help(task, datetime(2028, 2, 28, 14, tzinfo=UTC))
    task = replace(task, starts_at=datetime(2027, 1, 1, 1, tzinfo=UTC))
    assert "오늘 시작 예정" in schedule_help(task, datetime(2026, 12, 31, 16, tzinfo=UTC))


@pytest.mark.parametrize("bad", ["", "relative", 7, "C:/bad\x00path", []])
def test_invalid_import_setting_preserves_existing_fields(tmp_path, bad):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {"compact_list": True, "window_width": 1400, "attachment_import_directory": bad}
        ),
        encoding="utf-8",
    )
    value = JsonSettingsStore(path).load()
    assert (
        value.compact_list
        and value.window_width == 1400
        and value.attachment_import_directory is None
    )


def test_import_setting_roundtrip_and_no_network_probe(tmp_path, monkeypatch):
    store = JsonSettingsStore(tmp_path / "settings.json")
    value = AppSettings(
        attachment_import_directory=str(tmp_path / "missing"), automatic_backup_interval_hours=36
    )
    store.save(value)
    monkeypatch.setattr(
        Path, "is_dir", lambda _self: (_ for _ in ()).throw(AssertionError("startup probe"))
    )
    assert store.load() == value
