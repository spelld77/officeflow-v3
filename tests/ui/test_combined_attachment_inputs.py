import pytest
from PySide6.QtGui import QColor, QImage

from officeflow.application.attachment_documents import DocumentOperation
from officeflow.application.attachments import AttachmentPreviewError
from officeflow.application.image_attachments import ImageFailure, ImageReadError
from officeflow.application.tasks import TaskDraft
from officeflow.presentation import image_attachments as images
from officeflow.presentation.attachment_metadata import AttachmentMetadataDialog
from officeflow.presentation.image_failures import image_error_label
from tests.integration.test_document_lifecycle import lifecycle


@pytest.mark.parametrize(
    "format_name,extension",
    [("PNG", ".png"), ("JPEG", ".jpg"), ("BMP", ".bmp"), ("WEBP", ".webp"), ("WEBP", ".png")],
)
def test_preview_reads_actual_format_without_modifying_source(
    qtbot, tmp_path, format_name, extension
):
    with lifecycle(tmp_path) as (env, _facade):
        task = env.tasks.create(TaskDraft("actual format"))
        picture = QImage(80, 60, QImage.Format.Format_RGB32)
        picture.fill(QColor("red"))
        path = tmp_path / ("small" + extension)
        assert picture.save(str(path), format_name)
        before = path.read_bytes()
        attached = env.attachments.attach(task.id, path)
        decoded = images.read_preview(env.attachments, attached.id, lambda: False)
        assert (decoded.width(), decoded.height()) == (80, 60)
        assert path.read_bytes() == before
        assert env.attachments.get_metadata(attached.id) == attached
        assert images.is_image_name(path.name)


def test_frozen_webp_is_verified_and_bad_hash_blocks_preview(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, facade):
        task = env.tasks.create(TaskDraft("frozen"))
        picture = QImage(30, 30, QImage.Format.Format_RGB32)
        picture.fill(QColor("red"))
        path = tmp_path / "a.webp"
        assert picture.save(str(path), "WEBP")
        attached = env.attachments.attach(task.id, path)
        facade.create(facade.catalog.get("attachment", attached.id), DocumentOperation.ORIGINAL)
        assert not images.read_preview(env.attachments, attached.id, lambda: False).isNull()
        fixed = env.attachments.get_metadata(attached.id)
        picture.fill(QColor("blue"))
        assert picture.save(str(env.attachments.storage_root / fixed.relative_path), "WEBP")
        with pytest.raises(AttachmentPreviewError) as result:
            images.read_preview(env.attachments, attached.id, lambda: False)
        assert result.value.kind is ImageFailure.INTEGRITY
        assert env.attachments.get_metadata(attached.id) == fixed


@pytest.mark.parametrize(
    "kind,label",
    [
        (ImageFailure.UNSUPPORTED, "형식 미지원"),
        (ImageFailure.SIZE, "크기 제한"),
        (ImageFailure.INTEGRITY, "고정 내용 불일치"),
        (ImageFailure.CHANGING, "다시 확인 필요"),
        (ImageFailure.DECODE, "미리보기 실패"),
        (ImageFailure.READ, "읽기 불가"),
    ],
)
def test_image_errors_have_explicit_reason(kind, label):
    assert image_error_label(ImageReadError(kind, "test")) == label
    assert image_error_label(AttachmentPreviewError(kind, "test")) == label


def test_size_and_unsupported_are_not_original_corruption(qtbot, tmp_path, monkeypatch):
    with lifecycle(tmp_path) as (env, facade):
        task = env.tasks.create(TaskDraft("bound"))
        path = tmp_path / "unsupported.png"
        path.write_bytes(b"not a supported image")
        attached = env.attachments.attach(task.id, path)
        facade.create(facade.catalog.get("attachment", attached.id), DocumentOperation.ORIGINAL)
        with pytest.raises(ImageReadError) as result:
            images.read_preview(env.attachments, attached.id, lambda: False)
        assert result.value.kind is ImageFailure.UNSUPPORTED
        assert "원본" not in image_error_label(result.value)
        assert image_error_label(PermissionError()) == "읽기 불가"
        assert image_error_label(FileNotFoundError()) == "파일 없음"
        assert image_error_label(ValueError("unknown")) == "미리보기 실패"


@pytest.mark.parametrize(
    "text,expected", [("new.txt", "new.txt"), ("new.TXT", "new.txt"), ("new.v2", "new.v2.txt")]
)
def test_metadata_whole_filename_has_preview_and_single_extension(qtbot, tmp_path, text, expected):
    with lifecycle(tmp_path) as (env, _facade):
        task = env.tasks.create(TaskDraft("rename"))
        path = tmp_path / "old.txt"
        path.write_bytes(b"data")
        attached = env.attachments.attach(task.id, path)
        dialog = AttachmentMetadataDialog(attached)
        qtbot.addWidget(dialog)
        dialog.name_edit.setText(text)
        assert expected in dialog.final_name.full_text
        dialog._save()
        assert dialog.value == expected


def test_existing_duplicate_unchanged_and_invalid_save_preserves_draft(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, _facade):
        task = env.tasks.create(TaskDraft("old name"))
        path = tmp_path / "old.txt.txt"
        path.write_bytes(b"data")
        attached = env.attachments.attach(task.id, path)
        first = AttachmentMetadataDialog(attached)
        qtbot.addWidget(first)
        first._save()
        assert first.value == "old.txt.txt"
        dialog = AttachmentMetadataDialog(attached)
        qtbot.addWidget(dialog)
        dialog.show()
        dialog.name_edit.setText("new.txt.txt")
        dialog._save()
        assert dialog.isVisible() and "여러 번" in dialog.error_label.text()
        assert dialog.name_edit.text() == "new.txt.txt"


def test_long_final_filename_stays_one_line_and_invalid_preview_clears_old_tooltip(qtbot, tmp_path):
    with lifecycle(tmp_path) as (env, _facade):
        task = env.tasks.create(TaskDraft("long preview"))
        path = tmp_path / "old.txt"
        path.write_bytes(b"data")
        attached = env.attachments.attach(task.id, path)
        dialog = AttachmentMetadataDialog(attached)
        qtbot.addWidget(dialog)
        dialog.resize(340, 250)
        dialog.show()
        name = "검토된_업무자료_" * 30 + ".txt"
        dialog.name_edit.setText(name)
        qtbot.wait(20)
        assert dialog.final_name.full_text == f"완성 이름: {name}"
        assert dialog.final_name.toolTip() == name
        assert dialog.final_name.height() <= dialog.final_name.fontMetrics().lineSpacing() + 6
        assert dialog.final_name.fontMetrics().horizontalAdvance(dialog.final_name.text()) <= (
            dialog.final_name.contentsRect().width()
        )
        dialog.name_edit.setText("invalid.txt.txt")
        assert "여러 번" in dialog.final_name.full_text
        assert dialog.final_name.toolTip() != name
