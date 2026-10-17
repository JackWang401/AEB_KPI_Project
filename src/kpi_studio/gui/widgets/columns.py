"""Resizable columns and full-text hover tips for every list, table and tree in KPI Studio.

`install(app)` once: from then on every horizontal header that is shown gets columns the user can
drag (they keep fitting their content and the view's width until the user resizes one), and a cell
or header whose text is cut off shows its full text on hover."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer
from PySide6.QtWidgets import QAbstractItemView, QApplication, QHeaderView, QMenu, QToolTip

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
        else:
            QTimer.singleShot(0, self.fill)

    def fill(self, dragged: int = -1):
        """After the user sized columns, keep them spanning the whole width: the free (or missing)
        width goes to a stretching column (else the last one), never the column being dragged."""
        h = self.header
        if h.stretchLastSection() or self.view is None:
            return  # Qt already fills with the last column
        shown = [c for c in range(h.count()) if not h.isSectionHidden(c)]
        if len(shown) < 2:
            return
        stretch = [c for c in shown if c < len(self.modes) and self.modes[c] == QHeaderView.Stretch]
        # who gives or takes the width: a stretching column first, then the others right to left, and
        # the dragged column last (it can't push the table wider than the view)
        order = [c for c in dict.fromkeys(stretch + shown[::-1]) if c != dragged]
        if dragged in shown:
            order.append(dragged)
        free = h.viewport().width() - sum(h.sectionSize(c) for c in shown)
        if free == 0:
            return
        self._fitting = True
        try:
            if free > 0:
                h.resizeSection(order[0], h.sectionSize(order[0]) + free)
            else:
                for c in order:
                    give = min(-free, h.sectionSize(c) - self.header_width(c))
                    if give > 0:
                        h.resizeSection(c, h.sectionSize(c) - give)
                        free += give
                    if free >= 0:
                        break
        finally:
            self._fitting = False

    def header_width(self, c: int) -> int:
        """The width the column's header text needs: no column gets narrower than that."""
        return max(self.header.sectionSizeHint(c), self.header.minimumSectionSize())

    def _resized(self, c: int, _old: int = 0, new: int = 0):
        if self._fitting:
            return
        if self._pressed:
            self.user_sized = True  # from now on the user's widths stay
        if new == 0:  # the column was just hidden (Qt reports that as a resize to 0): leave it hidden
            return
        if 0 <= c < self.header.count() and new < self.header_width(c) and not self.header.isSectionHidden(c):
            self._fitting = True
            try:
                self.header.resizeSection(c, self.header_width(c))
            finally:
                self._fitting = False
        if self.user_sized:
            self.fill(dragged=c)

    def eventFilter(self, obj, e):
        t = e.type()
        if t == QEvent.MouseButtonPress:
            self._pressed = True
        elif t == QEvent.MouseButtonRelease:
            self._pressed = False
        elif t == QEvent.Resize and obj is self.header:
            self.schedule()  # (once the user sized columns: refill the width)
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


def cell_rect(view: QAbstractItemView, pos) -> QRect:
    """The cell (or header section) at viewport `pos`, in viewport coordinates."""
    if isinstance(view, QHeaderView):
        s = view.logicalIndexAt(pos)
        if view.orientation() == Qt.Horizontal:
            return QRect(view.sectionViewportPosition(s), 0, view.sectionSize(s), view.height())
        return QRect(0, view.sectionViewportPosition(s), view.width(), view.sectionSize(s))
    return view.visualRect(view.indexAt(pos))


def _tip_label():
    return next((w for w in QApplication.topLevelWidgets()
                 if w.metaObject().className() == "QTipLabel" and w.isVisible()), None)


def show_tip(viewport, rect: QRect, text: str) -> None:
    """Show `text` right under the cut-off cell `rect`, starting where its text starts (not at the
    mouse pointer); it hides once the mouse leaves the cell."""
    below = viewport.mapToGlobal(rect.bottomLeft()) + QPoint(0, 2)
    QToolTip.showText(below, text, viewport, rect)
    label = _tip_label()
    if label is not None:
        label.move(below)


