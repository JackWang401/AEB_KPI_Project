"""KPI Studio toolbox: importing this package registers every built-in tool."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

from src.kpi_studio.toolbox import (  # noqa: F401  (imports register the tools)
    condition_tools,
    event_tools,
    moment_tools,
    reducer_tools,
    signal_tools,
)
from src.kpi_studio.toolbox.registry import (  # noqa: F401
    KINDS,
    Param,
    ToolContext,
    ToolError,
    ToolSpec,
    find_expression_tool,
    get,
    names,
    register,
    tool,
    tools,
)

log = logging.getLogger(__name__)

TOOLBOX_VERSION = "1"

_loaded_user_files: list[str] = []


def load_user_tools(folder: str | Path) -> list[str]:
    """Import every *.py file in `folder` so its @tool functions register.

    This executes user code, so callers only do it when the user enabled
    user tools in settings. Returns the files loaded; failures are logged and skipped.
    """
    loaded = []
    folder = Path(folder)
    if not folder.is_dir():
        return loaded
    for path in sorted(folder.glob("*.py")):
        if str(path) in _loaded_user_files:
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"kpi_studio_user_tools.{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            _mark_user_tools(module.__name__)
            _loaded_user_files.append(str(path))
            loaded.append(str(path))
        except Exception:  # noqa: BLE001
            log.exception("Failed to load user tool file %s", path)
    return loaded


def _mark_user_tools(module_name: str) -> None:
    import dataclasses

    for spec in tools():
        if getattr(spec.func, "__module__", "") == module_name and spec.origin != "user":
            register(dataclasses.replace(spec, origin="user"), replace=True)


def loaded_user_tool_files() -> list[str]:
    return list(_loaded_user_files)
