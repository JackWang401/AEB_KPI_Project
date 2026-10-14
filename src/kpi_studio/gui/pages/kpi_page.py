"""Step 3: define events, windows, moments and KPIs (previewed in the shared signal panel)."""

from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
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
    QMenu,
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
    KpiDef,
    signal_units,
)
from src.kpi_studio.core.rename import rename_kpi, rename_moment
from src.kpi_studio.core.validate import kpi_unit
from src.kpi_studio.gui.state import AppState
from src.kpi_studio.gui.widgets.common import (
    PAGE_MARGINS,
    ConditionBuilder,
    ExprEdit,
    ToolPalette,
    format_options,
    parse_options,
)

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FORMS = [("value", "Value at a moment"), ("duration", "Time between two moments"),
         ("reduce", "Reduce over a range"), ("formula", "Formula of earlier KPIs")]


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
        sb, eb = QPushButton("Build…"), QPushButton("Build…")
        self.build_buttons = (sb, eb)
        srow, erow = QHBoxLayout(), QHBoxLayout()
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
        form.addRow("Trigger", self.mode)
        self.start_label, self.end_label = QLabel("Start when"), QLabel("End when")
        form.addRow(self.start_label, srow)
        form.addRow(self.end_label, erow)
        self.preset_label, self.inputs_label, self.params_label = QLabel("Preset"), QLabel("Inputs"), QLabel("Options")
        form.addRow(self.preset_label, self.preset)
        form.addRow(self.inputs_label, self.preset_inputs)
        form.addRow(self.params_label, self.preset_params)
        occ = QHBoxLayout()
        occ.setSpacing(8)
        for label, w in (("ignore shorter than", self.min_dur), ("merge gaps below", self.merge)):
            occ.addWidget(QLabel(label))
            occ.addWidget(w)
            w.setSuffix(" s")
        occ.addStretch(1)
        form.addRow("Occurrences", occ)
        win = QHBoxLayout()
        win.setSpacing(8)
        for label, w in (("before start", self.pre), ("after end", self.post)):
            win.addWidget(QLabel(label))
            win.addWidget(w)
            w.setSuffix(" s")
        win.addStretch(1)
        form.addRow("Window", win)
        lay.addLayout(form)

        box = QGroupBox("Moments: the exact instants KPIs are read at (start, end, window_start, window_end are built in)")
        bl = QVBoxLayout(box)
        self.moments = QTableWidget(0, 4)
        self.moments.setHorizontalHeaderLabels(["Name", "Rule", "Expression / target moment", "Options"])
        self.moments.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.moments.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        bl.addWidget(self.moments)
        hint = QLabel("Options: after: start, before: end, within: 1.5, between: [start, end], hold: 0.2, "
                      "plus the rule's own options (e.g. threshold: -20). Offset rule: target moment + 'by: -0.5'.")
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        bl.addWidget(hint)
        lay.addWidget(box, 1)

        self.name_edit.editingFinished.connect(self.rename)
        self.desc.editingFinished.connect(lambda: self.set("description", self.desc.text()))
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.start.editingFinished.connect(lambda: self.set("start", self.start.text().strip()))
        self.end.editingFinished.connect(lambda: self.set("end", self.end.text().strip()))
        sb.clicked.connect(lambda: self.build(self.start, "start"))
        eb.clicked.connect(lambda: self.build(self.end, "end"))
        self.preset.currentTextChanged.connect(lambda *_: self.save_preset())
        self.preset_inputs.editingFinished.connect(self.save_preset)
        self.preset_params.editingFinished.connect(self.save_preset)
        for key, w in (("min_duration", self.min_dur), ("merge_gap", self.merge)):
            w.valueChanged.connect(lambda v, k=key: self.set(k, v))
        self.pre.valueChanged.connect(lambda v: self.set_window("pre", v))
        self.post.valueChanged.connect(lambda v: self.set_window("post", v))
        # right-click: on a moment, Build condition… / Delete moment; on the blank space, Add moment
        self.moments.setContextMenuPolicy(Qt.CustomContextMenu)
        self.moments.customContextMenuRequested.connect(
            lambda pos: self._exec_menu(self.moment_menu(pos), self.moments.viewport().mapToGlobal(pos)))
        vh = self.moments.verticalHeader()
        vh.setContextMenuPolicy(Qt.CustomContextMenu)
        vh.customContextMenuRequested.connect(
            lambda pos: self._exec_menu(self.moment_menu(row=vh.logicalIndexAt(pos)), vh.mapToGlobal(pos)))
        self.moments.itemChanged.connect(self.moment_renamed)
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
        self._show_mode()
        self._loading = False

    def _show_mode(self):
        preset = self.mode.currentIndex() == 1
        for w in (self.start, self.end, self.start_label, self.end_label, *self.build_buttons):
            w.setVisible(not preset)
        for w in (self.preset, self.preset_inputs, self.preset_params, self.preset_label, self.inputs_label,
                  self.params_label):
            w.setVisible(preset)

    def mode_changed(self):
        self._show_mode()
        if self._loading:
            return
        if self.mode.currentIndex() == 1:
            self.ev.pop("start", None)
            self.ev.pop("end", None)
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
        dlg = ConditionBuilder(self.state.signal_names(), self, initial=edit.text())
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
        self.moments.blockSignals(True)
        self.moments.setRowCount(len(moments))
        for r, (mname, raw) in enumerate(moments.items()):
            raw = dict(raw or {})
            if "offset" in raw:
                rule, target = "offset", raw.pop("offset")
            else:
                rule = next((k for k in raw if k in tools), tools[0])
                target = raw.pop(rule, "")
            name_item = QTableWidgetItem(mname)
            name_item.setData(Qt.UserRole, mname)
            self.moments.setItem(r, 0, name_item)
            combo = QComboBox()
            combo.addItems(tools + ["offset"])
            for i, t in enumerate(tools + ["offset"]):
                combo.setItemData(i, self.rule_help(t), Qt.ToolTipRole)
            combo.setCurrentText(rule)
            combo.setToolTip(self.rule_help(rule))
            combo.currentTextChanged.connect(lambda t, c=combo: c.setToolTip(self.rule_help(t)))
            combo.currentTextChanged.connect(lambda t, row=r: self.fill_rule_options(row, t))
            combo.currentTextChanged.connect(lambda *_, row=r: self.save_moment(row))
            self.moments.setCellWidget(r, 1, combo)
            expr = ExprEdit(self.state.signal_names, enums_fn=self.state.enum_map,
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

    @staticmethod
    def _exec_menu(menu, global_pos):
        if menu is not None:
            menu.exec(global_pos)

    def moment_menu(self, pos=None, row=None):
        """The right-click menu of the moments table at viewport `pos` (or for `row`)."""
        if self.name is None:
            return None
        if row is None:
            index = self.moments.indexAt(pos)
            row = index.row() if index.isValid() else -1
        menu = QMenu(self.moments)
        if row < 0:
            menu.addAction("Add moment").triggered.connect(self.add_moment)
            return menu
        if not self.moments.selectionModel().isRowSelected(row, self.moments.rootIndex()):
            self.moments.selectRow(row)
        rows = {i.row() for i in self.moments.selectedIndexes()}
        if len(rows) == 1:
            menu.addAction("Build condition…").triggered.connect(lambda: self.build_moment_row(row))
        menu.addAction("Delete moment" if len(rows) == 1 else f"Delete {len(rows)} moments").triggered.connect(
            self.delete_moment)
        menu.addSeparator()  # for when the moments fill the table and there is no blank space left
        menu.addAction("Add moment").triggered.connect(self.add_moment)
        return menu

    def build_moment_row(self, row: int):
        dlg = ConditionBuilder(self.state.signal_names(), self, initial=self.moments.cellWidget(row, 2).text())
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

    def delete_moment(self):
        rows = sorted({i.row() for i in self.moments.selectedIndexes()}, reverse=True)
        moments = self.ev.get("moments", {})
        for r in rows:
            moments.pop(self.moments.item(r, 0).data(Qt.UserRole), None)
        if rows:
            self.state.edited(structure=True)
            self.load_moments()


# ============================================================================ KPI form
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
                             enums_fn=self.state.enum_map)
        self.formula = QLineEdit()
        self.formula.setPlaceholderText("e.g. pedalPosMax - pedalPosAtStart")
        self.at = QComboBox()
        self.m1, self.m2 = QComboBox(), QComboBox()
        self.reducer = QComboBox()
        self.reducer.addItems(toolbox.names("reducer"))
        self.args = QLineEdit()
        self.args.setPlaceholderText("reducer options, if any")
        self.unit = QLineEdit()  # shows the unit taken from the signal; typed only for formulas
        self.unit.setToolTip("Taken from the signal the KPI reads (a KPI is one value of that signal). "
                             "Only a formula needs a unit typed in.")
        self.desc = QLineEdit()
        rng = QHBoxLayout()
        rng.addWidget(self.m1)
        rng.addWidget(QLabel("to"))
        rng.addWidget(self.m2)
        self.rows = {}
        for label, w in (("Name", self.name_edit), ("Event", self.event), ("Kind", self.form),
                         ("Reducer", self.reducer), ("Expression", self.expr), ("Formula", self.formula),
                         ("At moment", self.at), ("From", rng), ("Options", self.args), ("Unit", self.unit),
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
        self.formula.editingFinished.connect(self.save)
        for w in (self.at, self.m1, self.m2):  # a valid choice clears the "missing moment" mark
            w.currentTextChanged.connect(lambda _t, w=w: self._set_moment(w, w.currentText()))
        for w in (self.at, self.m1, self.m2, self.reducer):
            w.currentTextChanged.connect(lambda *_: self.save())
        for w in (self.args, self.unit, self.desc):
            w.editingFinished.connect(self.save)
        self.expr.focused.connect(page.set_target)

    @property
    def k(self) -> dict:
        return self.state.data["kpis"][self.index]

    def _fill_moments(self, event: str):
        moments = self.state.moments_of(event)
        for combo in (self.at, self.m1, self.m2):
            cur = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(moments)
            if cur in moments:
                combo.setCurrentText(cur)
            combo.blockSignals(False)

    @staticmethod
    def _set_moment(combo: QComboBox, value: str):
        """Select `value`; a moment the event no longer has is shown in red at the top, so it can be replaced."""
        combo.blockSignals(True)
        if value and combo.findText(value) < 0:
            combo.insertItem(0, value)
            combo.setItemData(0, QColor("#e34948"), Qt.ForegroundRole)
            combo.setItemData(0, f"'{value}' is not a moment of this event: pick one from the list", Qt.ToolTipRole)
        combo.setCurrentText(value)
        missing = combo.currentIndex() == 0 and combo.itemData(0, Qt.ForegroundRole) is not None
        combo.setStyleSheet("QComboBox { color: #e34948; border: 1px solid #e34948; }" if missing else "")
        combo.setToolTip(f"'{value}' is not a moment of this event: pick one from the list" if missing else "")
        combo.blockSignals(False)

    def load(self, index: int):
        self._loading = True
        self.index = index
        k = self.k
        self.name_edit.setText(k.get("name", ""))
        self.event.blockSignals(True)
        self.event.clear()
        self.event.addItems(self.state.event_names())
        self.event.setCurrentText(k.get("event", ""))
        self.event.blockSignals(False)
        self._fill_moments(k.get("event", ""))
        form = ("value" if "value" in k else "duration" if "duration" in k else "formula" if "formula" in k
                else "reduce")
        self.form.setCurrentIndex([f for f, _ in FORMS].index(form))
        self.expr.setText(k.get("value") or k.get("of") or "")
        self.formula.setText(k.get("formula", ""))
        if form == "reduce":
            rng = k.get("between") or ["window_start", "window_end"]
        else:
            rng = k.get("duration") or ["start", "end"]
        for combo, value in ((self.at, k.get("at", "start")), (self.m1, rng[0]), (self.m2, rng[1])):
            self._set_moment(combo, value)
        self.reducer.setCurrentText(k.get("reduce", "max"))
        self.args.setText(format_options(k.get("args", {})))
        self.update_unit(k)
        self.desc.setText(k.get("description", "") or "")
        self._show_form(form)
        self._loading = False
        self.show_status()

    def _show_form(self, form):
        visible = {
            "value": {"Expression", "At moment"},
            "duration": {"From"},
            "reduce": {"Reducer", "Expression", "From", "Options"},
            "formula": {"Formula"},
        }[form]
        for label in ("Reducer", "Expression", "Formula", "At moment", "From", "Options"):
            lab, w = self.rows[label]
            lab.setVisible(label in visible)
            if hasattr(w, "setVisible"):
                w.setVisible(label in visible)
            else:
                for i in range(w.count()):
                    wid = w.itemAt(i).widget()
                    if wid:
                        wid.setVisible(label in visible)
        self.rows["From"][0].setText("Between" if form == "reduce" else "From")

    def form_changed(self):
        form = self.form.currentData()
        self._show_form(form)
        self.save()

    def event_changed(self, ev):
        self._fill_moments(ev)
        self.save()

    def save(self):
        if self._loading or self.index is None:
            return
        form = self.form.currentData()
        name = self.k.get("name")
        k = {"name": name, "event": self.event.currentText()}
        if form == "value":
            k.update(value=self.expr.text().strip(), at=self.at.currentText())
        elif form == "duration":
            k.update(duration=[self.m1.currentText(), self.m2.currentText()])
        elif form == "reduce":
            k.update(reduce=self.reducer.currentText(), of=self.expr.text().strip())
            if (self.m1.currentText(), self.m2.currentText()) != ("window_start", "window_end"):
                k["between"] = [self.m1.currentText(), self.m2.currentText()]
            try:
                args = parse_options(self.args.text())
                if args:
                    k["args"] = args
            except Exception:  # noqa: BLE001
                pass
        else:
            k.update(formula=self.formula.text().strip())
        if form == "formula":
            if self.unit.text().strip():
                k["unit"] = self.unit.text().strip()
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
        """Show the unit the KPI gets from its signal; editable only for formulas."""
        formula = k.get("formula") is not None
        self.unit.setReadOnly(not formula)
        self.unit.setStyleSheet("" if formula else "QLineEdit { background: transparent; border: none; }")
        if formula:
            self.unit.setText(k.get("unit", "") or "")
            self.unit.setPlaceholderText("unit of the formula (optional)")
            return
        units = signal_units(self.state.resolved) if self.state.resolved is not None else {}
        try:
            unit = kpi_unit(KpiDef.model_validate({kk: v for kk, v in k.items() if v is not None}), units)
        except Exception:  # noqa: BLE001 - incomplete KPI
            unit = ""
        self.unit.setText(unit)
        self.unit.setPlaceholderText("from the signal (it has no unit)")

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
            for b, tip in buttons:
                b.setToolTip(tip)
                b.setFixedSize(30, 26)
                b.setStyleSheet("QPushButton { padding: 0; font-size: 15px; }")
                row.addWidget(b)
            return row

        self.add_ev, self.del_ev = QPushButton("+"), QPushButton("−")
        ll.addLayout(header("Events", [(self.add_ev, "Add an event"), (self.del_ev, "Delete the selected event")]))
        self.events = QListWidget()
        self.events.setMinimumHeight(52)
        ll.addWidget(self.events, 1)
        self.add_kpi, self.del_kpi = QPushButton("+"), QPushButton("−")
        self.up, self.down = QPushButton("↑"), QPushButton("↓")
        krow = header("KPIs", [(self.add_kpi, "Add a KPI"), (self.del_kpi, "Delete the selected KPI"),
                               (self.up, "Move up"), (self.down, "Move down")])
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
                w.setSizePolicy(QSizePolicy.Preferred,
                                QSizePolicy.Preferred if w is self.stack.currentWidget() else QSizePolicy.Ignored)
            self.stack.adjustSize()
        self.stack.currentChanged.connect(fit_current)
        fit_current(0)
        top.addWidget(self.form_scroll)

        self.palette = ToolPalette(names_fn=state.signal_names)
        self.palette.insert_requested.connect(self.insert_tool)
        top.addWidget(self.palette)
        top.setSizes([260, 700, 300])


        self.events.currentRowChanged.connect(self.event_selected)
        self.kpis.currentRowChanged.connect(self.kpi_selected)
        self.add_ev.clicked.connect(self.new_event)
        self.del_ev.clicked.connect(self.delete_event)
        self.add_kpi.clicked.connect(self.new_kpi)
        self.del_kpi.clicked.connect(self.delete_kpi)
        self.up.clicked.connect(lambda: self.move_kpi(-1))
        self.down.clicked.connect(lambda: self.move_kpi(1))

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

    def delete_event(self):
        item = self.events.currentItem()
        if not item:
            return
        name = item.text()
        used = [k.get("name") for k in self.state.data.get("kpis", []) if k.get("event") == name]
        if used and QMessageBox.question(self, "Delete event",
                                         f"KPIs {', '.join(used)} use '{name}'. Delete the event anyway?") \
                != QMessageBox.Yes:
            return
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
        row = self.kpis.currentRow()
        if row < 0:
            return
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
