"""Step 2: pick signals from the channels found in the files, and define derived signals."""

from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
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
from src.kpi_studio.gui.widgets.common import ExprEdit, matches
from src.kpi_studio.gui.widgets.derived_builder import DerivedBuilder
from src.kpi_studio.gui.widgets.signal_viewer import SignalViewer
from src.kpi_studio.io import profile_store
from src.kpi_studio.io.mf4 import ChannelInfo, match_channel

LIB_COLOR = QColor("#6b6b66")


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
        intro = QLabel("Pick the channels you need and give each a short name. When files name the same signal "
                       "differently, list all channel names; the first one found in a file is used.")
        intro.setWordWrap(True)
        outer.addWidget(intro)
        outer.addWidget(split, 1)

        # ---- left: channels in files
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Channels in the loaded files"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter channels… (wildcards: * ?  e.g. lmc*it)")
        ll.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Channel", "Unit", "Files"])
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        left.setMinimumWidth(300)
        ll.addWidget(self.tree, 1)
        self.add_sig_btn = QPushButton("Add selected as signals →")
        ll.addWidget(self.add_sig_btn)
        split.addWidget(left)

        # ---- right: profile signals, imports, derived, preview
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)

        imp = QHBoxLayout()
        imp.addWidget(QLabel("Libraries:"))
        self.imports = QListWidget()
        self.imports.setMaximumHeight(60)
        self.imports.setFlow(QListWidget.LeftToRight)
        imp.addWidget(self.imports, 1)
        self.lib_combo = QComboBox()
        self.lib_combo.addItems(sorted(p.name.split(".")[0] for p in profile_store.builtin_library_dir().glob("*.kpi-lib.yaml")))
        self.lib_add = QPushButton("Import")
        self.lib_remove = QPushButton("Remove")
        imp.addWidget(self.lib_combo)
        imp.addWidget(self.lib_add)
        imp.addWidget(self.lib_remove)
        rl.addLayout(imp)

        vsplit = QSplitter(Qt.Vertical)
        rl.addWidget(vsplit, 1)

        sig_box = QWidget()
        sl = QVBoxLayout(sig_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(QLabel("Signals (grey rows come from an imported library; editing one overrides it here)"))
        self.sig_table = QTableWidget(0, len(self.SIG_COLS))
        self.sig_table.setHorizontalHeaderLabels(self.SIG_COLS)
        self.sig_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.sig_table.verticalHeader().setDefaultSectionSize(24)
        sl.addWidget(self.sig_table, 1)
        row = QHBoxLayout()
        self.del_sig_btn = QPushButton("Delete signal")
        self.preview_btn = QPushButton("Plot selected signals ↓")
        row.addWidget(self.del_sig_btn)
        row.addStretch(1)
        row.addWidget(self.preview_btn)
        sl.addLayout(row)
        vsplit.addWidget(sig_box)

        der_box = QWidget()
        dl = QVBoxLayout(der_box)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(QLabel("Derived signals: pick a signal and a processing method (Add / Edit), "
                            "or type an expression, e.g. lowpass(x, 10)"))
        self.der_table = QTableWidget(0, len(self.DER_COLS))
        self.der_table.setHorizontalHeaderLabels(self.DER_COLS)
        self.der_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.der_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.der_table.setColumnWidth(2, 80)
        self.der_table.verticalHeader().setDefaultSectionSize(24)
        dl.addWidget(self.der_table, 1)
        drow = QHBoxLayout()
        self.add_der_btn = QPushButton("Add derived…")
        self.edit_der_btn = QPushButton("Edit…")
        self.edit_der_btn.setToolTip("Change the selected derived signal's input, method and arguments")
        self.del_der_btn = QPushButton("Delete derived")
        drow.addWidget(self.add_der_btn)
        drow.addWidget(self.edit_der_btn)
        drow.addWidget(self.del_der_btn)
        drow.addStretch(1)
        dl.addLayout(drow)
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
        self.der_table.itemChanged.connect(self.derived_cell_changed)
        self.lib_add.clicked.connect(self.add_import)
        self.lib_remove.clicked.connect(self.remove_import)

        state.files_changed.connect(self.refresh_channels)
        state.files_changed.connect(self.refresh_signals)
        state.profile_changed.connect(self.refresh_all)
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
            sigs[name] = {"channels": [key], "unit": c.unit or None}
            added += 1
        if added:
            self.state.edited(structure=True)

    # ------------------------------------------------------------------ signals table
    def refresh_all(self):
        self._loading = True
        self.imports.clear()
        self.imports.addItems(self.state.data.get("imports", []) or [])
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
        if self.state.resolved is not None:
            for n, sdef in self.state.resolved.signals.items():
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
                    d.get("enum") or "", "yes" if d.get("required", True) else "no", self._found(d)]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setData(Qt.UserRole, (name, from_lib))
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
            if text and text != name and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", text) \
                    and text not in self.state.signal_names():
                self.state.data["signals"] = {(text if k == name else k): v for k, v in sigs.items()}  # keep order
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
            d["enum"] = text or None
        elif col == 5:
            d["required"] = text.lower() not in ("no", "false", "0", "n")
        self.state.edited(structure=from_lib)

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
            if new and new != name and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", new) \
                    and new not in self.state.signal_names():
                self.state.data["derived"] = {(new if k == name else k): v for k, v in derived.items()}
                self._renamed(name, new)
                self.state.edited(structure=True)
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
