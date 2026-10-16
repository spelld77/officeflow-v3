from collections.abc import Callable
from datetime import date

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QDateEdit, QMessageBox, QVBoxLayout, QWidget

from officeflow.application.input_review import DateInput, InputNotice, review_dates
from officeflow.domain.task_history import fingerprint
from officeflow.presentation.compact_labels import CompactLabel
from officeflow.presentation.theme import LIGHT_STYLESHEET


class DateCaution(QWidget):
    """A compact, field-local warning; hidden notices take no layout space."""

    def __init__(self, edit: QDateEdit, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.edit = edit
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addWidget(edit)
        self.notice = CompactLabel(parent=self)
        self.notice.setObjectName("dateCautionNotice")
        layout.addWidget(self.notice)
        self.notice.hide()
        self.set_warning("")

    def set_warning(self, text: str, detail: str = "") -> None:
        self.notice.setText("주의 · " + text if text else "")
        self.notice.setToolTip(detail or text)
        self.notice.setAccessibleName(detail or text)
        self.notice.setVisible(bool(text))
        if bool(self.edit.property("dateCaution")) != bool(text):
            self.edit.setProperty("dateCaution", bool(text))
            self.edit.style().unpolish(self.edit)
            self.edit.style().polish(self.edit)
            self.edit.update()

    def review(self, value: DateInput, today: date) -> None:
        notices = review_dates((value,), today=today)
        far = value.is_new and value.far_check and abs((value.after - today).days) > 366
        if not any(item.confirm for item in notices) and not far:
            self.set_warning("")
            return
        if value.before.year != value.after.year:
            text = f"연도 변경: {value.before.year} → {value.after.year}"
        elif far:
            text = "오늘보다 1년 이상 " + ("전입니다" if value.after < today else "뒤입니다")
        else:
            text = f"날짜가 {abs((value.after - value.before).days)}일 변경되었습니다"
        self.set_warning(text, "\n".join(item.text for item in notices))


class DateReview(QObject):
    """Approve an immutable draft before writes; leave the event loop/alerts usable."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._owner = parent
        self._approved: str | None = None
        self.dialog: QMessageBox | None = None

    def allow(
        self, draft: object, notices: tuple[InputNotice, ...], retry: Callable[[], None]
    ) -> bool:
        signature = fingerprint(draft)
        if not any(item.confirm for item in notices) or signature == self._approved:
            return True
        if self.dialog is not None:
            self.dialog.raise_()
            return False
        dialog = QMessageBox(self._owner)
        dialog.setWindowTitle("날짜 확인")
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setText("연도 또는 날짜가 크게 바뀝니다. 이 날짜로 저장할까요?")
        dialog.setInformativeText("\n".join(item.text for item in notices if item.confirm))
        dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        dialog.button(QMessageBox.StandardButton.Yes).setText("이 날짜로 저장")
        dialog.button(QMessageBox.StandardButton.No).setText("돌아가기")
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setStyleSheet(LIGHT_STYLESHEET)
        self.dialog = dialog

        def finished(result: int) -> None:
            self.dialog = None
            dialog.deleteLater()
            if result == QMessageBox.StandardButton.Yes:
                self._approved = signature
                retry()  # Rebuild/validate, never persist the captured draft blindly.

        dialog.finished.connect(finished)
        dialog.show()
        return False

    def close(self) -> None:
        if self.dialog is not None:
            self.dialog.reject()
