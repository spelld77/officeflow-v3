from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.reminder_groups import ReminderGroup, ScopeKey
from officeflow.application.reminder_runtime import scope_label
from officeflow.presentation.dates import format_datetime
from officeflow.presentation.lookup_style import SCROLLBAR_STYLE
from officeflow.presentation.theme import LIGHT_STYLESHEET


def group_summary(group: ReminderGroup, timezone: str) -> str:
    item = group.representative
    scope = scope_label(item.task, item.occurrence_status)
    if group.pending_count:
        state = (
            "미확인"
            if item.status == "미확인"
            else "다음 " + format_datetime(item.effective_at, timezone).rsplit(" ", 1)[1]
        )
    elif group.attention_count:
        state = "발생 기록 확인 필요"
    else:
        state = "처리 이력"
    return f"{scope + ' · ' if scope else ''}알림 {len(group.items)}건 · {state}"


def group_text(group: ReminderGroup, timezone: str) -> str:
    item = group.representative
    attachment = " · 첨부" if group.task.has_attachments else ""
    return f"{format_datetime(item.effective_at, timezone, short=True)}  {group.task.title}{attachment} · {group_summary(group, timezone)}"


def group_tooltip(group: ReminderGroup, timezone: str) -> str:
    schedule = group.scheduled.starts_at if group.scheduled else group.task.starts_at
    return (
        group_text(group, timezone)
        + "\n업무 일정: "
        + (format_datetime(schedule, timezone) if schedule else "없음")
        + (
            "\n연결된 원래 반복 회차: " + format_datetime(group.occurrence_start, timezone)
            if group.occurrence_start
            else ""
        )
        + (
            "\n반복 업무 공통 알림(특정 회차 아님)"
            if group.task.recurrence_rule and group.occurrence_start is None
            else ""
        )
        + f"\n확인할/예정 {group.pending_count}건 · 발생 기록 확인 필요 {group.attention_count}건"
        + "\n개별 시각·상태는 '알림 시각 보기'에서 확인하세요."
    )


class ReminderGroupDetailsDialog(QDialog):
    recordRequested = Signal(object)
    settingsRequested = Signal(object)
    PAGE_SIZE = 100

    def __init__(
        self,
        group: ReminderGroup,
        timezone: str,
        *,
        complete: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._timezone = timezone
        self._group: ReminderGroup | None = group
        self._key = group.key
        self._page = 0
        self.setWindowTitle("알림 시각·상태")
        self.setStyleSheet(LIGHT_STYLESHEET + SCROLLBAR_STYLE)
        self.setMinimumSize(460, 340)
        self.resize(620, 430)
        layout = QVBoxLayout(self)
        self.heading = QLabel()
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setWordWrap(True)
        self.heading.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.heading)
        self.list = QListWidget()
        self.list.setObjectName("reminderPreviewList")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.list, 1)
        paging = QHBoxLayout()
        self.previous_button = QPushButton("이전")
        self.next_button = QPushButton("다음")
        self.page_label = QLabel()
        paging.addWidget(self.previous_button)
        paging.addWidget(self.page_label, 1)
        paging.addWidget(self.next_button)
        layout.addLayout(paging)
        actions = QHBoxLayout()
        self.records_button = QPushButton("업무 기록")
        self.settings_button = QPushButton("알림 설정…")
        self.close_button = QPushButton("닫기")
        for button in (self.records_button, self.settings_button, self.close_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.previous_button.clicked.connect(lambda: self._move(-1))
        self.next_button.clicked.connect(lambda: self._move(1))
        self.close_button.clicked.connect(self.reject)
        self.records_button.clicked.connect(self._records)
        self.settings_button.clicked.connect(self._settings)
        self.list.itemDoubleClicked.connect(lambda _row: self._records())
        self.set_group(group, complete=complete)

    @property
    def group_key(self) -> ScopeKey:
        return self._key

    def set_group(self, group: ReminderGroup | None, *, complete: bool = True) -> None:
        self._group = group
        if group:
            self._key = group.key
        self._complete = complete
        self._page = min(self._page, (len(group.items) - 1) // self.PAGE_SIZE) if group else 0
        self._fill()

    def _move(self, delta: int) -> None:
        self._page += delta
        self._fill()

    def _fill(self) -> None:
        selected = (
            self.list.currentItem().data(Qt.ItemDataRole.UserRole).key
            if self.list.currentItem()
            else None
        )
        self.list.clear()
        group = self._group
        if group:
            title = group.task.title
            self.heading.setText(
                f"{title[:100]}{'…' if len(title) > 100 else ''}\n{group_summary(group, self._timezone)}"
                + (" · 일부 조회" if not self._complete else "")
                + (
                    "\n반복 회차: " + format_datetime(group.occurrence_start, self._timezone)
                    if group.occurrence_start
                    else "\n업무 공통 알림"
                    if group.task.recurrence_rule
                    else ""
                )
            )
            self.heading.setToolTip(title)
            for alarm in group.items[
                self._page * self.PAGE_SIZE : (self._page + 1) * self.PAGE_SIZE
            ]:
                text = f"{format_datetime(alarm.effective_at, self._timezone)} · {alarm.display_status}"
                row = QListWidgetItem(text)
                row.setData(Qt.ItemDataRole.UserRole, alarm)
                row.setSizeHint(QSize(0, max(32, self.list.fontMetrics().height() + 12)))
                tooltip = (
                    text + "\n원래 예정: " + format_datetime(alarm.scheduled_at, self._timezone)
                )
                if alarm.snoozed_until:
                    tooltip += "\n다시 알림: " + format_datetime(
                        alarm.snoozed_until, self._timezone
                    )
                if alarm.actual_fired_at:
                    tooltip += "\n최근 발생: " + format_datetime(
                        alarm.actual_fired_at, self._timezone
                    )
                row.setToolTip(tooltip)
                self.list.addItem(row)
                if alarm.key == selected:
                    self.list.setCurrentItem(row)
        else:
            self.heading.setText("현재 날짜에 표시할 알림이 없습니다.")
        count = len(group.items) if group else 0
        self.previous_button.setEnabled(self._page > 0)
        self.next_button.setEnabled((self._page + 1) * self.PAGE_SIZE < count)
        self.previous_button.setVisible(count > self.PAGE_SIZE)
        self.next_button.setVisible(count > self.PAGE_SIZE)
        self.page_label.setText(
            f"{self._page + 1} / {max(1, (count + 99) // 100)}페이지 · {count}건"
            + (" 이상" if not self._complete else "")
        )
        self.records_button.setEnabled(bool(group))
        self.settings_button.setEnabled(bool(group))

    def _records(self) -> None:
        if self._group:
            selected = self.list.currentItem()
            self.recordRequested.emit(
                selected.data(Qt.ItemDataRole.UserRole) if selected else self._group.representative
            )

    def _settings(self) -> None:
        if self._group:
            self.settingsRequested.emit(self._group.task)
