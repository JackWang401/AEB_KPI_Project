"""Step 2: pick signals from the channels found in the files, and define derived signals."""

from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.kpi_studio.core.model import derived_to_yaml, signal_units
from src.kpi_studio.core.rename import rename_signal_references
from src.kpi_studio.gui.state import AppState
from src.kpi_studio.gui.widgets.common import PAGE_MARGINS, ExprEdit, matches
from src.kpi_studio.gui.widgets.derived_builder import DerivedBuilder
from src.kpi_studio.gui.widgets.signal_viewer import SignalViewer
from src.kpi_studio.io import profile_store
from src.kpi_studio.io.mf4 import ChannelInfo, match_channel

LIB_COLOR = QColor("#6b6b66")


def _num(v) -> str:
    v = float(v)
    return str(int(v)) if v.is_integer() else repr(v)


def format_enum(enum) -> str:
    """'0 = Off, 3 = Served' for a value table; a table name as it is."""
    if isinstance(enum, dict):
        return ", ".join(f"{_num(v)} = {k}" for k, v in sorted(enum.items(), key=lambda kv: kv[1]))
    return enum or ""


def enum_summary(enum) -> str:
    """Short cell text: '8 values: Longitudinal Off, …' for a value table; a table name as it is."""
    if isinstance(enum, dict):
        first = min(enum.items(), key=lambda kv: kv[1])[0] if enum else ""
        return f"{len(enum)} values: {first}, …" if len(enum) > 1 else f"1 value: {first}"
    return enum or ""


def parse_enum(text: str):
    """Inverse of format_enum: a value table when the text has 'value = text' pairs, else a table name."""
    text = text.strip()
    if "=" not in text:
        return text or None
    out = {}
    for part in text.split(","):
        if "=" not in part:
            continue
        v, label = (x.strip() for x in part.split("=", 1))
        try:
            out[label] = int(float(v)) if float(v).is_integer() else float(v)
        except ValueError:
            continue
    return out or None


def _identifier(name: str) -> str:
    base = name.replace("\\", "/").split("/")[-1]
    ident = re.sub(r"[^A-Za-z0-9_]", "_", base).strip("_") or "signal"
    if ident[0].isdigit():
        ident = "s_" + ident
    return ident


