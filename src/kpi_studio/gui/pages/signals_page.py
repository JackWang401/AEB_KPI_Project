"""Step 2: pick signals from the channels found in the files, and define derived signals."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
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

from src.kpi_studio.gui.state import AppState
from src.kpi_studio.gui.widgets.common import ExprEdit, make_canvas
from src.kpi_studio.io import profile_store
from src.kpi_studio.io.mf4 import ChannelInfo, match_channel, read_signals
from src.kpi_studio.core.model import SignalDef

LIB_COLOR = QColor("#6b6b66")


def _identifier(name: str) -> str:
    base = name.replace("\\", "/").split("/")[-1]
    ident = re.sub(r"[^A-Za-z0-9_]", "_", base).strip("_") or "signal"
    if ident[0].isdigit():
        ident = "s_" + ident
    return ident


class SignalsPage(QWidget):
    SIG_COLS = ["Name", "Channel candidates (first match wins)", "Unit", "Kind", "Enum", "Required", "Found in"]

    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
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
        self.search.setPlaceholderText("Filter channels…")
        ll.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Channel", "Unit", "Files"])
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
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

        rl.addWidget(QLabel("Signals (grey rows come from an imported library; editing one overrides it here)"))
        self.sig_table = QTableWidget(0, len(self.SIG_COLS))
        self.sig_table.setHorizontalHeaderLabels(self.SIG_COLS)
        self.sig_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        rl.addWidget(self.sig_table, 2)
        row = QHBoxLayout()
        self.del_sig_btn = QPushButton("Delete signal")
        self.preview_btn = QPushButton("Preview signal")
        self.preview_file = QComboBox()
        row.addWidget(self.del_sig_btn)
        row.addStretch(1)
        row.addWidget(QLabel("from"))
        row.addWidget(self.preview_file, 1)
        row.addWidget(self.preview_btn)
        rl.addLayout(row)

        rl.addWidget(QLabel("Derived signals (expressions over signals; use the toolbox, e.g. lowpass(x, 10))"))
        self.der_table = QTableWidget(0, 2)
        self.der_table.setHorizontalHeaderLabels(["Name", "Expression"])
        self.der_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        rl.addWidget(self.der_table, 1)
        drow = QHBoxLayout()
        self.add_der_btn = QPushButton("Add derived")
        self.del_der_btn = QPushButton("Delete derived")
        drow.addWidget(self.add_der_btn)
        drow.addWidget(self.del_der_btn)
        drow.addStretch(1)
        rl.addLayout(drow)

        self.fig, self.canvas, _ = make_canvas()
        self.canvas.setMinimumHeight(180)
        rl.addWidget(self.canvas, 1)
        split.addWidget(right)
        split.setSizes([380, 820])

        self.search.textChanged.connect(self.refresh_channels)
        self.add_sig_btn.clicked.connect(self.add_selected_channels)
        self.tree.itemDoubleClicked.connect(lambda *_: self.add_selected_channels())
        self.del_sig_btn.clicked.connect(self.delete_signal)
        self.preview_btn.clicked.connect(self.preview_signal)
        self.add_der_btn.clicked.connect(self.add_derived)
        self.del_der_btn.clicked.connect(self.delete_derived)
        self.sig_table.itemChanged.connect(self.signal_cell_changed)
        self.der_table.itemChanged.connect(self.derived_name_changed)
        self.lib_add.clicked.connect(self.add_import)
        self.lib_remove.clicked.connect(self.remove_import)

        state.files_changed.connect(self.refresh_channels)
        state.files_changed.connect(self.refresh_signals)
        state.profile_changed.connect(self.refresh_all)
        state.validated.connect(self.refresh_signals_if_imports)
        self._shown_resolved_sig = None
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
        needle = self.search.text().strip().lower()
        union = self._channel_union()
        n_files = sum(1 for f in self.state.files if self.state.indexes.get(f) and self.state.indexes[f].ok)
        self.tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        shown = 0
        for key in sorted(union):
            e = union[key]
            c: ChannelInfo = e["info"]
            if needle and needle not in key.lower():
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
        ok = [f for f in self.state.files if self.state.indexes.get(f) and self.state.indexes[f].ok]
        if not ok:
            return ""
        hits = 0
        for f in ok:
            for cand in sdict.get("channels", []):
                if match_channel(cand, self.state.indexes[f].channels)[0] is not None:
                    hits += 1
                    break
        return f"{hits}/{len(ok)}"

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
        files = [Path(f).name for f in self.state.files]
        cur = self.preview_file.currentText()
        self.preview_file.clear()
        self.preview_file.addItems(files)
        if cur in files:
            self.preview_file.setCurrentText(cur)

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
            if text and text != name and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", text) and text not in sigs:
                sigs[text] = sigs.pop(name)
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
        if not rows or not self.state.files:
            return
        name, from_lib = self.sig_table.item(rows[0], 0).data(Qt.UserRole)
        sdict = dict(self.state.data.get("signals", {}).get(name) or
                     (self.state.resolved.signals[name].model_dump(exclude_none=True) if self.state.resolved else {}))
        f = self.state.files[max(self.preview_file.currentIndex(), 0)]
        try:
            raw, issues = read_signals(f, {name: SignalDef.model_validate(sdict)}, self.state.indexes.get(f))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Preview", str(exc))
            return
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        if name in raw:
            r = raw[name]
            try:
                y = np.asarray(r.samples, dtype=float)
            except (TypeError, ValueError):
                y = np.full(len(r.timestamps), np.nan)
            ax.plot(r.timestamps, y, color="#2a78d6", lw=1.5)
            ax.set_title(f"{name}  ←  {r.channel}  [{r.unit}]  ({Path(f).name})", fontsize=9)
        else:
            ax.set_title(f"{name}: not found in {Path(f).name}", fontsize=9)
        ax.grid(True, color="#e6e5e0")
        ax.set_xlabel("time [s]")
        self.canvas.draw_idle()

    # ------------------------------------------------------------------ derived
    def refresh_derived(self):
        self._loading = True
        derived = self.state.data.get("derived", {}) or {}
        self.der_table.setRowCount(len(derived))
        for r, (name, expr) in enumerate(derived.items()):
            item = QTableWidgetItem(name)
            item.setData(Qt.UserRole, name)
            self.der_table.setItem(r, 0, item)
            edit = ExprEdit(self._names_except(name), enums_fn=self.state.enum_map)
            edit.setText(expr)
            edit.editingFinished.connect(lambda n=name, e=edit: self._derived_expr(n, e.text()))
            self.der_table.setCellWidget(r, 1, edit)
        self._loading = False

    def _names_except(self, name):
        return lambda: [n for n in self.state.signal_names() if n != name]

    def _derived_expr(self, name, text):
        derived = self.state.data.setdefault("derived", {})
        if name in derived and derived[name] != text.strip():
            derived[name] = text.strip()
            self.state.edited()

    def derived_name_changed(self, item):
        if self._loading or item.column() != 0:
            return
        old = item.data(Qt.UserRole)
        new = item.text().strip()
        derived = self.state.data.setdefault("derived", {})
        if new and new != old and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", new) and new not in derived:
            derived[new] = derived.pop(old)
            self.state.edited(structure=True)

    def add_derived(self):
        name, ok = QInputDialog.getText(self, "Derived signal", "Name:")
        if not ok or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name.strip()):
            return
        self.state.data.setdefault("derived", {})[name.strip()] = ""
        self.state.edited(structure=True)

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
