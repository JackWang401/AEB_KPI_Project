"""Auto-hide side panels: a strip of vertical tabs on the window's left edge.

Hovering a tab slides its panel out over the window; it hides again once the mouse has left both
the strip and the panel (not while a dialog or popup opened from the panel is up)."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QLabel,
    QVBoxLayout,
    QWidget,
)

STRIP_WIDTH = 30


class VerticalTab(QWidget):
    """A tab label written bottom to top."""

    hovered = Signal()

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.text = text
        self.active = False
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover)

    def sizeHint(self) -> QSize:
        return QSize(STRIP_WIDTH, self.fontMetrics().horizontalAdvance(self.text) + 28)

    minimumSizeHint = sizeHint

    def set_active(self, value: bool):
        self.active = value
        self.update()

    def enterEvent(self, e):
        self.hovered.emit()
        super().enterEvent(e)

    def mousePressEvent(self, e):
        self.hovered.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.active:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#e6eefb"))
            p.drawRoundedRect(self.rect().adjusted(3, 2, -3, -2), 6, 6)
        f = p.font()
        f.setWeight(f.Weight.DemiBold)
        p.setFont(f)
        p.setPen(QColor("#007aff" if self.active else "#3a3a3c"))
        p.translate(0, self.height())
        p.rotate(-90)
        p.drawText(QRect(0, 0, self.height(), self.width()), Qt.AlignCenter, self.text)


class SideBar(QWidget):
    """The strip of vertical tabs. Panels are added with add_panel and float over `host`."""

    HIDE_DELAY_MS = 350

    def __init__(self, host: QWidget, parent=None):
        super().__init__(parent)
        self.host = host
        self.setFixedWidth(STRIP_WIDTH)
        self.setObjectName("sideStrip")
        self.setAttribute(Qt.WA_StyledBackground)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 12, 0, 12)
        self._lay.setSpacing(6)
        self._lay.addStretch(1)
        self.tabs: list[VerticalTab] = []
        self.panels: list[QFrame] = []
        self.widths: list[int] = []
        self.current: int | None = None
        self._outside = 0
        self._timer = QTimer(self)
        self._timer.setInterval(self.HIDE_DELAY_MS // 2)
        self._timer.timeout.connect(self._check_mouse)
        host.installEventFilter(self)

    def add_panel(self, title: str, widget: QWidget, width: int) -> int:
        i = len(self.tabs)
        tab = VerticalTab(title)
        tab.hovered.connect(lambda i=i: self.open(i))
        self._lay.insertWidget(i, tab)
        frame = QFrame(self.host)
        frame.setObjectName("sidePanel")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(14, 10, 14, 14)
        lay.setSpacing(6)
        head = QLabel(title)
        head.setObjectName("sectionTitle")
        lay.addWidget(head)
        lay.addWidget(widget, 1)
        shadow = QGraphicsDropShadowEffect(frame)
        shadow.setBlurRadius(24)
        shadow.setOffset(4, 0)
        shadow.setColor(QColor(0, 0, 0, 60))
        frame.setGraphicsEffect(shadow)
        frame.hide()
        self.tabs.append(tab)
        self.panels.append(frame)
        self.widths.append(width)
        return i

    # ------------------------------------------------------------------ open / close
    def open(self, i: int):
        if self.current is not None and self.current != i:
            self.close_panel()
        self.current = i
        self.tabs[i].set_active(True)
        self._place()
        self.panels[i].show()
        self.panels[i].raise_()
        self._outside = 0
        self._timer.start()

    def close_panel(self):
        if self.current is None:
            return
        self.panels[self.current].hide()
        self.tabs[self.current].set_active(False)
        self.current = None
        self._timer.stop()

    def _place(self):
        if self.current is None:
            return
        width = min(self.widths[self.current], max(260, self.host.width() - 40))
        self.panels[self.current].setGeometry(0, 0, width, self.host.height())

    def _over(self) -> bool:
        pos = QCursor.pos()
        strip = QRect(self.mapToGlobal(QPoint(0, 0)), self.size())
        panel = self.panels[self.current]
        return strip.contains(pos) or QRect(panel.mapToGlobal(QPoint(0, 0)), panel.size()).contains(pos)

    def _busy(self) -> bool:
        """A dialog, menu or combo list opened from the panel is still up."""
        return QApplication.activeModalWidget() is not None or QApplication.activePopupWidget() is not None

    def _check_mouse(self):
        if self.current is None:
            return
        if self._over() or self._busy():
            self._outside = 0
            return
        self._outside += 1
        if self._outside >= 2:
            self.close_panel()

    def eventFilter(self, obj, e):
        if obj is self.host and e.type() == QEvent.Resize:
            self._place()
        return False
