"""Resizable columns and full-text hover tips for every list, table and tree in KPI Studio.

`install(app)` once: from then on every horizontal header that is shown gets columns the user can
drag (they keep fitting their content and the view's width until the user resizes one), and a cell
or header whose text is cut off shows its full text on hover."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QToolTip

TIP_FONT_PX = 13  # the size of the text in the tables

MIN_STRETCH = 120
MAX_FIT = 260
PADDING = 12


class ColumnSizer(QObject):
    """Makes every column of `header` user-resizable, keeping the auto layout it was set up with
    (fit to contents / share the free width) until the user drags a column edge."""

    def __init__(self, header: QHeaderView):
        super().__init__(header)
        self.header = header
        self.view = header.parentWidget()
        n = header.count()
        self.modes = [header.sectionResizeMode(c) for c in range(n)]
        self.user_sized = False
        self._pressed = False
        self._fitting = False
        for c in range(n):
            header.setSectionResizeMode(c, QHeaderView.Interactive)
        header.setMinimumSectionSize(36)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.fit)
        header.sectionResized.connect(self._resized)
        header.viewport().installEventFilter(self)
        header.installEventFilter(self)
        model = header.model()
        if model is not None:
            for sig in (model.modelReset, model.rowsInserted, model.rowsRemoved, model.dataChanged,
                        model.layoutChanged):
                sig.connect(self.schedule)
        self.fit()
        for c in range(n):  # widths set before (e.g. setColumnWidth) respect the header text too
            if header.sectionSize(c) < self.header_width(c) and not header.isSectionHidden(c):
                header.resizeSection(c, self.header_width(c))

    def schedule(self, *_):
        if not self.user_sized:
            self._timer.start(0)

    def header_width(self, c: int) -> int:
        """The width the column's header text needs: no column gets narrower than that."""
        return max(self.header.sectionSizeHint(c), self.header.minimumSectionSize())

    def _resized(self, c: int, _old: int = 0, new: int = 0):
        if self._fitting:
            return
        if self._pressed:
            self.user_sized = True  # from now on the user's widths stay
        if 0 <= c < self.header.count() and new < self.header_width(c) and not self.header.isSectionHidden(c):
            self._fitting = True
            try:
                self.header.resizeSection(c, self.header_width(c))
            finally:
                self._fitting = False

    def eventFilter(self, obj, e):
        t = e.type()
        if t == QEvent.MouseButtonPress:
            self._pressed = True
        elif t == QEvent.MouseButtonRelease:
            self._pressed = False
        elif t == QEvent.Resize and obj is self.header:
            self.schedule()
        return False

    def fit(self):
        """Columns that fitted their contents fit them again; stretching ones share what is left."""
        if self.user_sized or self.view is None:
            return
        h = self.header
        n = min(h.count(), len(self.modes))
        if n == 0:
            return
        self._fitting = True
        try:
            stretch = [c for c in range(n) if self.modes[c] == QHeaderView.Stretch and not h.isSectionHidden(c)]
            for c in range(n):
                if h.isSectionHidden(c) or c in stretch or self.modes[c] == QHeaderView.Fixed:
                    continue
                want = max(self.view.sizeHintForColumn(c), h.sectionSizeHint(c))
                if self.modes[c] != QHeaderView.ResizeToContents:
                    want = min(want, MAX_FIT)  # long text: the full value shows on hover
                h.resizeSection(c, max(want, self.header_width(c)))
            if stretch:
                last = n - 1 if h.stretchLastSection() else -1
                used = sum(h.sectionSize(c) for c in range(n)
                           if c not in stretch and c != last and not h.isSectionHidden(c))
                free = h.viewport().width() - used
                if last >= 0 and last not in stretch:
                    free -= h.sectionSizeHint(last)
                each = max(MIN_STRETCH, free // len(stretch))
                for c in stretch:
                    h.resizeSection(c, max(each, self.header_width(c)))
        finally:
            self._fitting = False


def _widest_line(fm, text: str) -> int:
    return max((fm.horizontalAdvance(line) for line in text.splitlines()), default=0)


def _cell_tip(view: QAbstractItemView, pos) -> str | None:
    index = view.indexAt(pos)
    if not index.isValid() or view.indexWidget(index) is not None:
        return None
    text = index.data(Qt.DisplayRole)
    if text is None or not str(text).strip():
        return None
    text = str(text)
    room = view.visualRect(index).width() - PADDING
    if index.data(Qt.CheckStateRole) is not None:
        room -= 22
    if index.data(Qt.DecorationRole) is not None:
        room -= view.iconSize().width() + 4 if view.iconSize().isValid() else 20
    if _widest_line(view.fontMetrics(), text) <= room:
        return None
    own = index.data(Qt.ToolTipRole)
    return text if not own or str(own) == text else f"{text}\n\n{own}"


def _header_tip(header: QHeaderView, pos) -> str | None:
    section = header.logicalIndexAt(pos)
    model = header.model()
    if section < 0 or model is None:
        return None
    text = model.headerData(section, header.orientation(), Qt.DisplayRole)
    if text is None:
        return None
    text = str(text)
    if _widest_line(header.fontMetrics(), text) <= header.sectionSize(section) - PADDING - 8:
        return None
    own = model.headerData(section, header.orientation(), Qt.ToolTipRole)
    return text if not own or str(own) == text else f"{text}\n\n{own}"


def tip_at(view: QAbstractItemView, pos) -> str | None:
    """The full text to show on hover at viewport `pos`, when the text there is cut off."""
    if isinstance(view, QHeaderView):
        return _header_tip(view, pos)
    return _cell_tip(view, pos)


class _AppFilter(QObject):
    def eventFilter(self, obj, e):
        t = e.type()
        if t == QEvent.ToolTip:
            view = obj.parent() if obj is not None else None
            if isinstance(view, QAbstractItemView) and obj is view.viewport():
                tip = tip_at(view, e.pos())
                if tip:
                    QToolTip.showText(e.globalPos(), tip, obj)
                    return True
        elif t == QEvent.Show and isinstance(obj, QHeaderView):
            make_resizable(obj)
        return False


def make_resizable(header: QHeaderView) -> None:
    """Let the user drag every column of `header` (once; vertical headers are left alone)."""
    if header.orientation() != Qt.Horizontal or header.property("columnSizer"):
        return
    header.setProperty("columnSizer", True)
    header._column_sizer = ColumnSizer(header)


_filter: _AppFilter | None = None


def install(app) -> None:
    """Resizable columns and hover tips for every view in the application (idempotent)."""
    global _filter
    font = app.font()
    font.setPixelSize(TIP_FONT_PX)
    QToolTip.setFont(font)  # hover text as large as the text it shows
    if _filter is None:
        _filter = _AppFilter()
        app.installEventFilter(_filter)
