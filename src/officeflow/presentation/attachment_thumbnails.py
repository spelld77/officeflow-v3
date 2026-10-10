"""Visible-only attachment cards and an explicitly owned, bounded image reader."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import cast

from PySide6.QtCore import (
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRect,
    QSize,
    Qt,
    QThread,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from officeflow.application.attachments import AttachmentService
from officeflow.domain.attachment import Attachment
from officeflow.presentation.background import finish_thread
from officeflow.presentation.file_list import FILE_ROW_ROLE, FileRow
from officeflow.presentation.image_attachments import (
    ImageReadResult,
    is_image_name,
    read_bounded_image,
)


def visible_items(listing: QListWidget) -> tuple[QListWidgetItem, ...]:
    """O(log n + visible) for the uniform row/grid layouts, including clipped cards."""
    viewport = listing.viewport().rect()
    low, high = 0, listing.count()
    while low < high:
        middle = (low + high) // 2
        if listing.visualItemRect(listing.item(middle)).bottom() < viewport.top():
            low = middle + 1
        else:
            high = middle
    items = []
    for index in range(low, listing.count()):
        item = listing.item(index)
        rect = listing.visualItemRect(item)
        if rect.top() > viewport.bottom():
            break
        if rect.intersects(viewport):
            items.append(item)
    return tuple(items)


@dataclass(frozen=True)
class _Request:
    client: int
    service: AttachmentService
    attachment: Attachment
    width: int
    height: int
    epoch: int
    expanded: bool
    force: bool

    @property
    def source(self) -> tuple[object, ...]:
        value = self.attachment
        return (
            id(self.service),
            value.asset_id,
            value.relative_path,
            value.is_frozen,
            value.fixed_size_bytes,
            value.fixed_checksum,
            self.width,
            self.height,
        )


@dataclass
class ImageState:
    request: _Request
    key: tuple[object, ...] | None = None
    error: str = ""
    error_kind: str = ""
    finished: bool = False

    @property
    def label(self) -> str:
        if not self.error:
            return "읽는 중"
        if self.error_kind:
            return self.error_kind
        if "찾을 수" in self.error or "파일 없음" in self.error or "No such file" in self.error:
            return "파일 없음"
        if self.request.attachment.is_frozen:
            return "원본 확인 필요"
        if "크" in self.error or "64 MB" in self.error:
            return "크기 제한"
        if "denied" in self.error.casefold() or "권한" in self.error or "사용 중" in self.error:
            return "읽기 불가"
        return "이미지 확인 불가"


@dataclass
class _Job:
    request: _Request
    cached: ImageReadResult | None
    canceled: Event = field(default_factory=Event)


class _ImageWorker(QObject):
    completed = Signal(object)

    @Slot(object)
    def read(self, value: object) -> None:
        job = cast(_Job, value)
        try:
            request = job.request
            result: ImageReadResult | Exception = read_bounded_image(
                request.service,
                request.attachment.id_required,
                job.canceled.is_set,
                target=QSize(request.width, request.height),
                expected=request.attachment,
                cached=job.cached,
                background=not request.expanded,
            )
        except Exception as error:
            result = error
        self.completed.emit((job, result))


class AttachmentImageCoordinator(QObject):
    """One reader for all stored-image views; QImages live only in a bounded LRU."""

    updated = Signal(int)
    viewModeChanged = Signal(str)
    requested = Signal(object)
    MAX_CACHE_BYTES = 32 * 1024 * 1024
    MAX_CACHE_ENTRIES = 64
    MAX_QUEUE = 32
    MAX_CLIENT_REQUESTS = 24
    MAX_VISIBLE_STATES = 256

    def __init__(self, parent: QObject | None = None, *, default_view: str = "list") -> None:
        super().__init__(parent)
        self.default_view = default_view if default_view in ("list", "thumbnails") else "list"
        self._clients: dict[int, dict[int, ImageState]] = {}
        self._sequence = 0
        self._cache: OrderedDict[tuple[object, ...], ImageReadResult] = OrderedDict()
        self.cache_bytes = 0
        self.read_count = 0
        self.decode_count = 0
        self._active: _Job | None = None
        self._queue: list[_Request] = []
        self._deferred: list[tuple[int, frozenset[str], Callable[[], None]]] = []
        self._thread: QThread | None = None
        self._worker: _ImageWorker | None = None
        self._closing = False
        self._last_client = 0

    @property
    def busy(self) -> bool:
        return self._active is not None or bool(self._queue)

    @property
    def cache_entries(self) -> int:
        return len(self._cache)

    @property
    def pending_count(self) -> int:
        return len(self._queue)

    def register(self) -> int:
        self._sequence += 1
        self._clients[self._sequence] = {}
        return self._sequence

    def remember_mode(self, mode: str) -> None:
        if mode in ("list", "thumbnails") and mode != self.default_view:
            self.default_view = mode
            self.viewModeChanged.emit(mode)

    def sync(
        self,
        client: int,
        values: Sequence[tuple[AttachmentService, Attachment, QSize]],
        *,
        epoch: int = 0,
        expanded: bool = False,
        force: bool = False,
    ) -> None:
        if self._closing or client not in self._clients:
            return
        previous = self._clients[client]
        current: dict[int, ImageState] = {}
        for service, attachment, target in values[: self.MAX_VISIBLE_STATES]:
            width = min(1200 if expanded else 640, max(1, target.width()))
            height = min(900 if expanded else 400, max(1, target.height()))
            request = _Request(client, service, attachment, width, height, epoch, expanded, force)
            old = previous.get(attachment.id_required)
            current[attachment.id_required] = (
                old if old and old.request == request else ImageState(request)
            )
        self._clients[client] = current
        self._queue = [r for r in self._queue if self._is_current(r)]
        if self._active and not self._is_current(self._active.request):
            self._active.canceled.set()
        self._pump()

    def _is_current(self, request: _Request) -> bool:
        state = self._clients.get(request.client, {}).get(request.attachment.id_required)
        return state is not None and state.request == request

    def state_for(self, client: int, identity: int) -> ImageState | None:
        return self._clients.get(client, {}).get(identity)

    def image_for(self, client: int, identity: int) -> QImage | None:
        state = self.state_for(client, identity)
        if state and state.key is not None and state.key in self._cache:
            self._cache.move_to_end(state.key)
            return self._cache[state.key].image
        return None

    def _pump(self) -> None:
        if self._closing:
            return
        # Round-robin windows, explicit enlargement first; at most 32 queued requests.
        clients = list(self._clients)
        if self._last_client in clients:
            pivot = clients.index(self._last_client) + 1
            clients = clients[pivot:] + clients[:pivot]
        queued = {(r.client, r.attachment.id) for r in self._queue}
        if self._active:
            queued.add((self._active.request.client, self._active.request.attachment.id))
        blocked = (
            set().union(*(assets for _c, assets, _f in self._deferred)) if self._deferred else set()
        )
        for client in clients:
            waiting = sum(1 for owner, _identity in queued if owner == client)
            for identity, state in self._clients[client].items():
                if len(self._queue) >= self.MAX_QUEUE or waiting >= self.MAX_CLIENT_REQUESTS:
                    break
                if (
                    state.finished
                    or (client, identity) in queued
                    or state.request.attachment.asset_id in blocked
                ):
                    continue
                self._queue.append(state.request)
                queued.add((client, identity))
                waiting += 1
        self._queue.sort(key=lambda r: not r.expanded)
        if self._active or not self._queue:
            return
        priority = self._queue[0].expanded
        position = next(
            (
                i
                for i, r in enumerate(self._queue)
                if r.expanded == priority and r.client != self._last_client
            ),
            0,
        )
        request = self._queue.pop(position)
        self._last_client = request.client
        cached = (
            None
            if request.force
            else next(
                (v for k, v in reversed(self._cache.items()) if k[:-1] == request.source), None
            )
        )
        job = _Job(request, cached)
        self._active = job
        if self._thread is None:
            self._thread = QThread(self)
            self._worker = _ImageWorker()
            self._worker.moveToThread(self._thread)
            self.requested.connect(self._worker.read, Qt.ConnectionType.QueuedConnection)
            self._worker.completed.connect(self._completed, Qt.ConnectionType.QueuedConnection)
            self._thread.finished.connect(self._worker.deleteLater)
            self._thread.start()
        self.read_count += 1
        self.requested.emit(job)

    @Slot(object)
    def _completed(self, value: object) -> None:
        job, result = cast(tuple[_Job, ImageReadResult | Exception], value)
        if job is not self._active:
            return
        self._active = None
        request = job.request
        if not self._closing and self._is_current(request) and not job.canceled.is_set():
            state = self._clients[request.client][request.attachment.id_required]
            state.finished = True
            if isinstance(result, ImageReadResult):
                self.decode_count += int(result.decoded)
                key = (*request.source, result.fingerprint)
                old = self._cache.pop(key, None)
                if old:
                    self.cache_bytes -= old.image.sizeInBytes()
                self._cache[key] = result
                self.cache_bytes += result.image.sizeInBytes()
                while (
                    self.cache_bytes > self.MAX_CACHE_BYTES
                    or len(self._cache) > self.MAX_CACHE_ENTRIES
                ):
                    _, evicted = self._cache.popitem(last=False)
                    self.cache_bytes -= evicted.image.sizeInBytes()
                state.key = key
            else:
                state.error = str(result)
                if isinstance(result, FileNotFoundError):
                    state.error_kind = "파일 없음"
                elif isinstance(result, PermissionError):
                    state.error_kind = "읽기 불가"
            notifications = {request.client}
            # Concurrent requests for the same attachment, policy and observation
            # epoch share one read. A later refocus epoch must verify again.
            for client, states in self._clients.items():
                for other in states.values():
                    if (
                        other is not state
                        and not other.finished
                        and other.request.attachment.id == request.attachment.id
                        and other.request.source == request.source
                        and other.request.epoch == request.epoch
                        and other.request.force == request.force
                    ):
                        other.key, other.error, other.finished = state.key, state.error, True
                        other.error_kind = state.error_kind
                        notifications.add(client)
            self._queue = [
                r
                for r in self._queue
                if self._is_current(r)
                and not self._clients[r.client][r.attachment.id_required].finished
            ]
            for client in notifications:
                if client in self._clients:
                    self.updated.emit(client)
        pending, self._deferred = self._deferred, []
        for client, _assets, callback in pending:
            if client in self._clients and not self._closing:
                callback()
        if not self._clients:
            self._cache.clear()
            self.cache_bytes = 0
        self._pump()

    def after_read(self, client: int, assets: frozenset[str], callback: Callable[[], None]) -> bool:
        """Defer a file command only while its protected read is actually active."""
        self._queue = [r for r in self._queue if r.attachment.asset_id not in assets]
        for states in self._clients.values():
            for state in states.values():
                if state.request.attachment.asset_id in assets:
                    state.finished = True
        if self._active and self._active.request.attachment.asset_id in assets:
            self._active.canceled.set()
            self._deferred.append((client, assets, callback))
            return True
        return False

    def unregister(self, client: int) -> None:
        self._clients.pop(client, None)
        self._queue = [r for r in self._queue if r.client != client]
        self._deferred = [entry for entry in self._deferred if entry[0] != client]
        if self._active and self._active.request.client == client:
            self._active.canceled.set()
        if not self._clients:
            self._cache.clear()
            self.cache_bytes = 0

    def invalidate(self, client: int) -> None:
        states = self._clients.get(client, {})
        sources = {state.request.source for state in states.values()}
        for key in tuple(self._cache):
            if key[:-1] in sources:
                self.cache_bytes -= self._cache.pop(key).image.sizeInBytes()
        for state in states.values():
            state.key = None
        self.sync(client, ())

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._clients.clear()
        self._queue.clear()
        self._deferred.clear()
        self._cache.clear()
        self.cache_bytes = 0
        if self._active:
            self._active.canceled.set()
        if self._thread:
            self._thread.quit()
            finish_thread(self._thread)
            self._thread.deleteLater()
        # Retain the worker wrapper until native thread-local cleanup has completed.
        self._thread, self._worker, self._active = None, None, None


class ThumbnailFileDelegate(QStyledItemDelegate):
    def __init__(
        self, listing: QListWidget, coordinator: AttachmentImageCoordinator, client: int
    ) -> None:
        super().__init__(listing)
        self.listing, self.coordinator, self.client = listing, coordinator, client

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return self.listing.gridSize()

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        row = index.data(FILE_ROW_ROLE)
        if not isinstance(row, FileRow):
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        bounds = option.rect.adjusted(3, 3, -3, -3)
        painter.save()
        painter.setClipRect(option.rect.intersected(self.listing.viewport().rect()))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        painter.setPen(
            QPen(QColor("#2F6FED" if selected or focused else "#DCE5F2"), 2 if focused else 1)
        )
        painter.setBrush(QColor("#E8F0FF" if selected else "#FFFFFF"))
        painter.drawRoundedRect(bounds, 6, 6)
        area = bounds.adjusted(10, 8, -10, -8)
        picture_area = QRect(area.left(), area.top(), area.width(), 104)
        painter.fillRect(picture_area, QColor("#F3F6FA"))
        identity = index.data(Qt.ItemDataRole.UserRole)
        picture = self.coordinator.image_for(self.client, identity)
        state = self.coordinator.state_for(self.client, identity)
        painter.setPen(QColor("#172033"))
        painter.setFont(option.font)
        if picture is not None:
            fitted = picture.size().scaled(picture_area.size(), Qt.AspectRatioMode.KeepAspectRatio)
            # Never enlarge small images beyond their physical-to-logical size.
            ratio = max(1.0, self.listing.devicePixelRatioF())
            fitted = QSize(
                min(fitted.width(), int(picture.width() / ratio)),
                min(fitted.height(), int(picture.height() / ratio)),
            )
            target = QRect(0, 0, fitted.width(), fitted.height())
            target.moveCenter(picture_area.center())
            painter.drawImage(target, picture)
        else:
            label = (
                "읽는 중"
                if is_image_name(row.name)
                else Path(row.name).suffix.lstrip(".").upper() or "파일"
            )
            if state and state.error:
                label = state.label
            elif state and state.finished:
                label = "이미지"
            if not is_image_name(row.name):
                page = QRect(0, 0, 68, 76)
                page.moveCenter(picture_area.center())
                painter.setBrush(QColor("#FFFFFF"))
                painter.setPen(QPen(QColor("#8AA0BC"), 1))
                painter.drawRoundedRect(page, 4, 4)
                painter.setPen(QColor("#355176"))
                painter.drawText(page, Qt.AlignmentFlag.AlignCenter, label[:8])
            else:
                painter.drawText(picture_area, Qt.AlignmentFlag.AlignCenter, label)
        metrics = option.fontMetrics
        line = metrics.height() + 4
        name_rect = QRect(area.left(), picture_area.bottom() + 7, area.width(), line)
        painter.drawText(
            name_rect,
            Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(row.name, Qt.TextElideMode.ElideMiddle, area.width()),
        )
        painter.setPen(QColor("#355176"))
        status = row.role + (" · " + row.status if row.status else "")
        painter.drawText(
            name_rect.translated(0, line),
            Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(status, Qt.TextElideMode.ElideRight, area.width()),
        )
        painter.restore()
