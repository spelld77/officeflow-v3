from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from officeflow.application.folder_links import FolderLink, FolderLinkService, FolderRelinkCandidate
from officeflow.presentation.background import OperationWorker, ShutdownOwner, ShutdownScope
from officeflow.presentation.compact_labels import CompactLabel
from officeflow.presentation.detail_widgets import CollapsibleText, show_text_details
from officeflow.presentation.theme import LIGHT_STYLESHEET


class FolderLinksPanel(ShutdownOwner, QWidget):
    changed = Signal()

    def __init__(
        self,
        service: FolderLinkService,
        task_id: int,
        *,
        read_only: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._shutdown_scope = ShutdownScope(self, self._shutdown_completed)
        self.setObjectName("folderLinksPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            LIGHT_STYLESHEET + "\nQWidget#folderLinksPanel {background:#F5F7FA;color:#172033;}"
        )
        self.service, self.task_id, self.read_only = service, task_id, read_only
        self._job_thread: QThread | None = None
        self.worker: OperationWorker | None = None
        self._opening = False
        self._shutting_down = False
        self._links: dict[int, FolderLink] = {}
        self._candidate: FolderRelinkCandidate | None = None
        self._choice_dialog: QMessageBox | None = None
        self._updating_link = False
        layout = QVBoxLayout(self)
        description = QLabel("외부 폴더의 파일은 OfficeFlow 백업에 포함되지 않습니다.")
        description.setWordWrap(True)
        layout.addWidget(description)
        self.list = QListWidget()
        self.list.setObjectName("folderLinksList")
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.currentItemChanged.connect(self._selection_changed)
        self.list.itemDoubleClicked.connect(lambda _item: self.open_selected())
        layout.addWidget(self.list, 1)
        self.path = CompactLabel("연결된 폴더가 없습니다.", elide_mode=Qt.TextElideMode.ElideMiddle)
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path)
        self.buttons: dict[str, QPushButton] = {}
        row = QHBoxLayout()
        for key, label, callback in (
            ("open", "열기", self.open_selected),
            ("add", "추가…", self.add),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            self.buttons[key] = button
            row.addWidget(button)
        self.more_button = QPushButton("더보기")
        self.more_menu = QMenu(self.more_button)
        self.more_menu.setStyleSheet(LIGHT_STYLESHEET)
        self.menu_actions = {}
        for key, label, callback in (
            ("path", "전체 경로…", self._show_path),
            ("rename", "표시 이름 변경…", self.rename),
            ("relink", "폴더 다시 지정…", self.relink),
            ("delete", "연결 삭제…", self.delete),
            ("refresh", "새로고침", self.refresh),
        ):
            self.menu_actions[key] = self.more_menu.addAction(label, callback)
        self.more_menu.aboutToShow.connect(self._selection_changed)
        self.more_button.setMenu(self.more_menu)
        row.addWidget(self.more_button)
        layout.addLayout(row)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(
            CollapsibleText(
                "폴더 연결 안내",
                "목록의 이름만 바꾸며 실제 폴더는 바꾸지 않습니다. OfficeFlow는 폴더의 파일을 복사·삭제하거나 관리하지 않습니다. 새로 연결하거나 다시 지정한 로컬 NTFS 폴더는 같은 상위 폴더에서 이름이 바뀌면 후보를 확인할 수 있습니다. 기존 연결이나 확인할 수 없는 폴더는 폴더 다시 지정을 사용하세요.",
            )
        )
        self.status = QLabel("")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.refresh()

    def refresh(self) -> None:
        identity = self.selected.id if self.selected else None
        try:
            links = self.service.list(self.task_id)
        except Exception as error:
            self.status.setText(f"폴더 조회 실패: {error}")
            return
        self._links = {link.id: link for link in links}
        self.list.clear()
        for link in links:
            item = QListWidgetItem(link.name)
            item.setToolTip(f"{link.name}\n{link.path}")
            item.setData(Qt.ItemDataRole.UserRole, link.id)
            self.list.addItem(item)
            if link.id == identity:
                self.list.setCurrentItem(item)
        if self.list.count() and self.list.currentItem() is None:
            self.list.setCurrentRow(0)
        self._selection_changed()

    @property
    def selected(self) -> FolderLink | None:
        item = self.list.currentItem()
        return self._links.get(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _selection_changed(self, *_args: object) -> None:
        link = self.selected
        self.path.setText(link.path if link else "연결된 폴더가 없습니다.")
        self.path.setToolTip(link.path if link else "")
        controls: dict[str, QAction | QPushButton] = {**self.buttons, **self.menu_actions}
        for key, action in controls.items():
            action.setEnabled(
                self._job_thread is None
                and (link is not None or key in {"add", "refresh"})
                and (not self.read_only or key in {"open", "path", "refresh"})
            )
        self.more_button.setEnabled(self._job_thread is None)

    def _show_path(self) -> None:
        if self.selected:
            self._path_dialog = show_text_details(
                self, "전체 경로", f"{self.selected.name}\n{self.selected.path}"
            )

    def _context_menu(self, point: object) -> None:
        from PySide6.QtCore import QPoint

        if not isinstance(point, QPoint):
            return
        item = self.list.itemAt(point)
        if item:
            self.list.setCurrentItem(item)
        self.more_menu.exec(self.list.viewport().mapToGlobal(point))

    def _editable(self) -> bool:
        return not self.read_only and self._job_thread is None and not self._shutting_down

    def add(self) -> None:
        if not self._editable():
            return
        directory = QFileDialog.getExistingDirectory(self, "외부 업무 폴더 선택")
        if directory:
            path = Path(directory)
            name, accepted = QInputDialog.getText(
                self, "폴더 이름", "목록에 표시할 이름", text=path.name or str(path)
            )
            if accepted:
                self._run(
                    lambda canceled: self.service.save(self.task_id, name, path, canceled=canceled)
                )

    def relink(self) -> None:
        if not self._editable():
            return
        link = self.selected
        if link is None:
            return
        directory = QFileDialog.getExistingDirectory(self, "새 외부 폴더 선택")
        if directory:
            self._run(
                lambda canceled: self.service.save(
                    self.task_id,
                    link.name,
                    Path(directory),
                    identity=link.id,
                    expected=link,
                    canceled=canceled,
                )
            )

    def rename(self) -> None:
        if not self._editable():
            return
        link = self.selected
        if link is None:
            return
        name, accepted = QInputDialog.getText(
            self, "폴더 이름 변경", "목록에 표시할 이름", text=link.name
        )
        if accepted:
            self._run(lambda _canceled: self.service.rename(self.task_id, link, name))

    def delete(self) -> None:
        if not self._editable():
            return
        link = self.selected
        if (
            link
            and QMessageBox.question(
                self,
                "연결 삭제",
                f"'{link.name}' 연결을 삭제할까요?\n실제 폴더와 파일은 삭제하지 않습니다.",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self._run(lambda _canceled: self.service.delete(self.task_id, link.id))

    def open_selected(self) -> None:
        link = self.selected
        if link:
            self._run(lambda canceled: self.service.inspect_open(link, canceled), opening=True)

    def _run(
        self,
        operation: Callable[[Callable[[], bool]], object],
        *,
        opening: bool = False,
        updating_link: bool = False,
    ) -> None:
        if self._job_thread or self._shutting_down:
            return
        self._opening = opening
        self._updating_link = updating_link
        self._job_thread = QThread(self)
        self.worker = OperationWorker(operation)
        self.worker.moveToThread(self._job_thread)
        self._job_thread.started.connect(self.worker.run)
        self.worker.succeeded.connect(self._succeeded, Qt.ConnectionType.QueuedConnection)
        self.worker.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self._job_thread.quit, Qt.ConnectionType.DirectConnection)
        self.worker.finished.connect(self.worker.deleteLater)
        self._shutdown_scope.track(
            self._job_thread, self.worker, self._finished, self.worker.cancel
        )
        self._selection_changed()
        self.status.setText("폴더를 확인하고 있습니다…")
        self._job_thread.start()

    @Slot(object)
    def _succeeded(self, result: object) -> None:
        if self._shutting_down:
            return
        if isinstance(result, FolderRelinkCandidate):
            self._candidate = result
            return
        if self._opening and isinstance(result, Path):
            if self._updating_link:
                self.refresh()
                self.changed.emit()
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(result)))
            self.status.setText(
                "폴더를 열었습니다." if opened else "탐색기를 열지 못했습니다. 연결은 유지됩니다."
            )
        else:
            self.refresh()
            self.changed.emit()
            self.status.setText("폴더 연결을 반영했습니다.")

    @Slot(object)
    def _failed(self, error: object) -> None:
        if not self._shutting_down:
            self.status.setText(str(error))

    @Slot()
    def _finished(self) -> None:
        thread = self._job_thread
        if thread:
            assert thread.wait(0)
            thread.deleteLater()
        self._job_thread, self.worker = None, None
        if not self._shutting_down:
            self._selection_changed()
            candidate, self._candidate = self._candidate, None
            if candidate:
                self._confirm_candidate(candidate)

    def _confirm_candidate(self, candidate: FolderRelinkCandidate) -> None:
        if self.selected != candidate.link:
            return
        if self.read_only:
            self.status.setText(
                "폴더 위치가 바뀌었습니다. 업무를 복원한 뒤 폴더를 다시 지정하세요. 연결은 유지했습니다."
            )
            return
        dialog = QMessageBox(self)
        self._choice_dialog = dialog
        dialog.setStyleSheet(LIGHT_STYLESHEET)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setWindowTitle("폴더 위치 변경")
        dialog.setText("연결한 폴더를 찾을 수 없습니다.\n아래 폴더를 사용할까요?")
        dialog.setInformativeText(f"이전: {candidate.link.path}\n찾은 폴더: {candidate.path}")
        use = dialog.addButton("이 폴더 사용", QMessageBox.ButtonRole.AcceptRole)
        other = dialog.addButton("다른 폴더 선택", QMessageBox.ButtonRole.ActionRole)
        cancel = dialog.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)

        def finished(_result: int) -> None:
            clicked = dialog.clickedButton()
            self._choice_dialog = None
            dialog.deleteLater()
            if self._shutting_down or self.selected != candidate.link:
                return
            if clicked is use:
                self._run(
                    lambda canceled: self.service.confirm_candidate(candidate, canceled),
                    opening=True,
                    updating_link=True,
                )
            elif clicked is other:
                self.relink()
            else:
                self.status.setText("폴더 연결은 유지했습니다.")

        dialog.finished.connect(finished)
        dialog.open()

    def request_shutdown(self) -> None:
        self._shutting_down = True
        self._candidate = None
        if self._choice_dialog:
            self._choice_dialog.reject()
        self._shutdown_scope.request_stop()