class SignalsPage(QWidget):
    DER_COLS = ["Name", "Expression", "Unit", "Description"]
    SIG_COLS = ["Name", "Channel candidates (first match wins)", "Unit", "Kind", "Enum", "Required", "Found in"]

    def __init__(self, state: AppState, parent=None, viewer: SignalViewer | None = None):
        super().__init__(parent)
        self.state = state
        self.viewer = viewer or SignalViewer(state)  # shared panel below the tabs (main window)
        self._loading = False

        split = QSplitter(Qt.Horizontal)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*PAGE_MARGINS)
        outer.setSpacing(6)
        outer.addWidget(split, 1)

        def title(text, tip=""):
            lab = QLabel(text)
            lab.setObjectName("sectionTitle")
            lab.setToolTip(tip)
            return lab

        def compact_table(table, min_rows):
            table.verticalHeader().setDefaultSectionSize(22)
            table.horizontalHeader().setFixedHeight(26)
            table.setMinimumHeight(26 + 22 * min_rows + 4)

        # ---- left: channels in files (title, filter and add button on one line)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(title("Channels", "Channels in the loaded files. Pick the ones you need and give each a "
                                         "short name. When files name the same signal differently, list all "
                                         "channel names; the first one found in a file is used."))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter… (wildcards: * ?  e.g. lmc*it)")
        head.addWidget(self.search, 1)
        self.add_sig_btn = QPushButton("Add →")
        self.add_sig_btn.setToolTip("Add the selected channels as signals (or double-click a channel)")
        head.addWidget(self.add_sig_btn)
        ll.addLayout(head)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Channel", "Unit", "Files"])
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        left.setMinimumWidth(300)
        ll.addWidget(self.tree, 1)
        split.addWidget(left)

        # ---- right: libraries, profile signals, derived signals
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)

        # libraries are not offered any more; the row only shows for a profile that still imports one,
        # so those imports can be removed
        self.lib_row = QWidget()
        imp = QHBoxLayout(self.lib_row)
        imp.setContentsMargins(0, 0, 0, 0)
        imp.addWidget(title("Libraries", "Libraries this profile imports; select one and press Remove"))
        self.imports = QListWidget()
        self.imports.setFixedHeight(28)
        self.imports.setFlow(QListWidget.LeftToRight)
        self.imports.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.imports.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.imports.setStyleSheet("QListWidget::item { padding: 1px 8px; }")
        imp.addWidget(self.imports, 1)
        self.lib_combo = QComboBox()
        self.lib_combo.addItems(sorted(p.name.split(".")[0] for p in profile_store.builtin_library_dir().glob("*.kpi-lib.yaml")))
        self.lib_add = QPushButton("Import")
        self.lib_remove = QPushButton("Remove")
        self.lib_combo.setVisible(False)
        self.lib_add.setVisible(False)
        imp.addWidget(self.lib_remove)
        rl.addWidget(self.lib_row)

        vsplit = QSplitter(Qt.Vertical)
        rl.addWidget(vsplit, 1)

        sig_box = QWidget()
        sl = QVBoxLayout(sig_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(4)
        row = QHBoxLayout()
        row.addWidget(title("Signals", "Grey rows come from an imported library; editing one overrides it here"))
        hint = QLabel("grey rows come from a library")
        hint.setObjectName("hint")
        row.addWidget(hint)
        row.addStretch(1)
        self.del_sig_btn = QPushButton("Delete")
        self.del_sig_btn.setToolTip("Delete the selected signals")
        self.preview_btn = QPushButton("Plot selected ↓")
        row.addWidget(self.del_sig_btn)
        row.addWidget(self.preview_btn)
        sl.addLayout(row)
        self.sig_table = QTableWidget(0, len(self.SIG_COLS))
        self.sig_table.setHorizontalHeaderLabels(self.SIG_COLS)
        self.sig_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        compact_table(self.sig_table, 4)
        sl.addWidget(self.sig_table, 1)
        vsplit.addWidget(sig_box)

        der_box = QWidget()
        dl = QVBoxLayout(der_box)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(4)
        drow = QHBoxLayout()
        drow.addWidget(title("Derived signals", "Pick a signal and a processing method (Add / Edit), "
                                                "or type an expression, e.g. lowpass(x, 10)"))
        dhint = QLabel("a signal + a method, or an expression")
        dhint.setObjectName("hint")
        drow.addWidget(dhint)
        drow.addStretch(1)
        self.add_der_btn = QPushButton("Add…")
        self.add_der_btn.setToolTip("Build a derived signal: pick a signal, a method and its arguments")
        self.edit_der_btn = QPushButton("Edit…")
        self.edit_der_btn.setToolTip("Change the selected derived signal's input, method and arguments")
        self.del_der_btn = QPushButton("Delete")
        self.del_der_btn.setToolTip("Delete the selected derived signals")
        drow.addWidget(self.add_der_btn)
        drow.addWidget(self.edit_der_btn)
        drow.addWidget(self.del_der_btn)
        dl.addLayout(drow)
        self.der_table = QTableWidget(0, len(self.DER_COLS))
        self.der_table.setHorizontalHeaderLabels(self.DER_COLS)
        self.der_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.der_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.der_table.setColumnWidth(2, 80)
        compact_table(self.der_table, 2)
        dl.addWidget(self.der_table, 1)
        vsplit.addWidget(der_box)

        vsplit.setSizes([300, 160])
        split.addWidget(right)
        split.setSizes([380, 820])

        self.search.textChanged.connect(self.refresh_channels)
        self.add_sig_btn.clicked.connect(self.add_selected_channels)
        self.tree.itemDoubleClicked.connect(lambda *_: self.add_selected_channels())
        self.del_sig_btn.clicked.connect(self.delete_signal)
        self.preview_btn.clicked.connect(self.preview_signal)
        self.add_der_btn.clicked.connect(self.add_derived)
        self.edit_der_btn.clicked.connect(lambda: self.edit_derived())
        self.del_der_btn.clicked.connect(self.delete_derived)
        self.sig_table.itemChanged.connect(self.signal_cell_changed)
        self.sig_table.itemDoubleClicked.connect(self._enum_double_clicked)
        self.der_table.itemChanged.connect(self.derived_cell_changed)
        self.lib_add.clicked.connect(self.add_import)
        self.lib_remove.clicked.connect(self.remove_import)

        state.files_changed.connect(self.refresh_channels)
        state.files_changed.connect(self.refresh_signals)
        state.profile_changed.connect(self.refresh_all)
        # units / descriptions come from the files without asking: fill them whenever files or signals change
        state.files_changed.connect(self._schedule_fill)
        state.profile_changed.connect(self._schedule_fill)
        state.validated.connect(self.refresh_signals_if_imports)
        self._shown_resolved_sig = None
        self.last_rename = None
        self.refresh_all()

    # ------------------------------------------------------------------ channels
    def _channel_union(self) -> dict[str, dict]:
        union: dict[str, dict] = {}
        for f in self.state.files:
            idx = self.state.indexes.get(f)
            if idx is None or not idx.ok:
                continue
            for c in idx.channels:
                key = f"{c.group_name}::{c.name}"
                e = union.setdefault(key, {"info": c, "files": set()})
                e["files"].add(f)
        return union

    def refresh_channels(self):
        needle = self.search.text().strip()
        union = self._channel_union()
        n_files = sum(1 for f in self.state.files if self.state.indexes.get(f) and self.state.indexes[f].ok)
        self.tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        shown = 0
        for key in sorted(union):
            e = union[key]
            c: ChannelInfo = e["info"]
            if needle and not (matches(needle, key) or matches(needle, c.name)):
                continue
            g = groups.get(c.group_name)
            if g is None:
                g = groups[c.group_name] = QTreeWidgetItem([c.group_name, "", ""])
                self.tree.addTopLevelItem(g)
            item = QTreeWidgetItem([c.name, c.unit, f"{len(e['files'])}/{n_files}"])
            item.setData(0, Qt.UserRole, key)
            if len(e["files"]) < n_files:
                item.setForeground(2, QColor("#c27a00"))
            g.addChild(item)
            shown += 1
            if shown > 5000:
                break
        if needle or len(groups) < 8:
            self.tree.expandAll()

    def add_selected_channels(self):
        sigs = self.state.data.setdefault("signals", {})
        union = self._channel_union()
        added = 0
        for item in self.tree.selectedItems():
            key = item.data(0, Qt.UserRole)
            if not key:
                continue
            c = union[key]["info"]
            name = _identifier(c.name)
            base = name
            i = 2
            while name in sigs or name in self.state.data.get("derived", {}):
                name = f"{base}_{i}"
                i += 1
            sigs[name] = self._from_channel({"channels": [key]}, c)
            added += 1
        if added:
            self.state.edited(structure=True)

    @staticmethod
    def _from_channel(d: dict, c: ChannelInfo, *, only_empty: bool = False) -> dict:
        """Take unit, kind and description from the file's channel information."""
        if c.unit and not (only_empty and d.get("unit")):
            d["unit"] = c.unit
        if c.enum and not d.get("enum"):
            d["enum"] = {text: value for text, value in c.enum}  # the file's value table
            d["kind"] = "discrete"
        if c.comment and not (only_empty and d.get("description")):
            d["description"] = c.comment
        return d

    def _schedule_fill(self):
        QTimer.singleShot(0, self.fill_from_files)

    def fill_from_files(self) -> list[str]:
        """Fill empty units and descriptions (and mark text-valued channels discrete) from the loaded files.

        Runs by itself whenever files are indexed or the signals change; never overwrites what the user set."""
        sigs = self.state.data.get("signals", {}) or {}
        channels = [c for f in self.state.files if (idx := self.state.indexes.get(f)) is not None and idx.ok
                    for c in idx.channels]
        if not channels or not sigs:
            return []
        changed = []
        for name, d in sigs.items():
            before = dict(d)
            for cand in d.get("channels", []):
                c, _ = match_channel(cand, channels)
                if c is not None:
                    self._from_channel(d, c, only_empty=True)
                    break
            if d != before:
                changed.append(name)
        if changed:
            self.state.edited(structure=True)
            win = self.window()
            if hasattr(win, "statusBar"):
                win.statusBar().showMessage(f"Units / descriptions taken from the files for: {', '.join(changed)}",
                                            8000)
        return changed

    # ------------------------------------------------------------------ signals table
    def refresh_all(self):
        self._loading = True
        self.imports.clear()
        self.imports.addItems(self.state.data.get("imports", []) or [])
        self.lib_row.setVisible(bool(self.state.data.get("imports")))
        self._loading = False
        self.refresh_signals()
        self.refresh_derived()

    def refresh_signals_if_imports(self):
        # library signals appear only after the profile resolves; refresh when that set changes
        names = tuple(self.state.resolved.signals) if self.state.resolved else ()
        if names != self._shown_resolved_sig:
            self.refresh_signals()

    def _rows(self):
        own = self.state.data.get("signals", {}) or {}
        rows = [(n, d, False) for n, d in own.items()]
        lib_signals, _ = self.state.library_definitions()  # from the files: never stale after a rename
        for n, sdef in lib_signals.items():
            if n not in own:
                rows.append((n, sdef.model_dump(exclude_none=True), True))
        return rows

    def _found(self, sdict: dict) -> str:
        """In how many recordings (a combined recording counts once) any channel candidate exists."""
        idx = self.state.indexes
        recs = [[f for f in r.files if idx.get(f) and idx[f].ok] for r in self.state.recordings()]
        recs = [r for r in recs if r]
        if not recs:
            return ""
        hits = sum(1 for files in recs
                   if any(match_channel(c, idx[f].channels)[0] is not None
                          for c in sdict.get("channels", []) for f in files))
        return f"{hits}/{len(recs)}"

    def refresh_signals(self):
        self._loading = True
        rows = self._rows()
        self._shown_resolved_sig = tuple(self.state.resolved.signals) if self.state.resolved else ()
        self.sig_table.setRowCount(len(rows))
        for r, (name, d, from_lib) in enumerate(rows):
            vals = [name, ", ".join(d.get("channels", [])), d.get("unit") or "", d.get("kind", "continuous"),
                    enum_summary(d.get("enum")), "yes" if d.get("required", True) else "no", self._found(d)]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setData(Qt.UserRole, (name, from_lib))
                if c == 0 and d.get("description"):
                    item.setToolTip(d["description"])
                if c == 4:
                    if isinstance(d.get("enum"), dict):
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)  # edited in the table dialog
                        item.setToolTip("\n".join(f"{_num(v)} = {k}" for k, v in
                                                  sorted(d["enum"].items(), key=lambda kv: kv[1]))
                                        + "\n\nDouble-click to see or edit the values")
                    else:
                        item.setToolTip("Name of an enum table, or double-click with a value table to edit it")
                if from_lib:
                    item.setForeground(LIB_COLOR)
                if c in (0, 6) and (from_lib or c == 6):
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if c == 6 and v and v.split("/")[0] != v.split("/")[1]:
                    item.setForeground(QColor("#c27a00") if not d.get("required", True) else QColor("#e34948"))
                self.sig_table.setItem(r, c, item)
        self._loading = False

    def signal_cell_changed(self, item: QTableWidgetItem):
        if self._loading:
            return
        name, from_lib = item.data(Qt.UserRole)
        sigs = self.state.data.setdefault("signals", {})
        if from_lib and name not in sigs:
            sigs[name] = self.state.resolved.signals[name].model_dump(exclude_none=True)
        d = sigs[name]
        col, text = item.column(), item.text().strip()
        if col == 0:
            if text != name:
                if self._rename_ok(text):
                    self.state.data["signals"] = {(text if k == name else k): v for k, v in sigs.items()}
                    self._renamed(name, text)
            self.state.edited(structure=True)
            return
        if col == 1:
            d["channels"] = [c.strip() for c in text.split(",") if c.strip()]
        elif col == 2:
            d["unit"] = text or None
        elif col == 3:
            d["kind"] = "discrete" if text.lower().startswith("d") else "continuous"
        elif col == 4:
            d["enum"] = parse_enum(text)
        elif col == 5:
            d["required"] = text.lower() not in ("no", "false", "0", "n")
        self.state.edited(structure=from_lib)

    def _enum_double_clicked(self, item):
        if item.column() != 4:
            return
        name, from_lib = item.data(Qt.UserRole)
        sigs = self.state.data.setdefault("signals", {})
        current = (sigs.get(name) or {}).get("enum") if not from_lib or name in sigs else None
        if from_lib and name not in sigs:
            current = self.state.resolved.signals[name].enum if self.state.resolved is not None else None
        if not isinstance(current, dict):
            return  # a table name: edited in the cell
        dlg = EnumDialog(name, current, self)
        if dlg.exec() != EnumDialog.Accepted:
            return
        if from_lib and name not in sigs:
            sigs[name] = self.state.resolved.signals[name].model_dump(exclude_none=True)
        sigs[name]["enum"] = dlg.values() or None
        self.state.edited(structure=True)

    def delete_signal(self):
        rows = sorted({i.row() for i in self.sig_table.selectedIndexes()}, reverse=True)
        sigs = self.state.data.get("signals", {})
        changed = False
        for r in rows:
            name, from_lib = self.sig_table.item(r, 0).data(Qt.UserRole)
            if name in sigs:
                del sigs[name]
                changed = True
            elif from_lib:
                QMessageBox.information(self, "Library signal",
                                        f"'{name}' comes from an imported library; remove the library to drop it.")
        if changed:
            self.state.edited(structure=True)

    def preview_signal(self):
        rows = sorted({i.row() for i in self.sig_table.selectedIndexes()})
        rows += sorted({i.row() + 10_000 for i in self.der_table.selectedIndexes()})
        names = [self.sig_table.item(r, 0).data(Qt.UserRole)[0] for r in rows if r < 10_000]
        names += [self.der_table.item(r - 10_000, 0).text() for r in rows
                  if r >= 10_000 and self.der_table.item(r - 10_000, 0)]
        if not names:
            self.viewer.status.setText("Select rows in the signals or derived table first, or tick signals in the panel below.")
            return
        self.viewer.plot_signals(names)

    def shutdown(self):
        self.viewer.shutdown()

    # ------------------------------------------------------------------ derived
    @staticmethod
    def _der_fields(value) -> dict:
        """{expr, unit, description} from either YAML form (plain expression or mapping)."""
        if isinstance(value, dict):
            return {"expr": value.get("expr", "") or "", "unit": value.get("unit") or "",
                    "description": value.get("description", "") or ""}
        return {"expr": value or "", "unit": "", "description": ""}

    def _set_der_field(self, name, key, text):
        derived = self.state.data.setdefault("derived", {})
        if name not in derived:
            return False
        f = self._der_fields(derived[name])
        if f[key] == text:
            return False
        f[key] = text
        derived[name] = derived_to_yaml(f)
        return True

    def refresh_derived(self):
        self._loading = True
        derived = self.state.data.get("derived", {}) or {}
        self.der_table.setRowCount(len(derived))
        for r, (name, value) in enumerate(derived.items()):
            f = self._der_fields(value)
            item = QTableWidgetItem(name)
            item.setData(Qt.UserRole, name)
            item.setToolTip(f["description"])
            self.der_table.setItem(r, 0, item)
            edit = ExprEdit(self._names_except(name), enums_fn=self.state.enum_map,
                            build_fn=lambda _e, n=name: self.edit_derived(n),
                            build_label="Build with a method… (signal, method, arguments)")
            edit.setText(f["expr"])
            edit.editingFinished.connect(lambda n=name, e=edit: self._derived_expr(n, e.text()))
            self.der_table.setCellWidget(r, 1, edit)
            unit = QTableWidgetItem(f["unit"])
            unit.setToolTip("Unit shown in the signal viewer and plots, e.g. km/h")
            self.der_table.setItem(r, 2, unit)
            desc = QTableWidgetItem(f["description"])
            desc.setToolTip(f["description"] or "What this signal means (free text)")
            self.der_table.setItem(r, 3, desc)
        self._loading = False

    def _names_except(self, name):
        return lambda: [n for n in self.state.signal_names() if n != name]

    def _derived_expr(self, name, text):
        if self._set_der_field(name, "expr", text.strip()):
            self.state.edited()

    def derived_cell_changed(self, item):
        if self._loading:
            return
        name = self.der_table.item(item.row(), 0).data(Qt.UserRole)
        if item.column() == 0:
            new = item.text().strip()
            derived = self.state.data.setdefault("derived", {})
            if new != name and self._rename_ok(new):
                self.state.data["derived"] = {(new if k == name else k): v for k, v in derived.items()}
                self._renamed(name, new)
                self.state.edited(structure=True)
            elif new != name:
                QTimer.singleShot(0, self.refresh_derived)  # put the old name back
        elif item.column() in (2, 3):
            key = "unit" if item.column() == 2 else "description"
            if self._set_der_field(name, key, item.text().strip()):
                self.state.edited()

    def _units(self) -> dict:
        units = {n: (d.get("unit") or "") for n, d in (self.state.data.get("signals", {}) or {}).items()}
        if self.state.resolved is not None:
            units = {**signal_units(self.state.resolved), **units}
        for n, v in (self.state.data.get("derived", {}) or {}).items():
            units[n] = self._der_fields(v)["unit"]
        return units

    def open_builder(self, name: str = "") -> Optional[dict]:
        """Run the derived-signal dialog; returns its values, or None when cancelled."""
        derived = self.state.data.get("derived", {}) or {}
        f = self._der_fields(derived.get(name)) if name else {"expr": "", "unit": "", "description": ""}
        dlg = DerivedBuilder(self.state.signal_names(), self._units(), self, name=name, expr=f["expr"],
                             unit=f["unit"], description=f["description"],
                             taken=set(self.state.signal_names()) | set(derived))
        return dlg.values() if dlg.exec() == DerivedBuilder.Accepted else None

    def add_derived(self):
        v = self.open_builder()
        if v is None:
            return
        self.state.data.setdefault("derived", {})[v["name"]] = derived_to_yaml(v)
        self.state.edited(structure=True)

    def edit_derived(self, name: Optional[str] = None):
        if name is None:
            row = self.der_table.currentRow()
            if row < 0:
                return
            name = self.der_table.item(row, 0).data(Qt.UserRole)
        v = self.open_builder(name)
        if v is None:
            return
        derived = self.state.data.setdefault("derived", {})
        # keep the table order when renaming
        self.state.data["derived"] = {(v["name"] if k == name else k): (derived_to_yaml(v) if k == name else d)
                                      for k, d in derived.items()}
        if v["name"] != name:
            self._renamed(name, v["name"])
        self.state.edited(structure=True)

    def _rename_ok(self, new: str) -> bool:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", new):
            QMessageBox.warning(self, "Rename", f"'{new}' is not a valid name: use letters, digits and _, "
                                               "not starting with a digit.")
            return False
        if new in self.state.taken_names():
            QMessageBox.warning(self, "Rename", f"'{new}' is already a signal or derived signal.")
            return False
        return True

    def _renamed(self, old: str, new: str):
        """Point every expression, moment, KPI and plot that used `old` at `new`."""
        n = rename_signal_references(self.state.data, old, new)
        self.last_rename = (old, new, n)
        win = self.window()
        if n and hasattr(win, "statusBar"):
            win.statusBar().showMessage(f"Renamed {old} → {new}: updated {n} place{'s' if n != 1 else ''} "
                                        f"in derived signals, events, KPIs and plots", 10000)

    def delete_derived(self):
        rows = sorted({i.row() for i in self.der_table.selectedIndexes()}, reverse=True)
        derived = self.state.data.get("derived", {})
        for r in rows:
            derived.pop(self.der_table.item(r, 0).data(Qt.UserRole), None)
        if rows:
            self.state.edited(structure=True)

    # ------------------------------------------------------------------ imports
    def add_import(self):
        lib = self.lib_combo.currentText()
        imports = self.state.data.setdefault("imports", [])
        if lib and lib not in imports:
            imports.append(lib)
            self.state.edited(structure=True)

    def remove_import(self):
        item = self.imports.currentItem()
        if item:
            self.state.data["imports"] = [i for i in self.state.data.get("imports", []) if i != item.text()]
            self.state.edited(structure=True)


