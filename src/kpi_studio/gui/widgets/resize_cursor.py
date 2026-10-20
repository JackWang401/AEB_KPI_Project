"""The cursor and hover note on every place that can be dragged to resize (splitter handles, the side
panel's edge, the plot's side border): a bar with an arrow on each side and a "Drag to resize" tip.
Table column borders keep Qt's own cursor."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QSplitterHandle, QWidget

TIP = "Drag to resize"
SIZE = 32
_cursors: dict = {}
_filter = None


def _draw(horizontal: bool) -> QPixmap:
    """◂|▸ for a border that moves left / right (horizontal=True), the same turned upright otherwise."""
    pm = QPixmap(SIZE * 2, SIZE * 2)  # drawn at twice the size: sharp on Retina screens
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = SIZE / 2
    if not horizontal:  # draw the same shape turned a quarter
        p.translate(c, c)
        p.rotate(90)
        p.translate(-c, -c)
    path = QPainterPath()
    path.addRect(c - 1.0, c - 9, 2.0, 18)  # the bar
    for d in (-1, 1):  # the arrows, pointing away from the bar
        tip, base = c + d * 13, c + d * 5
        path.addPolygon(QPolygonF([QPointF(base, c - 5), QPointF(tip, c), QPointF(base, c + 5)]))
        path.closeSubpath()
    p.setPen(QPen(QColor("white"), 2.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))  # halo: seen on any background
    p.drawPath(path)
    p.fillPath(path, QColor("black"))
    p.end()
    return pm


def cursor(horizontal: bool = True) -> QCursor:
    """The resize cursor for a border dragged left / right (True) or up / down (False)."""
    if horizontal not in _cursors:
        _cursors[horizontal] = QCursor(_draw(horizontal), SIZE // 2, SIZE // 2)
    return _cursors[horizontal]


def apply(widget: QWidget, horizontal: bool = True) -> None:
    widget.setCursor(cursor(horizontal))
    widget.setToolTip(TIP)


class _Filter(QObject):
    def eventFilter(self, obj, e):
        if e.type() in (QEvent.Polish, QEvent.Show) and isinstance(obj, QSplitterHandle):
            # a horizontal splitter's handle is a vertical bar moved left / right
            apply(obj, obj.orientation() == Qt.Horizontal)
        return False


def install(app) -> None:
    """Every splitter handle in the application gets the resize cursor and tip (idempotent)."""
    global _filter
    if _filter is None:
        _filter = _Filter()
        app.installEventFilter(_filter)
