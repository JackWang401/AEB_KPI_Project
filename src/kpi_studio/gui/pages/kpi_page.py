"""Step 3: define events, windows, moments and KPIs (previewed in the shared signal panel)."""

from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.kpi_studio import toolbox
from src.kpi_studio.core.model import (
    BUILTIN_MOMENTS,
    BUILTIN_RANGES,
    KpiDef,
    signal_units,
)
from src.kpi_studio.core.rename import rename_kpi, rename_moment, rename_range, use_named_ranges
from src.kpi_studio.core.validate import kpi_unit
from src.kpi_studio.gui.state import AppState
from src.kpi_studio.gui.widgets import context_menu
from src.kpi_studio.gui.widgets.common import (
    PAGE_MARGINS,
    ConditionBuilder,
    ConditionRow,
    parse_condition_rows,
    ExprEdit,
    ToolPalette,
    format_options,
    parse_options,
)
from src.kpi_studio.gui.widgets.flow_layout import FlowLayout
from src.kpi_studio.toolbox.units import plain as plain_unit
from src.kpi_studio.toolbox.units import pretty as pretty_unit

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FORMS = [("value", "At a moment"), ("range", "Over a range"), ("formula", "Formula of earlier KPIs")]
# reducers that are not toolbox tools: the range's length, and the conditions met in it
SPECIAL_REDUCERS = [("duration", "duration (length of the range)"), ("which", "which conditions are met (text)")]


class WhichTable(QWidget):
    """Label + condition rows of a 'which' KPI: its result names the conditions met in the range.
    Each condition is signal / operator / value in its own column, so each column can be dragged wider."""

    COLUMNS = ["Label", "Signal", "Operator", "Value"]

    def __init__(self, state, on_change):
        super().__init__()
        self.state, self.on_change = state, on_change
        self.rows: list[ConditionRow] = []  # one per table row, in order
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)  # no stretching gaps in the form
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        row = QHBoxLayout()  # hint with + / − on the right, as above the Moments table
        hint = QLabel("The result lists the labels whose condition is met in the range, e.g. PB + FB "
                      "(in this order), or none.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        hint.setMinimumWidth(120)
        row.addWidget(hint, 1)
        for b in context_menu.plus_minus(add=self.add_row, remove=self.remove_row, add_tip="Add a condition",
                                         remove_tip="Delete the selected condition"):
            row.addWidget(b)
        lay.addLayout(row)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        h = self.table.horizontalHeader()
        for col in (1, 3):  # Signal and Value share the free width
            h.setSectionResizeMode(col, QHeaderView.Stretch)
        for col, width in ((0, 70), (2, 90)):  # Label and Operator: set widths (still draggable)
            h.setSectionResizeMode(col, QHeaderView.Fixed)
            h.resizeSection(col, width)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(110)
        self.table.setMaximumHeight(170)  # a few rows; the form below stays in view
        self.table.itemChanged.connect(lambda *_: self.on_change())
        lay.addWidget(self.table)

    def set_items(self, items: dict):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.rows.clear()
        for label, cond in items.items():
            self._append(label, cond)
        self.table.blockSignals(False)

    def _append(self, label="", cond=""):
        r = self.table.rowCount()
        blocked = self.table.blockSignals(True)
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(label))
        self.table.blockSignals(blocked)
        row = ConditionRow(self.state.signal_names, self.state.enum_map, self.table, laid_out=False)
        row.hide()  # holds the logic; its fields sit in the cells
        row.set_expression(cond)
        row.changed.connect(self.on_change)
        self.rows.append(row)
        for col, w in enumerate((row.sig, row.op, row.val), start=1):
            self.table.setCellWidget(r, col, w)
        self.table.setRowHeight(r, max(self.table.rowHeight(r), row.sig.sizeHint().height() + 6))

    def add_row(self):
        self._append()
        self.table.setCurrentCell(self.table.rowCount() - 1, 0)
        self.table.editItem(self.table.item(self.table.rowCount() - 1, 0))

    def remove_row(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)
            self.rows.pop(r).deleteLater()
            self.on_change()

    def items(self) -> dict:
        out = {}
        for r, row in enumerate(self.rows):
            label = (self.table.item(r, 0).text() if self.table.item(r, 0) else "").strip()
            cond = row.expression()
            if label or cond:
                out[label] = cond
        return out


def _spin(minimum=0.0, maximum=1e6, decimals=2, step=0.1):
    s = QDoubleSpinBox()
    s.setRange(minimum, maximum)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setMinimumWidth(80)
    return s


def _form(parent=None) -> QFormLayout:
    """Form whose fields stretch to the full width on every platform (macOS keeps them narrow by default)."""
    form = QFormLayout(parent) if parent is not None else QFormLayout()
    form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.DontWrapRows)
    form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
    form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
    form.setHorizontalSpacing(10)
    form.setVerticalSpacing(4)
    return form