class EnumDialog(QDialog):
    """The value table of an enum signal, one value per row (raw value | text)."""

    def __init__(self, signal: str, enum: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Enum values of {signal}")
        self.resize(460, 420)
        lay = QVBoxLayout(self)
        info = QLabel("Use the text in conditions, e.g. " + signal + ' == "' + (next(iter(enum), "") or "TEXT") + '"')
        info.setWordWrap(True)
        lay.addWidget(info)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Value", "Text"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        for text, value in sorted(enum.items(), key=lambda kv: kv[1]):
            self._add(_num(value), text)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        add = QPushButton("Add value")
        delete = QPushButton("Delete value")
        add.clicked.connect(lambda: self._add("", ""))
        delete.clicked.connect(lambda: [self.table.removeRow(r) for r in
                                        sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)])
        row.addWidget(add)
        row.addWidget(delete)
        row.addStretch(1)
        lay.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _add(self, value: str, text: str):
        r = self.table.rowCount()
        self.table.insertRow(r)
        v = QTableWidgetItem(value)
        v.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.table.setItem(r, 0, v)
        self.table.setItem(r, 1, QTableWidgetItem(text))

    def values(self) -> dict:
        out = {}
        for r in range(self.table.rowCount()):
            v, t = self.table.item(r, 0), self.table.item(r, 1)
            text = t.text().strip() if t else ""
            try:
                num = float(v.text()) if v else None
            except ValueError:
                num = None
            if text and num is not None:
                out[text] = int(num) if num.is_integer() else num
        return out
