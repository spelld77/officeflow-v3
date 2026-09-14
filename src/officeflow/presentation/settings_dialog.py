from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from officeflow.infrastructure.settings.store import AppSettings
from officeflow.infrastructure.windows.hotkey import parse_windows_hotkey


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._original = settings
        self._settings: AppSettings | None = None

        self.setWindowTitle("OfficeFlow 설정")
        self.setModal(True)
        self.resize(500, 390)
        self.setMinimumSize(440, 350)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)

        title = QLabel("실행 및 알림 설정")
        title.setObjectName("pageTitle")
        root.addWidget(title)

        caption = QLabel(
            "창을 닫았을 때의 동작과 Windows 빠른 등록 단축키를 설정합니다."
        )
        caption.setObjectName("mutedText")
        caption.setWordWrap(True)
        root.addWidget(caption)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setVerticalSpacing(12)

        self.minimize_to_tray_check = QCheckBox("닫기 버튼을 누르면 트레이로 숨기기")
        self.minimize_to_tray_check.setChecked(settings.minimize_to_tray)
        form.addRow("창 닫기", self.minimize_to_tray_check)

        self.start_with_windows_check = QCheckBox("Windows 로그인 시 OfficeFlow 실행")
        self.start_with_windows_check.setChecked(settings.start_with_windows)
        form.addRow("자동 실행", self.start_with_windows_check)

        self.shortcut_edit = QLineEdit(settings.global_quick_add_shortcut)
        self.shortcut_edit.setObjectName("globalShortcutEdit")
        self.shortcut_edit.setPlaceholderText("예: Ctrl+Alt+O")
        form.addRow("빠른 등록", self.shortcut_edit)

        self.grace_minutes_spin = QSpinBox()
        self.grace_minutes_spin.setObjectName("reminderGraceMinutes")
        self.grace_minutes_spin.setRange(1, 43_200)
        self.grace_minutes_spin.setSuffix("분")
        self.grace_minutes_spin.setValue(
            settings.missed_reminder_grace_minutes
        )
        form.addRow("놓친 알림 복구", self.grace_minutes_spin)
        root.addLayout(form)

        hint = QLabel(
            "전역 단축키는 Ctrl/Alt/Shift/Win과 영문·숫자·F1~F24 조합을 지원합니다."
        )
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.error_label = QLabel()
        self.error_label.setObjectName("formError")
        self.error_label.setStyleSheet("color: #C62828; font-weight: 600;")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        root.addWidget(self.error_label)
        root.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        save_button.setText("적용")
        save_button.setObjectName("primaryButton")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def settings(self) -> AppSettings:
        if self._settings is None:
            raise RuntimeError("설정 대화상자가 아직 적용되지 않았습니다.")
        return self._settings

    def _validate_and_accept(self) -> None:
        try:
            hotkey = parse_windows_hotkey(self.shortcut_edit.text())
            self._settings = replace(
                self._original,
                minimize_to_tray=self.minimize_to_tray_check.isChecked(),
                start_with_windows=self.start_with_windows_check.isChecked(),
                global_quick_add_shortcut=hotkey.display,
                missed_reminder_grace_minutes=self.grace_minutes_spin.value(),
            )
        except ValueError as error:
            self.error_label.setText(str(error))
            self.error_label.show()
            return
        self.accept()
