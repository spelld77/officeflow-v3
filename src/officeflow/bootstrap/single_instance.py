from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtCore import QDir, QLockFile, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def instance_name(data_root: Path) -> str:
    normalized = str(data_root.resolve()).casefold().encode("utf-8")
    suffix = hashlib.sha256(normalized).hexdigest()[:16]
    return f"officeflow-v3-{suffix}"


class SingleInstanceCoordinator(QObject):
    messageReceived = Signal(str)

    def __init__(
        self,
        name: str,
        parent: QObject | None = None,
        *,
        lock_file: Path | None = None,
    ) -> None:
        super().__init__(parent)
        if not name.strip():
            raise ValueError("단일 실행 이름이 비어 있습니다.")
        self._name = name
        lock_path = lock_file or Path(QDir.tempPath()) / f"{name}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = QLockFile(str(lock_path))
        self._lock.setStaleLockTime(0)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._accept_connections)
        self._clients: set[QLocalSocket] = set()
        self._owns_server = False

    @property
    def is_primary(self) -> bool:
        return self._owns_server

    def acquire(self) -> bool:
        if self._owns_server:
            return True
        if not self._lock.tryLock(0):
            return False
        QLocalServer.removeServer(self._name)
        self._owns_server = self._server.listen(self._name)
        if not self._owns_server:
            self._lock.unlock()
        return self._owns_server

    def send_message(self, message: str, *, timeout_ms: int = 1_000) -> bool:
        payload = message.strip().encode("utf-8")
        if not payload:
            raise ValueError("단일 실행 메시지가 비어 있습니다.")
        socket = QLocalSocket()
        socket.connectToServer(self._name)
        if not socket.waitForConnected(timeout_ms):
            return False
        written = socket.write(payload)
        if written != len(payload):
            socket.abort()
            return False
        socket.flush()
        if socket.bytesToWrite() > 0:
            socket.waitForBytesWritten(timeout_ms)
        socket.disconnectFromServer()
        return True

    def close(self) -> None:
        for socket in tuple(self._clients):
            socket.abort()
        self._clients.clear()
        if self._owns_server:
            self._server.close()
            QLocalServer.removeServer(self._name)
            self._owns_server = False
            self._lock.unlock()

    def _accept_connections(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            self._clients.add(socket)
            socket.readyRead.connect(lambda client=socket: self._read_message(client))
            socket.disconnected.connect(lambda client=socket: self._release_client(client))

    def _read_message(self, socket: QLocalSocket) -> None:
        message = bytes(socket.readAll().data()).decode("utf-8", errors="replace").strip()
        if message:
            self.messageReceived.emit(message)
        socket.disconnectFromServer()

    def _release_client(self, socket: QLocalSocket) -> None:
        self._clients.discard(socket)
        socket.deleteLater()