# ============================================================================ event form
class EventForm(QWidget):
    def __init__(self, page: "KpiPage"):
        super().__init__()
        self.page = page
        self.state = page.state
        self.name: str | None = None
        self._loading = False
        names = self.state.signal_names
        enums = self.state.enum_map

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 12, 0)
        form = _form()
        self.name_edit = QLineEdit()
        self.desc = QLineEdit()
        self.mode = QComboBox()
        self.mode.addItems(["Condition (start / end expressions)", "Preset from the toolbox"])
        self.start = ExprEdit(names, placeholder="e.g. aebRequest in (FULL, PARTIAL)", enums_fn=enums,
                              build_fn=lambda e: self.build(e, "start"))
        self.end = ExprEdit(names, placeholder="optional; default: until the start condition is false",
                            enums_fn=enums, build_fn=lambda e: self.build(e, "end"))
        sb, eb = QPushButton("Edit condition"), QPushButton("Edit condition")
        self.build_buttons = (sb, eb)
        for b in (sb, eb):
            b.setToolTip("Edit the condition as rows: signal, comparison, value")
            b.setMinimumWidth(b.sizeHint().width())  # the label is never cut off
        srow, erow = QHBoxLayout(), QHBoxLayout()
        self.start_rule, self.end_rule = QComboBox(), QComboBox()
        for combo, which in ((self.start_rule, "start"), (self.end_rule, "end")):
            combo.addItems(["first", "last"])
            combo.setItemData(0, f"The window {which}s the first time the condition is met"
                              + (" (when it becomes true)" if which == "start" else " after the start"),
                              Qt.ToolTipRole)
            combo.setItemData(1, "The window starts the last time the condition becomes true before the end"
                              if which == "start" else
                              "The window ends the last time the condition is met before the next start "
                              "(or the end of the file)", Qt.ToolTipRole)
            combo.setToolTip(f"Rule: which time the condition is met the window {which}s at. "
                             "Every window in the file is found, each with this rule.")
        srow.addWidget(self.start_rule)
        erow.addWidget(self.end_rule)
        srow.addWidget(self.start, 1)
        srow.addWidget(sb)
        erow.addWidget(self.end, 1)
        erow.addWidget(eb)
        self.preset = QComboBox()
        self.preset.addItems(toolbox.names("event"))
        self.preset_inputs = QLineEdit()
        self.preset_inputs.setPlaceholderText("signal names, comma separated")
        self.preset_params = QLineEdit()
        self.preset_params.setPlaceholderText("e.g. merge_window: 2.0")
        self.min_dur, self.merge = _spin(), _spin()
        self.pre, self.post = _spin(), _spin()
        form.addRow("Name", self.name_edit)
        form.addRow("Description", self.desc)
        # presets are kept only for profiles that already use one: a new event is a start / end condition
        self.mode_label = QLabel("Trigger")
        form.addRow(self.mode_label, self.mode)
        self.start_label, self.end_label = QLabel("Start when"), QLabel("End when")
        form.addRow(self.start_label, srow)
        form.addRow(self.end_label, erow)
        self.preset_label, self.inputs_label, self.params_label = QLabel("Preset"), QLabel("Inputs"), QLabel("Options")
        form.addRow(self.preset_label, self.preset)
        form.addRow(self.inputs_label, self.preset_inputs)
        form.addRow(self.params_label, self.preset_params)
        def pairs(*items):
            """Label + spin box pairs that wrap onto a second line when the panel is narrow."""
            box = QWidget()
            policy = box.sizePolicy()
            policy.setHeightForWidth(True)
            box.setSizePolicy(policy)
            flow = FlowLayout(box, spacing=12)
            for label, w in items:
                pair = QWidget()
                pl = QHBoxLayout(pair)
                pl.setContentsMargins(0, 0, 0, 0)
                pl.setSpacing(8)
                pl.addWidget(QLabel(label))
                pl.addWidget(w)
                w.setSuffix(" s")
                flow.addWidget(pair)
            return box

        form.addRow("Occurrences", pairs(("ignore shorter than", self.min_dur), ("merge gaps below", self.merge)))
        form.addRow("Window", pairs(("before start", self.pre), ("after end", self.post)))
        lay.addLayout(form)

        box = QGroupBox()
        box.setObjectName("plainBox")  # no title strip: the title row inside carries the buttons
        bl = QVBoxLayout(box)
        mrow = QHBoxLayout()  # title with + / − in the top right corner
        mtitle = QLabel("Moments: the exact instants KPIs are read at (start, end, window_start, window_end "
                        "are built in)")
        mtitle.setObjectName("sectionTitle")
        mtitle.setWordWrap(True)  # wraps in a narrow panel instead of widening the form
        mtitle.setMinimumWidth(120)
        mrow.addWidget(mtitle, 1)
        for b in context_menu.plus_minus(add=self.add_moment, remove=self.delete_moment, add_tip="Add a moment",
                                         remove_tip="Delete the selected moments"):
            mrow.addWidget(b)
        bl.addLayout(mrow)
        self.moments = QTableWidget(0, 4)
        self.moments.setHorizontalHeaderLabels(["Name", "Rule", "Expression / target moment", "Options"])
        mh = self.moments.horizontalHeader()
        mh.setSectionResizeMode(2, QHeaderView.Stretch)  # the expression takes the free width
        for col, width in ((1, 145), (3, 170)):  # Rule and Options: set widths (still draggable)
            mh.setSectionResizeMode(col, QHeaderView.Fixed)
            mh.resizeSection(col, width)
        bl.addWidget(self.moments)
        hint = QLabel("Options: after: start, before: end, within: 1.5, between: [start, end], hold: 0.2, "
                      "plus the rule's own options (e.g. threshold: -20). Offset rule: target moment + 'by: -0.5'.")
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        bl.addWidget(hint)
        # Moments and Ranges share a splitter: drag the divider between them to give either more height
        self.tables_split = QSplitter(Qt.Vertical)
        self.tables_split.setChildrenCollapsible(False)
        self.tables_split.setHandleWidth(10)
        self.tables_split.setMinimumHeight(380)
        self.tables_split.addWidget(box)
        lay.addWidget(self.tables_split, 1)

        # named ranges: from one moment to another; KPIs reduce over them
        rbox = QGroupBox()
        rbox.setObjectName("plainBox")
        rl = QVBoxLayout(rbox)
        rrow = QHBoxLayout()
        rtitle = QLabel("Ranges: spans from one moment to another that KPIs are taken over (event = start to end, "
                        "window = window_start to window_end are built in)")
        rtitle.setObjectName("sectionTitle")
        rtitle.setWordWrap(True)
        rtitle.setMinimumWidth(120)
        rrow.addWidget(rtitle, 1)
        for b in context_menu.plus_minus(add=self.add_range, remove=self.delete_range, add_tip="Add a range",
                                         remove_tip="Delete the selected ranges"):
            rrow.addWidget(b)
        rl.addLayout(rrow)
        self.ranges = QTableWidget(0, 3)
        self.ranges.setHorizontalHeaderLabels(["Name", "From moment", "To moment"])
        rh = self.ranges.horizontalHeader()
        for col in range(3):
            rh.setSectionResizeMode(col, QHeaderView.Stretch)
        rl.addWidget(self.ranges)
        self.tables_split.addWidget(rbox)
        self.tables_split.setStretchFactor(0, 1)  # Moments and Ranges start at the same height
        self.tables_split.setStretchFactor(1, 1)
        self.tables_split.setSizes([1000, 1000])
        for b in (box, rbox):
            b.setMinimumHeight(120)

        self.name_edit.editingFinished.connect(self.rename)
        self.desc.editingFinished.connect(lambda: self.set("description", self.desc.text()))
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.start.editingFinished.connect(lambda: self.set("start", self.start.text().strip()))
        self.end.editingFinished.connect(lambda: self.set("end", self.end.text().strip()))
        self.end.textChanged.connect(self._end_rule_enabled)
        self.start_rule.currentTextChanged.connect(lambda t: self.set_rule("start_rule", t))
        self.end_rule.currentTextChanged.connect(lambda t: self.set_rule("end_rule", t))
        sb.clicked.connect(lambda: self.build(self.start, "start"))
        eb.clicked.connect(lambda: self.build(self.end, "end"))
        self.preset.currentTextChanged.connect(lambda *_: self.save_preset())
        self.preset_inputs.editingFinished.connect(self.save_preset)
        self.preset_params.editingFinished.connect(self.save_preset)
        for key, w in (("min_duration", self.min_dur), ("merge_gap", self.merge)):
            w.valueChanged.connect(lambda v, k=key: self.set(k, v))
        self.pre.valueChanged.connect(lambda v: self.set_window("pre", v))
        self.post.valueChanged.connect(lambda v: self.set_window("post", v))
        # right-click: on a moment, Edit condition / Delete moment; Add moment anywhere
        context_menu.install(self.moments, add=[("Add moment", self.add_moment)], item=self._moment_actions)
        self.moments.itemChanged.connect(self.moment_renamed)
        context_menu.install(self.ranges, add=[("Add range", self.add_range)],
                             item=lambda rows: [("Delete range" if len(rows) == 1 else f"Delete {len(rows)} ranges",
                                                 self.delete_range)])
        self.ranges.itemChanged.connect(self.range_renamed)
        for w in (self.start, self.end):
            w.focused.connect(page.set_target)

    # ---- data helpers
    @property
    def ev(self) -> dict:
        return self.state.data["events"][self.name]

    def set(self, key, value):
        if self._loading or self.name is None:
            return
        if self.ev.get(key) != value:
            self.ev[key] = value
            self.state.edited()

    def set_rule(self, key, value):
        """start_rule / end_rule: 'first' is the default and is left out of the profile."""
        if self._loading or self.name is None:
            return
        if value == "first":
            if self.ev.pop(key, None) is not None:
                self.state.edited()
        elif self.ev.get(key) != value:
            self.ev[key] = value
            self.state.edited()

    def _end_rule_enabled(self, *_):
        """'last' needs an end condition: without one the window ends when the start condition is false."""
        has_end = bool(self.end.text().strip())
        self.end_rule.setEnabled(has_end)
        if not has_end and self.end_rule.currentText() != "first":
            self.end_rule.setCurrentText("first")

    def set_window(self, key, value):
        if self._loading or self.name is None:
            return
        self.ev.setdefault("window", {})[key] = value
        self.state.edited()

    def load(self, name: str):
        self._loading = True
        self.name = name
        ev = self.ev
        self.name_edit.setText(name)
        self.desc.setText(ev.get("description", ""))
        preset = ev.get("preset")
        self.mode.setCurrentIndex(1 if preset else 0)
        self.start.setText(ev.get("start") or "")
        self.end.setText(ev.get("end") or "")
        self.start_rule.setCurrentText(ev.get("start_rule") or "first")
        self.end_rule.setCurrentText(ev.get("end_rule") or "first")
        self._end_rule_enabled()
        if preset:
            self.preset.setCurrentText(preset.get("name", ""))
            of = preset.get("of", [])
            self.preset_inputs.setText(of if isinstance(of, str) else ", ".join(of))
            self.preset_params.setText(format_options({k: v for k, v in preset.items() if k not in ("name", "of")}))
        else:
            self.preset_inputs.clear()
            self.preset_params.clear()
        self.min_dur.setValue(float(ev.get("min_duration", 0) or 0))
        self.merge.setValue(float(ev.get("merge_gap", 0) or 0))
        w = ev.get("window", {}) or {}
        self.pre.setValue(float(w.get("pre", 0) or 0))
        self.post.setValue(float(w.get("post", 0) or 0))
        self.load_moments()
        self.load_ranges()
        for w in (self.mode_label, self.mode):
            w.setVisible(bool(preset))
        self._show_mode()
        self._loading = False

    def _show_mode(self):
        preset = self.mode.currentIndex() == 1
        for w in (self.start, self.end, self.start_label, self.end_label, *self.build_buttons, self.start_rule,
                  self.end_rule):
            w.setVisible(not preset)
        for w in (self.preset, self.preset_inputs, self.preset_params, self.preset_label, self.inputs_label,
                  self.params_label):
            w.setVisible(preset)

    def mode_changed(self):
        self._show_mode()
        if self._loading:
            return
        if self.mode.currentIndex() == 1:
            for key in ("start", "end", "start_rule", "end_rule"):
                self.ev.pop(key, None)
            self.save_preset()
        else:
            self.ev.pop("preset", None)
            self.ev["start"] = self.start.text().strip()
            self.state.edited()

    def save_preset(self):
        if self._loading or self.name is None or self.mode.currentIndex() != 1:
            return
        inputs = [s.strip() for s in self.preset_inputs.text().split(",") if s.strip()]
        try:
            params = parse_options(self.preset_params.text())
        except Exception as exc:  # noqa: BLE001
            self.preset_params.setToolTip(str(exc))
            return
        self.ev["preset"] = {"name": self.preset.currentText(), "of": inputs[0] if len(inputs) == 1 else inputs,
                             **params}
        self.state.edited()

    def build(self, edit: ExprEdit, key: str):
        dlg = ConditionBuilder(self.state.signal_names(), self, initial=edit.text(), enums=self.state.enum_map())
        if dlg.exec() == QDialog.Accepted and dlg.expression():
            edit.setText(dlg.expression())
            self.set(key, dlg.expression())

    def rename(self):
        new = self.name_edit.text().strip()
        if self.name is None or new == self.name:
            return
        events = self.state.data["events"]
        if not NAME_RE.match(new) or new in events:
            QMessageBox.warning(self, "Rename event", f"'{new}' is not a valid, unused name.")
            self.name_edit.setText(self.name)
            return
        events[new] = events.pop(self.name)
        for k in self.state.data.get("kpis", []):
            if k.get("event") == self.name:
                k["event"] = new
        for p in self.state.data.get("plots", []):
            if p.get("event") == self.name:
                p["event"] = new
        self.name = new
        self.state.edited(structure=True)
        self.page.select_event(new)

    # ---- moments
    def load_moments(self):
        moments = self.ev.get("moments", {}) or {}
        tools = toolbox.names("moment")
        known = toolbox.names("moment", include_hidden=True)  # an older profile may use one no longer offered
        self.moments.blockSignals(True)
        self.moments.setRowCount(len(moments))
        for r, (mname, raw) in enumerate(moments.items()):
            raw = dict(raw or {})
            if "offset" in raw:
                rule, target = "offset", raw.pop("offset")
            else:
                rule = next((k for k in raw if k in known), tools[0])
                target = raw.pop(rule, "")
            name_item = QTableWidgetItem(mname)
            name_item.setData(Qt.UserRole, mname)
            self.moments.setItem(r, 0, name_item)
            combo = QComboBox()
            rules = tools + ([rule] if rule not in tools and rule != "offset" else []) + ["offset"]
            combo.addItems(rules)
            for i, t in enumerate(rules):
                combo.setItemData(i, self.rule_help(t), Qt.ToolTipRole)
            combo.setCurrentText(rule)
            combo.setToolTip(self.rule_help(rule))
            combo.currentTextChanged.connect(lambda t, c=combo: c.setToolTip(self.rule_help(t)))
            combo.currentTextChanged.connect(lambda t, row=r: self.fill_rule_options(row, t))
            combo.currentTextChanged.connect(lambda *_, row=r: self.save_moment(row))
            self.moments.setCellWidget(r, 1, combo)
            expr = ExprEdit(self.state.signal_names, enums_fn=self.state.enum_map, units_fn=self.state.unit_map,
                            extras_fn=lambda m=mname: {"Moments": self.moment_names(exclude=m)},
                            build_fn=lambda e, row=r: self.build_moment_row(row))
            expr.setPlaceholderText("▾ pick a signal, or type a condition")
            expr.setText(str(target))
            expr.focused.connect(self.page.set_target)
            expr.editingFinished.connect(lambda row=r: self.save_moment(row))
            self.moments.setCellWidget(r, 2, expr)
            opts = QLineEdit(format_options(raw))
            opts.editingFinished.connect(lambda row=r: self.save_moment(row))
            self.moments.setCellWidget(r, 3, opts)
        self.moments.blockSignals(False)

    def save_moment(self, row: int):
        if self._loading:
            return
        mname = self.moments.item(row, 0).data(Qt.UserRole)
        rule = self.moments.cellWidget(row, 1).currentText()
        target = self.moments.cellWidget(row, 2).text().strip()
        opts_edit = self.moments.cellWidget(row, 3)
        try:
            opts = parse_options(opts_edit.text())
            opts_edit.setStyleSheet("")
        except Exception as exc:  # noqa: BLE001
            opts_edit.setStyleSheet("QLineEdit { border: 1px solid #e34948; }")
            opts_edit.setToolTip(f"Could not read options: {exc}")
            return
        self.ev.setdefault("moments", {})[mname] = {rule: target, **opts}
        self.state.edited()

    def moment_renamed(self, item):
        if item.column() != 0:
            return
        old, new = item.data(Qt.UserRole), item.text().strip()
        moments = self.ev.setdefault("moments", {})
        if new == old:
            return
        if not NAME_RE.match(new) or new in moments or new in BUILTIN_MOMENTS:
            item.setText(old)
            return
        # keep the order, and point other moments and this event's KPIs at the new name
        self.ev["moments"] = {(new if k == old else k): v for k, v in moments.items()}
        rename_moment(self.state.data, self.name, old, new)
        self.state.edited(structure=True)
        self.load_moments()

    def add_moment(self):
        moments = self.ev.setdefault("moments", {})
        i = len(moments) + 1
        while f"moment{i}" in moments:
            i += 1
        moments[f"moment{i}"] = {"first": "", "after": "start"}
        self.state.edited(structure=True)
        self.load_moments()

    CONDITION_RULES = ("first", "last", "last_segment_start")

    @staticmethod
    def rule_help(rule: str) -> str:
        if rule == "offset":
            return "offset: another moment shifted in time (target moment, options 'by: -0.5')"
        try:
            spec = toolbox.get("moment", rule)
        except Exception:  # noqa: BLE001
            return ""
        return f"{spec.signature}\n{spec.summary}"

    def moment_names(self, exclude=None) -> list[str]:
        own = [m for m in (self.ev.get("moments", {}) or {}) if m != exclude] if self.name else []
        return list(BUILTIN_MOMENTS) + own

    def fill_rule_options(self, row: int, rule: str):
        """Switching rule: add the new rule's required options (e.g. 'value: 3.0') if missing."""
        if self._loading or rule == "offset":
            return
        try:
            spec = toolbox.get("moment", rule)
        except Exception:  # noqa: BLE001
            return
        edit = self.moments.cellWidget(row, 3)
        try:
            opts = parse_options(edit.text())
        except Exception:  # noqa: BLE001
            return
        known = {p.name for p in spec.params}
        stale = [k for k in opts if k not in known and k not in ("after", "before", "within", "between",
                                                                  "hold", "by")]
        for k in stale:
            opts.pop(k)
        for p in spec.params:
            if p.required and p.name not in opts:
                opts[p.name] = p.example_value()
        edit.setText(format_options(opts))
        self.save_moment(row)

    def _moment_actions(self, rows):
        actions = []
        if len(rows) == 1:
            row = next(iter(rows))
            actions.append(("Edit condition", lambda: self.build_moment_row(row)))
        actions.append(("Delete moment" if len(rows) == 1 else f"Delete {len(rows)} moments", self.delete_moment))
        return actions

    def build_moment_row(self, row: int):
        dlg = ConditionBuilder(self.state.signal_names(), self, initial=self.moments.cellWidget(row, 2).text(),
                               enums=self.state.enum_map())
        if dlg.exec() == QDialog.Accepted and dlg.expression():
            self.apply_moment_condition(row, dlg.expression())

    def apply_moment_condition(self, row, expression: str):
        """Put `expression` in moment `row` (a new moment when None), using a condition rule."""
        if row is None:
            self.add_moment()
            row = self.moments.rowCount() - 1
        combo = self.moments.cellWidget(row, 1)
        self.moments.cellWidget(row, 2).setText(expression)
        if combo.currentText() not in self.CONDITION_RULES:
            combo.setCurrentText("first")  # saves, and drops the old rule's options
        else:
            self.save_moment(row)

    # ---- ranges
    def load_ranges(self):
        ranges = self.ev.get("ranges", {}) or {}
        moments = self.moment_names()
        self.ranges.blockSignals(True)
        self.ranges.setRowCount(len(ranges))
        for r, (rname, pair) in enumerate(ranges.items()):
            item = QTableWidgetItem(rname)
            item.setData(Qt.UserRole, rname)
            self.ranges.setItem(r, 0, item)
            pair = list(pair or []) + ["start", "end"][len(pair or []):]
            for col, value in ((1, pair[0]), (2, pair[1])):
                combo = QComboBox()
                combo.addItems(moments)
                KpiForm._set_moment(combo, str(value))
                combo.currentTextChanged.connect(lambda t, c=combo: KpiForm._set_moment(c, t))
                combo.currentTextChanged.connect(lambda *_, row=r: self.save_range(row))
                self.ranges.setCellWidget(r, col, combo)
        self.ranges.blockSignals(False)

    def save_range(self, row: int):
        if self._loading or self.name is None:
            return
        rname = self.ranges.item(row, 0).data(Qt.UserRole)
        pair = [self.ranges.cellWidget(row, 1).currentText(), self.ranges.cellWidget(row, 2).currentText()]
        ranges = self.ev.setdefault("ranges", {})
        if ranges.get(rname) != pair:
            ranges[rname] = pair
            self.state.edited()

    def range_renamed(self, item):
        if item.column() != 0:
            return
        old, new = item.data(Qt.UserRole), item.text().strip()
        ranges = self.ev.setdefault("ranges", {})
        if new == old:
            return
        if not NAME_RE.match(new) or new in ranges or new in BUILTIN_RANGES:
            self.ranges.blockSignals(True)
            item.setText(old)
            self.ranges.blockSignals(False)
            return
        self.ev["ranges"] = {(new if k == old else k): v for k, v in ranges.items()}
        rename_range(self.state.data, self.name, old, new)
        self.state.edited(structure=True)
        self.load_ranges()

    def add_range(self):
        if self.name is None:
            return
        ranges = self.ev.setdefault("ranges", {})
        i = len(ranges) + 1
        while f"range{i}" in ranges:
            i += 1
        ranges[f"range{i}"] = ["start", "end"]
        self.state.edited(structure=True)
        self.load_ranges()

    def delete_range(self):
        rows = sorted({i.row() for i in self.ranges.selectedIndexes()}, reverse=True)
        ranges = self.ev.get("ranges", {})
        for r in rows:
            ranges.pop(self.ranges.item(r, 0).data(Qt.UserRole), None)
        if rows:
            self.state.edited(structure=True)
            self.load_ranges()

    def delete_moment(self):
        rows = sorted({i.row() for i in self.moments.selectedIndexes()}, reverse=True)
        moments = self.ev.get("moments", {})
        for r in rows:
            moments.pop(self.moments.item(r, 0).data(Qt.UserRole), None)
        if rows:
            self.state.edited(structure=True)
            self.load_moments()


