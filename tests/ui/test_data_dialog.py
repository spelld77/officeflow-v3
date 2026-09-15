from __future__ import annotations

from typing import cast

from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QPushButton
from pytestqt.qtbot import QtBot

from officeflow.application.exporting import ExportService
from officeflow.application.tasks import TaskQuery
from officeflow.infrastructure.backup import BackupManager
from officeflow.presentation.data_dialog import DataManagementDialog, OperationWorker


def test_data_dialog_exposes_all_phase_six_c_actions_at_minimum_size(qtbot: QtBot) -> None:
    dialog = DataManagementDialog(
        export_service=cast(ExportService, object()),
        backup_manager=cast(BackupManager, object()),
        query=TaskQuery(),
    )
    qtbot.addWidget(dialog)
    dialog.resize(480, 390)
    dialog.show()

    labels = {button.text() for button in dialog.findChildren(QPushButton)}
    assert {"현재 목록 Excel", "전체 일정 ICS", "지금 백업", "백업에서 복원", "닫기"} <= labels
    assert dialog.width() >= 480
    assert dialog.height() >= 390


def test_operation_worker_reports_success_failure_and_cancel() -> None:
    success = OperationWorker(lambda canceled: "canceled" if canceled() else "done")
    success_spy = QSignalSpy(success.succeeded)
    success.run()
    assert success_spy.count() == 1
    assert success_spy.at(0)[0] == "done"

    canceled = OperationWorker(lambda is_canceled: is_canceled())
    canceled_spy = QSignalSpy(canceled.succeeded)
    canceled.cancel()
    canceled.run()
    assert canceled_spy.at(0)[0] is True

    failed = OperationWorker(lambda _canceled: (_ for _ in ()).throw(ValueError("failed")))
    failed_spy = QSignalSpy(failed.failed)
    failed.run()
    assert isinstance(failed_spy.at(0)[0], ValueError)
