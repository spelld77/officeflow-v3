"""Korean weekday labels without depending on the Windows display language."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QDateEdit

WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")


class _DateInputWidth(QObject):
    """Reserve text and arrow space after inherited fonts/styles are applied."""

    def __init__(self, edit: QDateEdit) -> None:
        super().__init__(edit)
        self._edit = edit
        edit.installEventFilter(self)

    def refresh(self) -> None:
        metrics = self._edit.fontMetrics()
        text_width = max(metrics.horizontalAdvance(f"8888-88-88 ({day})") for day in WEEKDAYS)
        self._edit.setMinimumWidth(max(self._edit.minimumSizeHint().width(), text_width + 50))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.Polish, QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.refresh()
        return super().eventFilter(watched, event)


def format_date(value: date, *, short: bool = False) -> str:
    return (
        f"{value:%m.%d}" + f"({WEEKDAYS[value.weekday()]})"
        if short
        else f"{value:%Y-%m-%d}({WEEKDAYS[value.weekday()]})"
    )


def format_datetime(value: datetime, timezone: str, *, short: bool = False) -> str:
    if value.tzinfo is None:
        raise ValueError("표시 시각에는 시간대 정보가 필요합니다.")
    local = value.astimezone(ZoneInfo(timezone))
    return f"{format_date(local.date(), short=short)} {local:%H:%M}"


def date_with_weekday(edit: QDateEdit) -> QDateEdit:
    """Show a Korean weekday inside the input, without a fourth editable section."""

    width = _DateInputWidth(edit)

    def refresh() -> None:
        display_format = f"yyyy-MM-dd '({WEEKDAYS[edit.date().dayOfWeek() - 1]})'"
        if edit.displayFormat() == display_format:
            width.refresh()
            return
        # The quoted weekday is display-only. Preserve the active date section
        # and cursor when a key or the calendar changes the selected date.
        section = edit.currentSection()
        line = edit.lineEdit()
        cursor = line.cursorPosition()
        selection = line.selectionStart()
        selected_length = len(line.selectedText())
        edit.setDisplayFormat(display_format)
        edit.setCurrentSection(section)
        if selection >= 0:
            line.setSelection(selection, selected_length)
        else:
            line.setCursorPosition(cursor)
        width.refresh()

    edit.dateChanged.connect(refresh)
    refresh()
    return edit
