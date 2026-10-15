"""One checkbox look for the whole app: rounded boxes that turn blue with a white tick when checked,
for QCheckBox and for checkable rows in lists, tables and trees."""

from __future__ import annotations

import tempfile
from pathlib import Path

_TICK = ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
         '<path d="M4 8.4l2.6 2.6L12 5.4" fill="none" stroke="#ffffff" stroke-width="2" '
         'stroke-linecap="round" stroke-linejoin="round"/></svg>')
_DASH = ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
         '<path d="M4.5 8h7" fill="none" stroke="#ffffff" stroke-width="2" stroke-linecap="round"/></svg>')


def _icons() -> tuple[str, str]:
    """The tick and dash images, written once to a cache folder (style sheets need file paths)."""
    folder = Path(tempfile.gettempdir()) / "kpi_studio_icons"
    folder.mkdir(exist_ok=True)
    paths = []
    for name, svg in (("tick.svg", _TICK), ("dash.svg", _DASH)):
        p = folder / name
        if not p.exists() or p.read_text() != svg:
            p.write_text(svg)
        paths.append(p.as_posix())
    return paths[0], paths[1]


def style() -> str:
    tick, dash = _icons()
    box = "QCheckBox::indicator, QAbstractItemView::indicator"
    return f"""
QCheckBox {{ spacing: 8px; }}
{box} {{ width: 14px; height: 14px; border: 1px solid #c7c7cc; border-radius: 4px; background: #ffffff; }}
QCheckBox::indicator:hover, QAbstractItemView::indicator:hover {{ border-color: #007aff; }}
QCheckBox::indicator:checked, QAbstractItemView::indicator:checked {{
    background: #007aff; border-color: #007aff; image: url("{tick}"); }}
QCheckBox::indicator:indeterminate, QAbstractItemView::indicator:indeterminate {{
    background: #007aff; border-color: #007aff; image: url("{dash}"); }}
QAbstractItemView::indicator:selected {{ border-color: #ffffff; }}
QCheckBox::indicator:disabled, QAbstractItemView::indicator:disabled {{ background: #f2f2f7; border-color: #d1d1d6; }}
QCheckBox::indicator:checked:disabled {{ background: #a6cdfb; border-color: #a6cdfb; }}
"""
