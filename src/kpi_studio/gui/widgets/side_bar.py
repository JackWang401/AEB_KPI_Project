"""Auto-hide side panels: a strip of vertical tabs on the window's left edge.

Hovering a tab slides its panel out over the window; it hides again once the mouse has left both
the strip and the panel (not while a dialog or popup opened from the panel is up). Clicking a tab
pins its panel open until the tab is clicked again."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

STRIP_WIDTH = 30


class VerticalTab(QWidget):
    """A tab label written bottom to top."""

    hovered = Signal()
    clicked = Signal()

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
        if e.button() == Qt.LeftButton:
            self.clicked.emit()

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


class ResizeGrip(QWidget):
    """The right edge of a side panel: drag it to change the panel's width."""

    def __init__(self, bar: "SideBar", index: int, parent: QWidget):
        super().__init__(parent)
        self.bar, self.index = bar, index
        self.setCursor(Qt.SizeHorCursor)
        self.setToolTip("Drag to resize")
        self._start = None

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._start = (e.globalPosition().x(), self.bar.widths[self.index])
            self.bar.dragging = True

    def mouseMoveEvent(self, e):
        if self._start is not None:
            x0, w0 = self._start
            self.bar.set_width(self.index, int(w0 + e.globalPosition().x() - x0))

    def mouseReleaseEvent(self, e):
        if self._start is not None:
            self._start = None
            self.bar.dragging = False
            self.bar.save_width(self.index)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#c7c7cc"))
        h = 36
        p.drawRoundedRect(QRect(self.width() // 2 - 1, (self.height() - h) // 2, 3, h), 1.5, 1.5)


class SideBar(QWidget):
    """The strip of vertical tabs. Panels are added with add_panel and float over `host`."""

    HIDE_DELAY_MS = 350
    MIN_WIDTH = 260
    GRIP = 8

    def __init__(self, host: QWidget, parent=None, settings=None):
        super().__init__(parent)
        self.host = host
        self.settings = settings
        self.dragging = False
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
        self.titles: list[str] = []
        self.grips: list[ResizeGrip] = []
        self.current: int | None = None
        self._outside = 0
        self._sticky = False
        self.pinned = False
        self._timer = QTimer(self)
        self._timer.setInterval(self.HIDE_DELAY_MS // 2)
        self._timer.timeout.connect(self._check_mouse)
        host.installEventFilter(self)

    def add_panel(self, title: str, widget: QWidget, width: int, buttons=()) -> int:
        i = len(self.tabs)
        tab = VerticalTab(title)
        tab.hovered.connect(lambda i=i: self.hover(i))
        tab.clicked.connect(lambda i=i: self.toggle_pin(i))
        self._lay.insertWidget(i, tab)
        frame = QFrame(self.host)
        frame.setObjectName("sidePanel")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(14, 10, 14 + self.GRIP, 14)
        lay.setSpacing(6)
        head = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        head.addWidget(label)
        head.addStretch(1)
        for b in buttons:  # + / − in the top right corner
            head.addWidget(b)
        lay.addLayout(head)
        lay.addWidget(widget, 1)
        shadow = QGraphicsDropShadowEffect(frame)
        shadow.setBlurRadius(24)
        shadow.setOffset(4, 0)
        shadow.setColor(QColor(0, 0, 0, 60))
        frame.setGraphicsEffect(shadow)
        frame.hide()
        self.tabs.append(tab)
        self.panels.append(frame)
        self.titles.append(title)
        self.grips.append(ResizeGrip(self, i, frame))
        if self.settings is not None:
            width = int(self.settings.value(self._key(i), width))
        self.widths.append(width)
        return i

    # ------------------------------------------------------------------ width
    def _key(self, i: int) -> str:
        return "side_panels/width_" + self.titles[i].replace(" ", "_").lower()

    def set_width(self, i: int, width: int):
        self.widths[i] = max(self.MIN_WIDTH, min(width, max(self.MIN_WIDTH, self.host.width() - 40)))
        if self.current == i:
            self._place()

    def save_width(self, i: int):
        if self.settings is not None:
            self.settings.setValue(self._key(i), self.widths[i])

    # ------------------------------------------------------------------ open / close
    def hover(self, i: int):
        """Hovering a tab shows its panel until the mouse leaves; a pinned panel stays put."""
        if not self.pinned:
            self.open(i)

    def toggle_pin(self, i: int):
        """A click pins the tab's panel open; clicking the pinned tab again closes it."""
        if self.pinned and self.current == i:
            self.close_panel()
        else:
            self.open(i)
            self.pinned = True

    def open(self, i: int, sticky: bool = False):
        """Show panel `i`. `sticky`: opened from elsewhere (not by hovering its tab), so it stays open
        until the mouse has been over it once and left again."""
        if self.current is not None and self.current != i:
            self.close_panel()
        self._sticky = sticky
        self.pinned = False
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
        self.pinned = False
        self._timer.stop()

    def _place(self):
        if self.current is None:
            return
        width = min(self.widths[self.current], max(self.MIN_WIDTH, self.host.width() - 40))
        h = self.host.height()
        self.panels[self.current].setGeometry(0, 0, width, h)
        grip = self.grips[self.current]
        grip.setGeometry(width - self.GRIP, 0, self.GRIP, h)
        grip.raise_()

    def _over(self) -> bool:
        pos = QCursor.pos()
        strip = QRect(self.mapToGlobal(QPoint(0, 0)), self.size())
        panel = self.panels[self.current]
        return strip.contains(pos) or QRect(panel.mapToGlobal(QPoint(0, 0)), panel.size()).contains(pos)

    def _busy(self) -> bool:
        """A dialog, menu or combo list opened from the panel is still up."""
        return QApplication.activeModalWidget() is not None or QApplication.activePopupWidget() is not None

    def _check_mouse(self):
        if self.current is None or self.pinned:
            return
        if self.dragging or self._over() or self._busy():
            self._outside = 0
            self._sticky = False
            return
        if self._sticky:
            return
        self._outside += 1
        if self._outside >= 2:
            self.close_panel()

    def eventFilter(self, obj, e):
        if obj is self.host and e.type() == QEvent.Resize:
            self._place()
        return False