class _AppFilter(QObject):
    def eventFilter(self, obj, e):
        t = e.type()
        if t == QEvent.ToolTip:
            view = obj.parent() if obj is not None else None
            if isinstance(view, QAbstractItemView) and obj is view.viewport():
                tip = tip_at(view, e.pos())
                if not tip and not isinstance(view, QHeaderView):  # a cell's own tip goes there too
                    index = view.indexAt(e.pos())
                    own = index.data(Qt.ToolTipRole) if index.isValid() else None
                    tip = str(own) if own else None
                if tip:
                    show_tip(obj, cell_rect(view, e.pos()), tip)
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


# ---------------------------------------------------------------------------- column picker
class _StayOpenMenu(QMenu):
    """A menu whose checkable entries toggle without closing it."""

    def mouseReleaseEvent(self, e):
        action = self.actionAt(e.position().toPoint())
        if action is not None and action.isCheckable() and action.isEnabled():
            action.trigger()
            return
        super().mouseReleaseEvent(e)


def column_menu(view: QAbstractItemView) -> QMenu:
    """Every column of `view`, ticked when shown; clicking one shows or hides it (one stays shown)."""
    header = view.horizontalHeader() if hasattr(view, "horizontalHeader") else view.header()
    model = header.model()
    menu = _StayOpenMenu(view)
    actions = []
    for c in range(header.count()):
        text = str(model.headerData(c, Qt.Horizontal, Qt.DisplayRole) or f"Column {c + 1}")
        a = menu.addAction(text)
        a.setCheckable(True)
        a.setChecked(not header.isSectionHidden(c))
        a.toggled.connect(lambda on, c=c: _set_shown(view, header, c, on))
        actions.append(a)

    def lock_last():
        shown = [a for a in actions if a.isChecked()]
        for a in actions:
            a.setEnabled(not (len(shown) == 1 and a is shown[0]))  # never hide every column
    for a in actions:
        a.toggled.connect(lambda *_: lock_last())
    lock_last()
    return menu


def _set_shown(view, header: QHeaderView, c: int, on: bool) -> None:
    header.setSectionHidden(c, not on)
    if on:
        sizer = getattr(header, "_column_sizer", None)
        width = sizer.header_width(c) if sizer is not None else header.sectionSizeHint(c)
        if header.sectionSize(c) < width:
            header.resizeSection(c, max(width, view.sizeHintForColumn(c)))
    sizer = getattr(header, "_column_sizer", None)
    if sizer is not None:
        sizer.schedule()  # the shown columns span the width again
    key = view.property("columnsKey")
    settings = getattr(view, "_columns_settings", None)
    if key and settings is not None:
        hidden = [str(model_text) for i in range(header.count()) if header.isSectionHidden(i)
                  for model_text in [header.model().headerData(i, Qt.Horizontal, Qt.DisplayRole)]]
        settings.setValue(key, hidden)


def install_picker(view: QAbstractItemView, settings=None, key: str = "") -> None:
    """Right-click the column header of `view` to pick the columns shown; remembered under `key`."""
    header = view.horizontalHeader() if hasattr(view, "horizontalHeader") else view.header()
    view.setProperty("columnsKey", key)
    view._columns_settings = settings
    if settings is not None and key:
        hidden = settings.value(key, []) or []
        hidden = [hidden] if isinstance(hidden, str) else list(hidden)
        names = [str(header.model().headerData(c, Qt.Horizontal, Qt.DisplayRole)) for c in range(header.count())]
        for c, name in enumerate(names):
            header.setSectionHidden(c, name in hidden)
        if all(header.isSectionHidden(c) for c in range(header.count())):
            header.setSectionHidden(0, False)
    header.setContextMenuPolicy(Qt.CustomContextMenu)
    try:
        header.customContextMenuRequested.disconnect()  # replaces the header's add / delete menu
    except (RuntimeError, TypeError):
        pass
    header.customContextMenuRequested.connect(lambda pos: column_menu(view).exec(header.mapToGlobal(pos)))
    header.setToolTip("Right-click to choose the columns shown")
