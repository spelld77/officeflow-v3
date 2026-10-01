from __future__ import annotations

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget


class NotificationCoordinator:
    """Position only: never activate a window or alter task-reminder processing."""

    @staticmethod
    def place(window: QWidget, other: QWidget | None, *, initial: bool = False) -> None:
        screen = window.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        geometry = window.frameGeometry()
        size = geometry.size()
        point = (
            area.center() - QPoint(size.width() // 2, size.height() // 2)
            if initial
            else geometry.topLeft()
        )
        if (
            other is not None
            and other.isVisible()
            and QRect(point, size).intersects(other.frameGeometry())
        ):
            obstacle = other.frameGeometry()
            choices = (
                QPoint(obstacle.right() + 12, obstacle.top()),
                QPoint(obstacle.left() - size.width() - 12, obstacle.top()),
                QPoint(obstacle.left(), obstacle.bottom() + 12),
                QPoint(area.left(), area.top()),
                QPoint(area.right() - size.width() + 1, area.bottom() - size.height() + 1),
            )
            point = min(
                choices,
                key=lambda p: (
                    not area.contains(QRect(p, size)),
                    QRect(p, size).intersected(obstacle).width()
                    * QRect(p, size).intersected(obstacle).height(),
                ),
            )
        point.setX(max(area.left(), min(point.x(), area.right() - size.width() + 1)))
        point.setY(max(area.top(), min(point.y(), area.bottom() - size.height() + 1)))
        window.move(point)