# ============================================================================ KPI form
class FormulaField(QWidget):
    """A formula of earlier KPIs, built from rows (KPI, operator, value, joined with and / or / xor, with
    brackets), e.g. a pass / fail rule. Arithmetic such as  dist / time  is typed instead."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.names: list[str] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.typed = QCheckBox("Type the formula instead (for arithmetic, e.g. braking_dist / braking_time)")
        self.typed.toggled.connect(self._mode_changed)
        self.line = QLineEdit()
        self.line.setPlaceholderText("e.g. braking_dist / braking_time")
        self.line.editingFinished.connect(self.changed.emit)
        self.rows: Optional[ConditionBuilder] = None
        self.rows_box = QVBoxLayout()
        lay.addLayout(self.rows_box)
        lay.addWidget(self.line)
        lay.addWidget(self.typed)
        self._make_rows("")
        self._mode_changed(False)

    def _make_rows(self, text: str):
        if self.rows is not None:
            self.rows.setParent(None)
            self.rows.deleteLater()
        self.rows = ConditionBuilder(self.names, self, embedded=True)
        self.rows.findChild(QLabel).setText(
            "Each row is  KPI  operator  value, e.g. speed_at_release > 0.5. Join rows with and / or / xor and "
            "group them with brackets. The result is 1 when true, 0 when false.")
        if text:
            self.rows.load_expression(text)
        self.rows.changed.connect(self.changed.emit)
        self.rows_box.addWidget(self.rows)

    def _mode_changed(self, typed: bool):
        if typed and not self.line.text().strip():
            self.line.setText(self.rows.expression())
        self.rows.setVisible(not typed)
        self.line.setVisible(typed)
        if not typed and self.line.text().strip():
            self._make_rows(self.line.text().strip())
        self.changed.emit()

    def set_formula(self, text: str, names: list[str]):
        """Show `text` as rows when it fits them, else typed; `names` are the KPIs it may use."""
        self.blockSignals(True)
        self.names = list(names)
        rows = not text.strip() or parse_condition_rows(text) is not None
        self.line.setText(text)
        self._make_rows(text if rows else "")
        self.typed.setChecked(not rows)
        self.rows.setVisible(rows)
        self.line.setVisible(not rows)
        self.blockSignals(False)

    def text(self) -> str:
        return self.line.text().strip() if self.typed.isChecked() else self.rows.expression()


class KpiForm(QWidget):
    def __init__(self, page: "KpiPage"):
        super().__init__()
        self.page = page
        self.state = page.state
        self.index: int | None = None
        self._loading = False

        lay = _form(self)
        lay.setContentsMargins(0, 0, 12, 0)  # flush top and bottom; room for the scroll bar on the right
        self.name_edit = QLineEdit()
        self.event = QComboBox()
        self.form = QComboBox()
        for key, label in FORMS:
            self.form.addItem(label, key)
        self.expr = ExprEdit(self.state.signal_names, placeholder="expression, e.g. egoSpeedKph",
                             enums_fn=self.state.enum_map, units_fn=self.state.unit_map)
        self.formula = FormulaField()
        self.at = QComboBox()
        self.range = QComboBox()
        self.range.setProperty("kind", "range")
        self.range.setProperty("tip", "The range of the event to reduce over: 'event' is start to end, 'window' is "
                              "window_start to window_end; add your own under Ranges on the event")
        self.reducer = QComboBox()
        for name in toolbox.names("reducer"):
            self.reducer.addItem(name, name)
        for key, label in SPECIAL_REDUCERS:
            self.reducer.addItem(label, key)
        self.args = QLineEdit()
        self.args.setPlaceholderText("reducer options, if any")
        self.which = WhichTable(self.state, self.save)
        self.unit = QLineEdit()  # shows the unit taken from the signal; typed for formulas and unit-less signals
        self.unit.setToolTip("Taken from the signal the KPI reads (a KPI is one value of that signal). "
                             "When the signal has no unit, and for a formula, type one here.")
        self.desc = QLineEdit()
        self.rows = {}
        for label, w in (("Name", self.name_edit), ("Event", self.event), ("Kind", self.form),
                         ("Range", self.range), ("Reducer", self.reducer), ("Expression", self.expr),
                         ("Formula", self.formula), ("Conditions", self.which),
                         ("At moment", self.at), ("Options", self.args), ("Unit", self.unit),
                         ("Description", self.desc)):
            lab = QLabel(label)
            lay.addRow(lab, w)
            self.rows[label] = (lab, w)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addRow(self.status)

        self.name_edit.editingFinished.connect(self.rename)
        self.event.currentTextChanged.connect(self.event_changed)
        self.form.currentIndexChanged.connect(self.form_changed)
        self.expr.editingFinished.connect(self.save)
        self.formula.changed.connect(self.save)
        for w in (self.at, self.range):  # a valid choice clears the "missing" mark
            w.currentTextChanged.connect(lambda _t, w=w: self._set_moment(w, w.currentText()))
        self.reducer.currentIndexChanged.connect(lambda *_: self._show_form(self.form.currentData()))
        for w in (self.at, self.range, self.reducer):
            w.currentTextChanged.connect(lambda *_: self.save())
        for w in (self.args, self.unit, self.desc):
            w.editingFinished.connect(self.save)
        self.expr.focused.connect(page.set_target)
        self._show_form("value")  # until a KPI is loaded: no hidden form's rows widen the panel

    @property
    def k(self) -> dict:
        return self.state.data["kpis"][self.index]

    def earlier_kpis(self, index: int, event: str) -> list[str]:
        """The KPIs a formula may use: those listed before it for the same event."""
        return [k.get("name", "") for k in self.state.data["kpis"][:index] if k.get("event") == event and k.get("name")]

    def _fill_moments(self, event: str):
        for combo, items in ((self.at, self.state.moments_of(event)), (self.range, self.state.ranges_of(event))):
            cur = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(items)
            if cur in items:
                combo.setCurrentText(cur)
            combo.blockSignals(False)

    @staticmethod
    def _set_moment(combo: QComboBox, value: str):
        """Select `value`; a moment (or range) the event no longer has is shown in red at the top, so it can be
        replaced."""
        combo.blockSignals(True)
        what = "range" if combo.property("kind") == "range" else "moment"
        if value and combo.findText(value) < 0:
            combo.insertItem(0, value)
            combo.setItemData(0, QColor("#e34948"), Qt.ForegroundRole)
            combo.setItemData(0, f"'{value}' is not a {what} of this event: pick one from the list", Qt.ToolTipRole)
        combo.setCurrentText(value)
        missing = combo.currentIndex() == 0 and combo.itemData(0, Qt.ForegroundRole) is not None
        combo.setStyleSheet("QComboBox { color: #e34948; border: 1px solid #e34948; }" if missing else "")
        combo.setToolTip(f"'{value}' is not a {what} of this event: pick one from the list" if missing
                         else combo.property("tip") or "")
        combo.blockSignals(False)

    def load(self, index: int):
        self._loading = True
        self.index = index
        k = self.k
        if k.get("over") is None and (k.get("between") or k.get("duration")):
            use_named_ranges(self.state.data)  # e.g. pasted in: give its moments a named range
            k = self.k
        self.name_edit.setText(k.get("name", ""))
        self.event.blockSignals(True)
        self.event.clear()
        self.event.addItems(self.state.event_names())
        self.event.setCurrentText(k.get("event", ""))
        self.event.blockSignals(False)
        self._fill_moments(k.get("event", ""))
        form = "value" if "value" in k else "formula" if "formula" in k else "range"
        self.form.setCurrentIndex([f for f, _ in FORMS].index(form))
        self.expr.setText(k.get("value") or k.get("of") or "")
        self.formula.set_formula(k.get("formula", ""), self.earlier_kpis(index, k.get("event", "")))
        self._set_moment(self.at, k.get("at", "start"))
        self._set_moment(self.range, k.get("over") or ("window" if k.get("reduce") else "event"))
        self.which.set_items(k.get("which") or {})
        reducer = "which" if "which" in k else k.get("reduce", "max")
        self.reducer.setCurrentIndex(max(0, self.reducer.findData(reducer)))
        self.args.setText(format_options(k.get("args", {})))
        self.update_unit(k)
        self.desc.setText(k.get("description", "") or "")
        self._show_form(form)
        self._loading = False
        self.show_status()

    def _show_form(self, form):
        reducer = self.reducer.currentData()
        if form == "range":
            visible = {"Range", "Reducer"} | ({"Conditions"} if reducer == "which" else set() if reducer == "duration"
                                              else {"Expression", "Options"})
        else:
            visible = {"value": {"Expression", "At moment"}, "formula": {"Formula"}}[form]
        for label in ("Range", "Reducer", "Expression", "Formula", "Conditions", "At moment", "Options"):
            lab, w = self.rows[label]
            lab.setVisible(label in visible)
            w.setVisible(label in visible)

    def form_changed(self):
        form = self.form.currentData()
        if form == "range" and not self._loading and self.index is not None and self.k.get("over") is None:
            self._set_moment(self.range, "event")  # the event itself, start to end
        self._show_form(form)
        self.save()

    def event_changed(self, ev):
        self._fill_moments(ev)
        self.save()
        if self.index is not None:
            self.formula.set_formula(self.formula.text(), self.earlier_kpis(self.index, ev))

    def save(self):
        if self._loading or self.index is None:
            return
        form = self.form.currentData()
        name = self.k.get("name")
        k = {"name": name, "event": self.event.currentText()}
        if form == "value":
            k.update(value=self.expr.text().strip(), at=self.at.currentText())
        elif form == "range":
            reducer = self.reducer.currentData()
            if reducer == "which":
                k["which"] = self.which.items()
            elif reducer == "duration":
                k["reduce"] = "duration"
            else:
                k.update(reduce=reducer, of=self.expr.text().strip())
                try:
                    args = parse_options(self.args.text())
                    if args:
                        k["args"] = args
                except Exception:  # noqa: BLE001
                    pass
            k["over"] = self.range.currentText()
        else:
            k.update(formula=self.formula.text().strip())
        if form == "formula":
            if self.unit.text().strip():
                k["unit"] = plain_unit(self.unit.text().strip())
        elif not self.unit.isReadOnly():  # the signal has no unit: the one typed in
            if self.unit.text().strip():
                k["unit"] = plain_unit(self.unit.text().strip())
        elif self.k.get("unit"):
            k["unit"] = self.k["unit"]  # kept as a fallback when the signal has no unit
        if self.desc.text().strip():
            k["description"] = self.desc.text().strip()
        if k != self.k:
            self.state.data["kpis"][self.index] = k
            self.state.edited()
            self.page.refresh_kpi_labels()
        self.update_unit(k)

    def update_unit(self, k: dict):
        """Show the unit the KPI gets from its signal; typed in for formulas and when the signal has none."""
        formula = k.get("formula") is not None
        if formula:
            self._unit_editable(True, k.get("unit", "") or "", "unit of the formula (optional)")
            return
        if k.get("which") is not None:
            self._unit_editable(False, "", "none: the result is text")
            return
        units = signal_units(self.state.resolved) if self.state.resolved is not None else {}
        try:
            inferred = kpi_unit(KpiDef.model_validate({kk: v for kk, v in k.items()
                                                       if v is not None and kk != "unit"}), units)
        except Exception:  # noqa: BLE001 - incomplete KPI
            inferred = ""
        if inferred:
            self._unit_editable(False, inferred, "")
        else:
            self._unit_editable(True, k.get("unit", "") or "", "the signal has no unit: type one (optional)")

    def _unit_editable(self, editable: bool, text: str, placeholder: str):
        self.unit.setReadOnly(not editable)
        self.unit.setStyleSheet("" if editable else "QLineEdit { background: transparent; border: none; }")
        if not self.unit.hasFocus():
            self.unit.setText(pretty_unit(text))
        self.unit.setPlaceholderText(placeholder)

    def rename(self):
        new = self.name_edit.text().strip()
        if self.index is None or new == self.k.get("name"):
            return
        taken = {k.get("name") for i, k in enumerate(self.state.data["kpis"]) if i != self.index}
        if not NAME_RE.match(new) or new in taken:
            QMessageBox.warning(self, "Rename KPI", f"'{new}' is not a valid, unused KPI name.")
            self.name_edit.setText(self.k.get("name", ""))
            return
        old = self.k.get("name")
        self.k["name"] = new
        rename_kpi(self.state.data, self.k.get("event"), old, new)  # formulas and plots
        self.state.edited()
        self.page.refresh_kpi_labels()

    def show_status(self):
        if self.index is None:
            return
        name = self.k.get("name", "")
        self.update_unit(self.k)
        msgs = [str(i) for i in self.state.issues if f"kpis.{name}" in i.where]
        self.status.setText("<span style='color:#e34948'>" + "<br>".join(msgs) + "</span>" if msgs else "")


class KpiPage(QWidget):
    def __init__(self, state: AppState, parent=None, viewer=None):
        super().__init__(parent)
        self.state = state
        self.viewer = viewer  # the shared signal viewer / preview below the tabs
        self.target: ExprEdit | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(*PAGE_MARGINS)
        top = QSplitter(Qt.Horizontal)
        outer.addWidget(top)

        # ---- left lists
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(8)
        # compact headers: the list title with its buttons on one line, so the lists keep their height
        def header(title, buttons):
            row = QHBoxLayout()
            lab = QLabel(title)
            lab.setObjectName("sectionTitle")
            row.addWidget(lab)
            row.addStretch(1)
            for b in buttons:
                row.addWidget(b)
            return row

        ll.addLayout(header("Events", context_menu.plus_minus(
            add=self.new_event, remove=self.delete_event, add_tip="Add an event",
            remove_tip="Delete the selected events")))
        self.events = QListWidget()
        self.events.setMinimumHeight(52)
        ll.addWidget(self.events, 1)
        self.up, self.down = QPushButton("↑"), QPushButton("↓")
        for b, tip in ((self.up, "Move up"), (self.down, "Move down")):
            b.setToolTip(tip)
            b.setFixedSize(30, 26)
            b.setStyleSheet("QPushButton { padding: 0; font-size: 15px; }")
        krow = header("KPIs", context_menu.plus_minus(add=self.new_kpi, remove=self.delete_kpi, add_tip="Add a KPI",
                                                      remove_tip="Delete the selected KPIs") + [self.up, self.down])
        ll.addLayout(krow)
        self.kpis = QListWidget()
        self.kpis.setMinimumHeight(60)
        ll.addWidget(self.kpis, 2)
        top.addWidget(left)

        # ---- center forms
        self.stack = QStackedWidget()
        self.empty = QLabel("Select or add an event or a KPI.\n\nAn event says WHEN something happens; its moments say "
                            "the exact instants inside the window; a KPI reads a value at a moment, a time between "
                            "moments, or a reduction over a range.")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        self.event_form = EventForm(self)
        self.kpi_form = KpiForm(self)
        self.lib_note = QLabel()
        self.lib_note.setWordWrap(True)
        self.lib_note.setAlignment(Qt.AlignTop)
        for w in (self.empty, self.event_form, self.kpi_form, self.lib_note):
            self.stack.addWidget(w)
        # the forms scroll when the tab is short, instead of squeezing their fields
        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setFrameShape(QFrame.NoFrame)
        self.form_scroll.setWidget(self.stack)
        # size the scroll area to the shown form only (a stacked widget otherwise takes its largest page)
        def fit_current(_i):
            for w in (self.empty, self.event_form, self.kpi_form, self.lib_note):
                p = QSizePolicy.Preferred if w is self.stack.currentWidget() else QSizePolicy.Ignored
                w.setSizePolicy(p, p)  # a hidden form's wide rows (e.g. a formula's) don't widen it either
            self.stack.adjustSize()
        self.stack.currentChanged.connect(fit_current)
        # in a narrow panel the fields get narrower; buttons beside them (Edit condition) stay in view
        for form in (self.event_form, self.kpi_form):
            for combo in form.findChildren(QComboBox):
                combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
                combo.setMinimumContentsLength(6)
        fit_current(0)
        top.addWidget(self.form_scroll)

        self.palette = ToolPalette(names_fn=state.signal_names)
        self.palette.insert_requested.connect(self.insert_tool)
        top.addWidget(self.palette)
        top.setSizes([260, 700, 300])


        self.events.currentRowChanged.connect(self.event_selected)
        self.kpis.currentRowChanged.connect(self.kpi_selected)
        self.up.clicked.connect(lambda: self.move_kpi(-1))
        self.down.clicked.connect(lambda: self.move_kpi(1))
        # right-click: Add anywhere in the list, Delete on an item
        for lst in (self.events, self.kpis):
            lst.setSelectionMode(QAbstractItemView.ExtendedSelection)  # several can be deleted at once
        context_menu.install(self.events, add=[("Add event", self.new_event)],
                             item=lambda rows: [("Delete event" if len(rows) == 1 else f"Delete {len(rows)} events",
                                                 self.delete_event)])
        context_menu.install(self.kpis, add=[("Add KPI", self.new_kpi)],
                             item=lambda rows: [("Delete KPI" if len(rows) == 1 else f"Delete {len(rows)} KPIs",
                                                 self.delete_kpi)])

        state.profile_changed.connect(self.refresh)
        state.validated.connect(self.on_validated)
        state.validated.connect(self.palette.refresh_names)
        self.refresh()

    # ------------------------------------------------------------------ lists
    def refresh(self):
        cur_ev = self.events.currentItem().text() if self.events.currentItem() else None
        self.events.blockSignals(True)
        self.events.clear()
        for name in self.state.data.get("events", {}):
            self.events.addItem(name)
        self.events.blockSignals(False)
        self.refresh_kpi_labels()
        if self.stack.currentWidget() is self.event_form and self.event_form.name in self.state.data.get("events", {}):
            self.event_form.load(self.event_form.name)
        elif cur_ev and cur_ev in self.state.data.get("events", {}):
            pass
        elif self.stack.currentWidget() is self.kpi_form and self.kpi_form.index is not None \
                and self.kpi_form.index < len(self.state.data.get("kpis", [])):
            self.kpi_form.load(self.kpi_form.index)
        else:
            self.stack.setCurrentWidget(self.empty)

    def refresh_kpi_labels(self):
        row = self.kpis.currentRow()
        self.kpis.blockSignals(True)
        self.kpis.clear()
        for k in self.state.data.get("kpis", []):
            if k.get("use"):
                label = f"⧉ {k['use']}" + (f" as {k['as']}" if k.get("as") else "")
            else:
                label = f"{k.get('event') or '?'}.{k.get('name') or '?'}"
            self.kpis.addItem(label)
        self.kpis.setCurrentRow(row)
        self.kpis.blockSignals(False)

    def select_event(self, name):
        items = self.events.findItems(name, Qt.MatchExactly)
        if items:
            self.events.setCurrentItem(items[0])

    def event_selected(self, row):
        if row < 0:
            return
        self.kpis.blockSignals(True)
        self.kpis.setCurrentRow(-1)
        self.kpis.blockSignals(False)
        self.event_form.load(self.events.item(row).text())
        self.stack.setCurrentWidget(self.event_form)
        if self.viewer is not None:
            self.viewer.set_event(self.events.item(row).text())

    def kpi_selected(self, row):
        if row < 0:
            return
        self.events.blockSignals(True)
        self.events.setCurrentRow(-1)
        self.events.blockSignals(False)
        k = self.state.data["kpis"][row]
        if self.viewer is not None and k.get("event"):
            self.viewer.set_event(k["event"])
        if k.get("use"):
            self.lib_note.setText(f"<b>{k['use']}</b><br><br>This entry takes KPIs from an imported library. "
                                  "Edit the library file to change them, or delete this entry and add your own.")
            self.stack.setCurrentWidget(self.lib_note)
            return
        self.kpi_form.load(row)
        self.stack.setCurrentWidget(self.kpi_form)

    def new_event(self):
        events = self.state.data.setdefault("events", {})
        i = len(events) + 1
        while f"event{i}" in events:
            i += 1
        events[f"event{i}"] = {"start": "", "window": {"pre": 2.0, "post": 2.0}, "moments": {}}
        self.state.edited(structure=True)
        self.select_event(f"event{i}")

    def _selected_rows(self, lst) -> list[int]:
        rows = sorted({lst.row(i) for i in lst.selectedItems()})
        return rows or ([lst.currentRow()] if lst.currentRow() >= 0 else [])

    def delete_event(self):
        names = [self.events.item(r).text() for r in self._selected_rows(self.events)]
        if not names:
            return
        used = [k.get("name") for k in self.state.data.get("kpis", []) if k.get("event") in names]
        what = f"'{names[0]}'" if len(names) == 1 else f"these {len(names)} events"
        if used and QMessageBox.question(self, "Delete event",
                                         f"KPIs {', '.join(used)} use {what}. Delete anyway?") \
                != QMessageBox.Yes:
            return
        for name in names:
            del self.state.data["events"][name]
        self.stack.setCurrentWidget(self.empty)
        self.state.edited(structure=True)

    def new_kpi(self):
        kpis = self.state.data.setdefault("kpis", [])
        names = {k.get("name") for k in kpis}
        i = len(kpis) + 1
        while f"kpi{i}" in names:
            i += 1
        events = self.state.event_names()
        kpis.append({"name": f"kpi{i}", "event": events[0] if events else "", "value": "", "at": "start"})
        self.state.edited(structure=True)
        self.kpis.setCurrentRow(len(kpis) - 1)

    def delete_kpi(self):
        rows = self._selected_rows(self.kpis)
        if not rows:
            return
        for row in reversed(rows):
            del self.state.data["kpis"][row]
        self.stack.setCurrentWidget(self.empty)
        self.kpi_form.index = None
        self.state.edited(structure=True)

    def move_kpi(self, delta):
        row = self.kpis.currentRow()
        kpis = self.state.data.get("kpis", [])
        new = row + delta
        if row < 0 or not 0 <= new < len(kpis):
            return
        kpis[row], kpis[new] = kpis[new], kpis[row]
        self.kpi_form.index = new
        self.state.edited(structure=True)
        self.kpis.setCurrentRow(new)

    # ------------------------------------------------------------------ toolbox
    def set_target(self, edit):
        self.target = edit

    def insert_tool(self, snippet):
        target = self.target
        if target is None or not self.stack.currentWidget().isAncestorOf(target):
            # nothing focused yet: use the main expression field of the form on screen
            if self.stack.currentWidget() is self.event_form:
                target = self.event_form.start
            elif self.stack.currentWidget() is self.kpi_form:
                target = self.kpi_form.expr
        if target is not None:
            target.insert_text(snippet)
            target.setFocus()
            target.editingFinished.emit()

    def on_validated(self):
        if self.stack.currentWidget() is self.kpi_form:
            self.kpi_form.show_status()
