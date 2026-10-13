import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QDialog, QDialogButtonBox

import officeflow.presentation.record_dialog as record_module
from officeflow.application.records import RecordService
from officeflow.application.tasks import TaskDraft
from officeflow.domain.attachment import validate_display_name
from officeflow.infrastructure.database.record_repository import SqlAlchemyRecordRepository
from officeflow.presentation.attachment_metadata import AttachmentMetadataDialog
from officeflow.presentation.record_dialog import TaskRecordsDialog
from tests.integration.test_attachment_search import search_environment


@pytest.mark.parametrize(
    "name", ["", "../file.txt", "CON.txt", "bad?.txt", "bad.txt ", "bad.", "other.pdf"]
)
def test_invalid_display_name_is_rejected(name):
    with pytest.raises(ValueError):
        validate_display_name(name, extension=".txt")


@pytest.mark.parametrize("edit_memo", [False, True])
def test_metadata_dialog_is_light_narrow_and_keeps_extension(qtbot, tmp_path, edit_memo):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="메타데이터"))
        attachment = env.add(task.id, "한글 신청서.txt")
        dialog = AttachmentMetadataDialog(attachment, edit_memo=edit_memo)
        qtbot.addWidget(dialog)
        dark = QPalette()
        dark.setColor(QPalette.ColorRole.Window, QColor("#202020"))
        dark.setColor(QPalette.ColorRole.WindowText, QColor("#000000"))
        dialog.setPalette(dark)
        dialog.resize(340, 300)
        dialog.show()
        qtbot.wait(20)
        assert dialog.palette().color(QPalette.ColorRole.Window).lightness() > 200
        save = dialog.buttons.button(QDialogButtonBox.StandardButton.Save)
        assert dialog.rect().contains(save.mapTo(dialog, save.rect().center()))
        if edit_memo:
            dialog.memo_edit.setPlainText("x" * 2001)
            qtbot.mouseClick(save, Qt.MouseButton.LeftButton)
            assert dialog.isVisible() and "2,000" in dialog.error_label.text()
            dialog.memo_edit.setPlainText("파일별 설명")
        else:
            dialog.name_edit.setText("새 신청서")
        qtbot.mouseClick(save, Qt.MouseButton.LeftButton)
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.value == ("파일별 설명" if edit_memo else "새 신청서.txt")


def test_record_menu_rename_memo_updates_list_and_export_name_without_touching_file(
    qtbot, tmp_path, monkeypatch
):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="첨부 변경"))
        source = tmp_path / "기존.txt"
        source.write_bytes(b"saved bytes")
        attached = env.attachments.attach(task.id, source)
        records = RecordService(SqlAlchemyRecordRepository(env.sessions), env.tasks)
        dialog = TaskRecordsDialog(
            task,
            task_service=env.tasks,
            record_service=records,
            attachment_service=env.attachments,
            initial_tab="attachments",
        )
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.select_attachment(attached.id)

        def edit(editor):
            if editor._edit_memo:
                editor.memo_edit.setPlainText("파일 설명")
            else:
                editor.name_edit.setText("현재이름")
            editor._save()
            return editor.result()

        monkeypatch.setattr(record_module.AttachmentMetadataDialog, "exec", edit)
        menu = dialog._build_attachment_context_menu()
        actions = {action.text(): action for action in menu.actions()}
        assert actions["이름 변경…"].isEnabled()
        actions["이름 변경…"].trigger()
        qtbot.waitUntil(lambda: "현재이름.txt" in dialog.attachment_list.item(0).text())
        menu = dialog._build_attachment_context_menu()
        next(action for action in menu.actions() if action.text() == "메모…").trigger()
        qtbot.waitUntil(lambda: "파일 설명" in dialog.attachment_list.item(0).toolTip())
        dialog._show_attachment_information()
        from PySide6.QtWidgets import QPlainTextEdit

        assert any(
            "파일 설명" in edit.toPlainText() for edit in dialog.findChildren(QPlainTextEdit)
        )
        assert "처음 첨부한 이름: 기존.txt" in dialog.attachment_detail.toolTip()
        saved = env.repository.get_attachment(attached.id)
        assert saved.name == "현재이름.txt" and saved.original_name == "기존.txt"
        assert env.attachments.path_for_open(attached.id).read_bytes() == b"saved bytes"
        assert "메모: 파일 설명" in dialog.attachment_list.item(0).toolTip()
        assert "\n" not in dialog.attachment_list.item(0).text()
        env.tasks.move_to_trash(task.id)
        menu = dialog._build_attachment_context_menu()
        assert not next(
            action for action in menu.actions() if action.text() == "이름 변경…"
        ).isEnabled()
        dialog.close()


def test_db_submission_failure_keeps_editor_and_user_draft(qtbot, tmp_path):
    with search_environment(tmp_path) as env:
        task = env.tasks.create(TaskDraft(title="입력 보존"))
        attached = env.add(task.id, "기존.txt")

        def failed(_value):
            raise ValueError("첨부 정보가 변경되었습니다. 창을 닫고 새로고침한 뒤 다시 시도하세요.")

        editor = AttachmentMetadataDialog(attached, submit=failed)
        qtbot.addWidget(editor)
        editor.show()
        editor.name_edit.setText("입력해둔 이름")
        editor._save()
        assert editor.isVisible() and editor.name_edit.text() == "입력해둔 이름"
        assert "첨부 정보가 변경" in editor.error_label.text()
        assert env.repository.get_attachment(attached.id).name == "기존.txt"
