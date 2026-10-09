"""One-line file rows with stable role/status columns and full tooltips."""

from dataclasses import dataclass

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

FILE_ROW_ROLE = int(Qt.ItemDataRole.UserRole) + 20


class FileCheckDelegate(QStyledItemDelegate):
    """Visible list-item checks independent of the system's widget checkbox theme."""

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return QSize(0, max(34, option.fontMetrics.height() + 16))

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        super().paint(painter, styled, index)
        widget = styled.widget
        if (
            widget is None
            or not styled.features & QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        ):
            return
        rect = widget.style().subElementRect(
            QStyle.SubElement.SE_ItemViewItemCheckIndicator, styled, widget
        )
        checked = styled.checkState == Qt.CheckState.Checked
        enabled = bool(styled.state & QStyle.StateFlag.State_Enabled)
        border = QColor("#2F6FED" if checked else "#5273A1") if enabled else QColor("#96A6BB")
        fill = QColor("#2F6FED" if enabled else "#63748B") if checked else QColor("#FFFFFF")
        painter.save()
        bounds = option.rect
        if isinstance(widget, QListWidget):
            bounds = bounds.intersected(widget.viewport().rect())
        painter.setClipRect(bounds, Qt.ClipOperation.IntersectClip)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(border, 2))
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(rect).adjusted(1, 1, -1, -1), 3, 3)
        if checked:
            tick = QPainterPath()
            tick.moveTo(rect.left() + rect.width() * 0.23, rect.top() + rect.height() * 0.51)
            tick.lineTo(rect.left() + rect.width() * 0.43, rect.top() + rect.height() * 0.72)
            tick.lineTo(rect.left() + rect.width() * 0.79, rect.top() + rect.height() * 0.28)
            painter.setPen(
                QPen(
                    QColor("#FFFFFF"),
                    2.2,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.drawPath(tick)
        painter.restore()


def checkable_file_list(listing: QListWidget) -> None:
    """Keep the standard click/Space check behavior while giving it a clear target."""
    listing.setItemDelegate(FileCheckDelegate(listing))
    listing.setUniformItemSizes(True)
    listing.setWordWrap(False)
    listing.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    listing.setStyleSheet("""
        QListWidget, QListWidget QWidget { color:#172033; background:#FFFFFF; }
        QListWidget::item:selected { color:#172033; background:#E8F0FF; }
        QListWidget::indicator { width:20px; height:20px; border:none; background:transparent; }
    """)


@dataclass(frozen=True, slots=True)
class FileRow:
    name: str
    role: str
    status: str = ""


class CompactFileDelegate(QStyledItemDelegate):
    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return QSize(0, max(34, option.fontMetrics.height() + 16))

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        row = index.data(FILE_ROW_ROLE)
        if not isinstance(row, FileRow):
            super().paint(painter, option, index)
            return
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        styled.text = ""
        widget = styled.widget
        if widget is None:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setClipRect(option.rect, Qt.ClipOperation.IntersectClip)
        style = widget.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, styled, painter, widget)
        # Stylesheets may reset the clip while drawing backgrounds. Keep custom text
        # inside both its row and the viewport, also when QWidget.grab redirects paint.
        bounds = option.rect
        if isinstance(widget, QListWidget):
            bounds = bounds.intersected(widget.viewport().rect())
        painter.setClipRect(bounds, Qt.ClipOperation.IntersectClip)
        area = option.rect.adjusted(12, 0, -12, 0)
        if styled.features & QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator:
            check = style.subElementRect(
                QStyle.SubElement.SE_ItemViewItemCheckIndicator, styled, widget
            )
            area.setLeft(max(area.left(), check.right() + 8))
        metrics = option.fontMetrics
        role_width = min(metrics.horizontalAdvance(row.role) + 8, int(area.width() * 0.2))
        status_width = (
            min(metrics.horizontalAdvance(row.status) + 8, int(area.width() * 0.32))
            if row.status
            else 0
        )
        group = (
            QPalette.ColorGroup.Active
            if styled.state & QStyle.StateFlag.State_Enabled
            else QPalette.ColorGroup.Disabled
        )
        color = (
            QPalette.ColorRole.HighlightedText
            if styled.state & QStyle.StateFlag.State_Selected
            else QPalette.ColorRole.Text
        )
        painter.setFont(option.font)
        painter.setPen(styled.palette.color(group, color))
        alignment = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        painter.drawText(
            QRect(area.left(), area.top(), role_width, area.height()),
            alignment,
            metrics.elidedText(row.role, Qt.TextElideMode.ElideRight, role_width),
        )
        name_area = QRect(
            area.left() + role_width + 6,
            area.top(),
            max(0, area.width() - role_width - status_width - 18),
            area.height(),
        )
        painter.drawText(
            name_area,
            alignment,
            metrics.elidedText(row.name, Qt.TextElideMode.ElideMiddle, name_area.width()),
        )
        if row.status:
            status_area = QRect(
                area.right() - status_width + 1, area.top(), status_width, area.height()
            )
            painter.drawText(
                status_area,
                alignment,
                metrics.elidedText(row.status, Qt.TextElideMode.ElideRight, status_width),
            )
        painter.restore()


def compact_file_list(listing: QListWidget) -> None:
    listing.setItemDelegate(CompactFileDelegate(listing))
    listing.setWordWrap(False)
    listing.setUniformItemSizes(True)
    listing.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    listing.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)


def set_file_row(
    item: QListWidgetItem, name: str, role: str, status: str = "", *, tooltip: str = ""
) -> None:
    row = FileRow(" ".join(name.split()), role, status)
    if item.data(FILE_ROW_ROLE) != row:
        item.setData(FILE_ROW_ROLE, row)
        item.setText(f"[{role}] {row.name}" + (f" · {status}" if status else ""))
    item.setToolTip(tooltip or item.text())
