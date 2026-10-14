"""Bounded clipboard drafts, asynchronous PNG import and read-only image viewing."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from uuid import uuid4

from PySide6.QtCore import (
    QFile,
    QIODevice,
    QObject,
    QSignalBlocker,
    QSize,
    Qt,
    QThread,
    Signal,
    Slot,
)
from PySide6.QtGui import QImage, QImageReader, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.attachments import (
    AttachmentCanceledError,
    AttachmentService,
    DuplicateAttachmentError,
)
from officeflow.application.image_attachments import (
    IMAGE_EXTENSIONS,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_PENDING_BYTES,
    MAX_PENDING_IMAGES,
    ImageFailure,
    ImageReadError,
    image_filename,
    suggested_image_name,
)
from officeflow.domain.attachment import Attachment
from officeflow.presentation.background import OperationWorker, finish_thread
from officeflow.presentation.image_failures import image_error_label
from officeflow.presentation.theme import LIGHT_STYLESHEET

if TYPE_CHECKING:
    from officeflow.presentation.attachment_thumbnails import AttachmentImageCoordinator


def is_image_name(name: str) -> bool:
    return Path(name).suffix.casefold() in IMAGE_EXTENSIONS


def check_image(image: QImage) -> None:
    if image.isNull():
        raise ValueError("클립보드에 사용할 수 있는 이미지가 없습니다.")
    if image.width() * image.height() > MAX_IMAGE_PIXELS or image.sizeInBytes() > MAX_IMAGE_BYTES:
        raise ValueError("이미지가 너무 큽니다. 1,600만 픽셀·64 MB 이내로 캡처하세요.")


@dataclass(frozen=True)
class ImageReadResult:
    image: QImage
    fingerprint: tuple[int, int, int, int, int]
    decoded: bool = True


def read_bounded_image(
    service: AttachmentService,
    identity: int,
    canceled: Callable[[], bool],
    *,
    target: QSize,
    expected: Attachment | None = None,
    cached: ImageReadResult | None = None,
    background: bool = False,
) -> ImageReadResult:
    with service.image_preview_stream(
        identity,
        cancel_requested=canceled,
        expected_asset_id=expected.asset_id if expected else None,
        expected_relative_path=expected.relative_path if expected else None,
        expected_frozen=expected.is_frozen if expected else None,
        expected_fixed_size_bytes=expected.fixed_size_bytes if expected else None,
        expected_fixed_checksum=expected.fixed_checksum if expected else None,
        background=background,
    ) as stream:

        def fingerprint() -> tuple[int, int, int, int, int]:
            info = os.fstat(stream.fileno())
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        before = fingerprint()
        if cached is not None and cached.fingerprint == before:
            return ImageReadResult(cached.image, before, False)
        device = QFile()
        if not device.open(
            stream.fileno(), QIODevice.OpenModeFlag.ReadOnly, QFile.FileHandleFlag.DontCloseHandle
        ):
            raise ImageReadError(ImageFailure.READ, "이미지를 읽을 수 없습니다.")
        try:
            reader = QImageReader(device)
            reader.setDecideFormatFromContent(True)
            if bytes(reader.format().data()).lower() not in {
                b"png",
                b"jpeg",
                b"jpg",
                b"bmp",
                b"webp",
            }:
                raise ImageReadError(
                    ImageFailure.UNSUPPORTED, "PNG·JPG·BMP·WebP 이미지의 미리보기만 지원합니다."
                )
            size = reader.size()
            if not size.isValid():
                raise ImageReadError(ImageFailure.DECODE, "이미지 크기를 확인할 수 없습니다.")
            if size.width() * size.height() > MAX_IMAGE_PIXELS:
                raise ImageReadError(ImageFailure.SIZE, "이미지가 1,600만 픽셀을 넘습니다.")
            if size.width() > target.width() or size.height() > target.height():
                reader.setScaledSize(size.scaled(target, Qt.AspectRatioMode.KeepAspectRatio))
            reader.setAutoTransform(True)
            if canceled():
                raise AttachmentCanceledError("미리보기를 취소했습니다.")
            image = reader.read()
            if image.isNull():
                raise ImageReadError(ImageFailure.DECODE, "이미지 내용을 해석할 수 없습니다.")
            if (
                image.width() * image.height() > MAX_IMAGE_PIXELS
                or image.sizeInBytes() > MAX_IMAGE_BYTES
            ):
                raise ImageReadError(ImageFailure.SIZE, "이미지가 미리보기 크기 제한을 넘습니다.")
            if canceled():
                raise AttachmentCanceledError("미리보기를 취소했습니다.")
            if fingerprint() != before:
                raise ImageReadError(
                    ImageFailure.CHANGING, "읽는 중 파일이 변경되었습니다. 저장 후 다시 확인하세요."
                )
            if image.width() > target.width() or image.height() > target.height():
                image = image.scaled(
                    target,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            return ImageReadResult(image, before)
        finally:
            device.close()


def read_preview(service: AttachmentService, identity: int, canceled: Callable[[], bool]) -> QImage:
    return read_bounded_image(service, identity, canceled, target=QSize(1200, 900)).image


class ImagePreviewDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        coordinator: AttachmentImageCoordinator | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("이미지 미리보기")
        self.setModal(False)
        self.setStyleSheet(LIGHT_STYLESHEET)
        self.resize(620, 460)
        self._image = QImage()
        self._thread: QThread | None = None
        self._worker: OperationWorker | None = None
        self._pending: tuple[AttachmentService, int, str] | None = None
        self._generation = 0
        self._closing = False
        self._coordinator = coordinator
        self._client = coordinator.register() if coordinator else None
        self._shared_identity: int | None = None
        if coordinator:
            coordinator.updated.connect(self._shared_updated)
        layout = QVBoxLayout(self)
        self.caption = QLabel()
        self.caption.setTextFormat(Qt.TextFormat.PlainText)
        self.caption.setWordWrap(True)
        self.caption.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.caption)
        self.picture = QLabel("이미지를 선택하세요.")
        self.picture.setTextFormat(Qt.TextFormat.PlainText)
        self.picture.setWordWrap(True)
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setMinimumSize(160, 100)
        self.picture.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        layout.addWidget(self.picture, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        close.rejected.connect(self.reject)
        layout.addWidget(close)

    def show_image(self, image: QImage, name: str) -> None:
        self.clear()
        self.caption.setText(name)
        self._image = image.scaled(
            1200,
            900,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._render()

    def clear(self, message: str = "미리보기할 이미지 한 개를 선택하세요.") -> None:
        self._generation += 1
        self._pending = None
        self._shared_identity = None
        if self._coordinator and self._client is not None:
            self._coordinator.sync(self._client, ())
        if self._worker:
            self._worker.cancel()
        self._image = QImage()
        self.caption.clear()
        self.picture.clear()
        self.picture.setToolTip("")
        self.picture.setText(message)

    def load(self, service: AttachmentService, identity: int, name: str) -> None:
        self.clear("이미지를 읽고 있습니다…")
        self.caption.setText(name)
        if self._coordinator and self._client is None:
            self._client = self._coordinator.register()
        if self._coordinator and self._client is not None:
            self._shared_identity = identity
            try:
                attachment = service.get_metadata(identity)
            except (LookupError, ValueError) as error:
                self.picture.setText(str(error))
                return
            self._coordinator.sync(
                self._client,
                ((service, attachment, QSize(1200, 900)),),
                epoch=self._generation,
                expanded=True,
            )
            return
        self._pending = (service, identity, name)
        self._start()

    @Slot(int)
    def _shared_updated(self, client: int) -> None:
        if (
            client != self._client
            or self._shared_identity is None
            or not self._coordinator
            or self._closing
        ):
            return
        state = self._coordinator.state_for(client, self._shared_identity)
        picture = self._coordinator.image_for(client, self._shared_identity)
        if picture is not None:
            self._image = picture
            self._render()
        elif state and state.error:
            self.picture.setText(state.label)
            self.picture.setToolTip(state.error)

    def _start(self) -> None:
        if self._thread is not None or self._pending is None or self._closing:
            return
        service, identity, _name = self._pending
        self._pending = None
        generation = self._generation
        thread = QThread(self)
        worker = OperationWorker(
            lambda canceled: (generation, read_preview(service, identity, canceled))
        )
        worker.moveToThread(thread)
        self._active_generation = generation
        worker.succeeded.connect(self._loaded, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.finished.connect(worker.deleteLater)
        thread.started.connect(worker.run)
        thread.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        self._thread, self._worker = thread, worker
        thread.start()

    @Slot(object)
    def _loaded(self, value: object) -> None:
        if not isinstance(value, tuple) or len(value) != 2:
            return
        generation, result = value
        if generation == self._generation and not self._closing and isinstance(result, QImage):
            self._image = result
            self._render()

    @Slot(object)
    def _failed(self, error: object) -> None:
        if (
            self._active_generation == self._generation
            and not self._closing
            and not isinstance(error, AttachmentCanceledError)
        ):
            self.picture.setText(image_error_label(error))
            self.picture.setToolTip(str(error))

    @Slot()
    def _finished(self) -> None:
        thread = self._thread
        if thread:
            thread.wait()
            thread.deleteLater()
        # Keep the Python worker wrapper alive through native thread-local cleanup.
        self._thread, self._worker = None, None
        self._start()

    def _render(self) -> None:
        if not self._image.isNull():
            self.picture.setPixmap(
                QPixmap.fromImage(self._image).scaled(
                    self.picture.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._render()

    def done(self, result: int) -> None:
        self._closing = True
        self.clear()
        if self._coordinator and self._client is not None:
            self._coordinator.unregister(self._client)
            self._client = None
        if self._thread:
            self._thread.quit()
            finish_thread(self._thread, parent=self)
        super().done(result)
        self._closing = False


@dataclass(frozen=True)
class DraftImage:
    key: str
    name: str
    image: QImage


@dataclass(frozen=True)
class ImageImportResult:
    completed: tuple[str, ...]
    errors: tuple[str, ...]
    duplicates: int = 0

    @property
    def notice(self) -> str:
        added = len(self.completed) - self.duplicates
        suffix = f" · 같은 사진 {self.duplicates}장은 이미 첨부됨" if self.duplicates else ""
        return f"사진 {added}장 첨부됨{suffix}"

    @property
    def error_summary(self) -> str:
        if not self.errors:
            return ""
        first = " ".join(self.errors[0].split())
        first = first[:160] + ("…" if len(first) > 160 else "")
        suffix = f" · 추가 오류 {len(self.errors) - 1}건" if len(self.errors) > 1 else ""
        return first + suffix


def import_images(
    service: AttachmentService,
    task_id: int,
    entries: tuple[DraftImage, ...],
    canceled: Callable[[], bool],
) -> ImageImportResult:
    completed: list[str] = []
    errors: list[str] = []
    duplicates = 0
    for entry in entries:
        if canceled():
            errors.append("사진 첨부를 취소했습니다. 미처리 사진은 대기 목록에 남습니다.")
            break
        try:
            with TemporaryDirectory(prefix="officeflow-image-") as directory:
                path = Path(directory) / image_filename(entry.name)
                if not entry.image.save(str(path)):
                    raise ValueError("사진을 PNG 파일로 준비하지 못했습니다.")
                try:
                    service.attach(task_id, path, cancel_requested=canceled)
                except DuplicateAttachmentError:
                    duplicates += 1
            completed.append(entry.key)
        except Exception as error:
            errors.append(f"{entry.name}: {error}")
    return ImageImportResult(tuple(completed), tuple(errors), duplicates)


class ImageImportController(QObject):
    completed = Signal(object)
    busyChanged = Signal(bool)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._parent = parent
        self._thread: QThread | None = None
        self._worker: OperationWorker | None = None
        self._result = ImageImportResult((), ())
        self._closing = False

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def start(
        self, service: AttachmentService, task_id: int, entries: tuple[DraftImage, ...]
    ) -> None:
        if self.busy or self._closing:
            return
        self._result = ImageImportResult((), ())
        thread = QThread(self)
        worker = OperationWorker(
            lambda canceled: import_images(service, task_id, entries, canceled)
        )
        worker.moveToThread(thread)
        worker.succeeded.connect(self._succeeded, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.finished.connect(worker.deleteLater)
        thread.started.connect(worker.run)
        thread.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        self._thread, self._worker = thread, worker
        self.busyChanged.emit(True)
        thread.start()

    @Slot(object)
    def _succeeded(self, result: object) -> None:
        if isinstance(result, ImageImportResult):
            self._result = result

    @Slot(object)
    def _failed(self, error: object) -> None:
        self._result = ImageImportResult((), (str(error),))

    @Slot()
    def _finished(self) -> None:
        thread = self._thread
        if thread:
            thread.wait()
            thread.deleteLater()
        # A finished signal can arrive before the worker's native cleanup completes.
        self._thread, self._worker = None, None
        self.busyChanged.emit(False)
        if not self._closing:
            self.completed.emit(self._result)

    def shutdown(self) -> None:
        self._closing = True
        if self._worker:
            self._worker.cancel()
        if self._thread:
            self._thread.quit()
            finish_thread(self._thread, parent=self._parent)


class ClipboardImagePanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[DraftImage] = []
        self._preview: ImagePreviewDialog | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        row = QHBoxLayout()
        self.add_button = QPushButton("클립보드 이미지 추가")
        self.add_button.setAutoDefault(False)
        self.add_button.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.add_button.clicked.connect(self.add_clipboard)
        row.addWidget(self.add_button)
        self.selector = QComboBox()
        self.selector.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.selector.currentIndexChanged.connect(self._selected)
        row.addWidget(self.selector, 1)
        layout.addLayout(row)
        self.card = QWidget()
        card = QHBoxLayout(self.card)
        card.setContentsMargins(0, 0, 0, 0)
        self.thumbnail = QLabel()
        self.thumbnail.setFixedSize(80, 56)
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card.addWidget(self.thumbnail)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("사진 이름 (.png)")
        self.name_edit.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.name_edit.textEdited.connect(self._rename)
        card.addWidget(self.name_edit, 1)
        self.preview_button = QPushButton("보기")
        self.remove_button = QPushButton("제거")
        for button in (self.preview_button, self.remove_button):
            button.setAutoDefault(False)
            button.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            card.addWidget(button)
        self.preview_button.clicked.connect(self._show_preview)
        self.remove_button.clicked.connect(self._remove)
        layout.addWidget(self.card)
        self.feedback = QLabel()
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        self._refresh()

    @property
    def has_images(self) -> bool:
        return bool(self._entries)

    def entries(self) -> tuple[DraftImage, ...]:
        return tuple(DraftImage(e.key, image_filename(e.name), e.image) for e in self._entries)

    def add_clipboard(self) -> None:
        try:
            if len(self._entries) >= MAX_PENDING_IMAGES:
                raise ValueError("사진은 한 번에 8장까지 추가할 수 있습니다.")
            mime = QApplication.clipboard().mimeData()
            if mime is None or not mime.hasImage():
                raise ValueError("클립보드에 이미지가 없습니다. 먼저 화면을 캡처하세요.")
            image = QApplication.clipboard().image()
            check_image(image)
            if (
                sum(e.image.sizeInBytes() for e in self._entries) + image.sizeInBytes()
                > MAX_PENDING_BYTES
            ):
                raise ValueError("추가 대기 사진의 합계는 128 MB 이내여야 합니다.")
            name = suggested_image_name(datetime.now(), tuple(e.name for e in self._entries))
            self._entries.append(DraftImage(str(uuid4()), name, image.copy()))
            self.feedback.setText("저장 전 · 업무 저장 또는 첨부 버튼을 눌러야 등록됩니다.")
            self._refresh(len(self._entries) - 1)
        except Exception as error:
            self.feedback.setText(str(error))

    def remove_completed(self, keys: tuple[str, ...]) -> None:
        self._entries = [e for e in self._entries if e.key not in keys]
        self._refresh()

    def _refresh(self, index: int = 0) -> None:
        with QSignalBlocker(self.selector):
            self.selector.clear()
            for number, _entry in enumerate(self._entries, 1):
                self.selector.addItem(f"추가 대기 {number}/{len(self._entries)}")
            self.selector.setCurrentIndex(min(index, len(self._entries) - 1))
        self.selector.setVisible(self.has_images)
        self.card.setVisible(self.has_images)
        self._selected()

    def _selected(self) -> None:
        index = self.selector.currentIndex()
        if index < 0:
            self.thumbnail.clear()
            self.name_edit.clear()
            if self._preview:
                self._preview.clear()
            return
        entry = self._entries[index]
        self.name_edit.setText(entry.name)
        self.thumbnail.setPixmap(
            QPixmap.fromImage(
                entry.image.scaled(
                    self.thumbnail.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        )
        if self._preview and self._preview.isVisible():
            self._preview.show_image(entry.image, entry.name)

    def _rename(self, name: str) -> None:
        index = self.selector.currentIndex()
        if index >= 0:
            entry = self._entries[index]
            self._entries[index] = DraftImage(entry.key, name, entry.image)

    def _remove(self) -> None:
        index = self.selector.currentIndex()
        if index >= 0:
            self._entries.pop(index)
            self._refresh()

    def _show_preview(self) -> None:
        index = self.selector.currentIndex()
        if index < 0:
            return
        if self._preview is None:
            self._preview = ImagePreviewDialog(self)
        entry = self._entries[index]
        self._preview.show_image(entry.image, entry.name)
        self._preview.show()
        self._preview.raise_()

    def shutdown(self) -> None:
        if self._preview:
            self._preview.reject()
        self._entries.clear()


class ClipboardAttachmentDialog(QDialog):
    changed = Signal()
    notice = Signal(str)

    def __init__(self, service: AttachmentService, task_id: int, parent: QWidget) -> None:
        super().__init__(parent)
        self._service, self._task_id = service, task_id
        self.setWindowTitle("클립보드 이미지 첨부")
        self.setStyleSheet(LIGHT_STYLESHEET)
        self.resize(540, 240)
        layout = QVBoxLayout(self)
        self.panel = ClipboardImagePanel(self)
        layout.addWidget(self.panel)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("첨부")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.controller = ImageImportController(self)
        self.controller.busyChanged.connect(self._busy)
        self.controller.completed.connect(self._completed)
        self.panel.add_clipboard()

    def _busy(self, busy: bool) -> None:
        self.panel.setEnabled(not busy)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(not busy)
        if busy:
            self.panel.feedback.setText("사진을 첨부하고 있습니다…")

    def _save(self) -> None:
        try:
            entries = self.panel.entries()
            if not entries:
                raise ValueError("먼저 사진을 추가하세요.")
            self.controller.start(self._service, self._task_id, entries)
        except Exception as error:
            self.panel.feedback.setText(str(error))

    def _completed(self, result: ImageImportResult) -> None:
        self.panel.remove_completed(result.completed)
        if result.completed:
            self.changed.emit()
            self.notice.emit(result.notice)
        if result.errors:
            self.panel.feedback.setText(result.error_summary)
            self.panel.feedback.setToolTip("\n".join(result.errors))
        else:
            self.accept()

    def done(self, result: int) -> None:
        self.controller.shutdown()
        self.panel.shutdown()
        super().done(result)
