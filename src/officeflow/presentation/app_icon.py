from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


def create_app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2F6FED"))
        margin = size * 0.06
        painter.drawRoundedRect(QRectF(margin, margin, size - margin * 2, size - margin * 2), size * 0.2, size * 0.2)
        path = QPainterPath(QPointF(size * 0.24, size * 0.52))
        path.lineTo(QPointF(size * 0.43, size * 0.70))
        path.lineTo(QPointF(size * 0.76, size * 0.32))
        painter.setPen(
            QPen(
                QColor("#FFFFFF"),
                max(2.0, size * 0.11),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.drawPath(path)
        painter.end()
        icon.addPixmap(pixmap)
    return icon
