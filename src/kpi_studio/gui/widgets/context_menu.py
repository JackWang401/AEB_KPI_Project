"""Right-click menus for lists, tables and trees: every add / delete in KPI Studio lives in one.

A right-click on a row offers that row's actions (Delete, Edit…) and, below a separator, the view's
add actions; a right-click on the blank space or the column header offers the add actions alone, so
adding never depends on there being blank space left."""

from __future__ import annotations

from typing import Callable, Iterable, Optional

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QMenu, QTableView, QTreeView

Action = tuple[str, Callable[[], object]]


def _select(view: QAbstractItemView, pos: QPoint) -> None:
    """Make the right-clicked row part of the selection (keep a multi-selection it belongs to)."""
    index = view.indexAt(pos)
    if not index.isValid():
        return
    sm = view.selectionModel()
    if isinstance(view, QTreeView):
        if not sm.isSelected(index):
            view.setCurrentIndex(index)
        return
    if not sm.isRowSelected(index.row(), index.parent()):
        if isinstance(view, QTableView):
            view.selectRow(index.row())
        view.setCurrentIndex(index)


def build_menu(view: QAbstractItemView, pos: Optional[QPoint],
               add: Iterable[Action] = (),
               item: Optional[Callable[[set[int]], list[Action]]] = None,
               row: Optional[int] = None) -> Optional[QMenu]:
    """The menu for a right-click at viewport `pos` (or on `row`, e.g. from the row header).

    `item(rows)` returns the actions for the selected rows (rows = selected row numbers)."""
    if row is None and pos is not None and view.indexAt(pos).isValid():
        _select(view, pos)
        row = view.indexAt(pos).row()
    elif row is not None and row >= 0 and isinstance(view, QTableView):
        if not view.selectionModel().isRowSelected(row, view.rootIndex()):
            view.selectRow(row)
    menu = QMenu(view)
    if row is not None and row >= 0 and item is not None:
        rows = {i.row() for i in view.selectionModel().selectedIndexes()} or {row}
        for label, fn in item(rows):
            menu.addAction(label).triggered.connect(lambda *_, fn=fn: fn())
    add = list(add)
    if add:
        if menu.actions():
            menu.addSeparator()
        for label, fn in add:
            menu.addAction(label).triggered.connect(lambda *_, fn=fn: fn())
    return menu if menu.actions() else None


_MENUS: dict[int, tuple] = {}


def menu_for(view: QAbstractItemView, pos: Optional[QPoint] = None, row: Optional[int] = None) -> Optional[QMenu]:
    """The menu `install` shows for a right-click at viewport `pos` (or on `row`); for tests."""
    add, item = _MENUS[id(view)]
    return build_menu(view, pos, add, item, row=row)


def install(view: QAbstractItemView, add: Iterable[Action] = (),
            item: Optional[Callable[[set[int]], list[Action]]] = None) -> None:
    """Right-click menus on the rows, the blank space and the headers of `view`."""
    add = list(add)
    view.setProperty("hasRowMenu", True)
    _MENUS[id(view)] = (add, item)

    def show(menu, global_pos):
        if menu is not None:
            menu.exec(global_pos)

    view.setContextMenuPolicy(Qt.CustomContextMenu)
    view.customContextMenuRequested.connect(
        lambda pos: show(build_menu(view, pos, add, item), view.viewport().mapToGlobal(pos)))
    headers: list[QHeaderView] = []
    if isinstance(view, QTableView):
        headers = [view.horizontalHeader(), view.verticalHeader()]
    elif isinstance(view, QTreeView):
        headers = [view.header()]
    for h in headers:
        h.setContextMenuPolicy(Qt.CustomContextMenu)
        vertical = h.orientation() == Qt.Vertical
        h.customContextMenuRequested.connect(
            lambda pos, h=h, vertical=vertical: show(
                build_menu(view, None, add, item, row=h.logicalIndexAt(pos) if vertical else -1),
                h.mapToGlobal(pos)))
    tip = "Right-click to add" + (" or delete" if item is not None else "") if add else \
        ("Right-click a row for its actions" if item is not None else "")
    if tip and not view.toolTip():
        view.setToolTip(tip)
