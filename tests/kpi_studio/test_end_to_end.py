import pandas as pd
import pytest

from src.kpi_studio.app.run_service import RunService
from src.kpi_studio.io import profile_store as ps

PROFILE = ps.builtin_profile_dir() / "aeb_basic.kpi.yaml"


@pytest.mark.parametrize("workers", [1, 2])
def test_run_aeb_profile(mf4_dir, tmp_path, workers):
    folder, info = mf4_dir
    files = [str(folder / "a.mf4"), str(folder / "b.mf4")]
    svc = RunService(PROFILE, files, tmp_path / "out", workers=workers)
    report = svc.preflight()
    assert report.ok, report.text()
    res = svc.run()
    assert res.n_ok == 2
    summary = pd.read_excel(res.excel, sheet_name="Summary")
    assert len(summary) == 3  # two activations in a, one in b
    assert summary["vehSpd [km/h]"].round(1).tolist() == [40.0, 40.0, 60.0]  # b is logged in km/h, converted
    assert summary["Start [s]"].tolist() == pytest.approx([5.0, 13.0, 4.0], abs=0.011)
    dead = summary["aebDeadTime [s]"]
    assert ((dead > 0) & (dead < 0.3)).all()
    sheets = pd.read_excel(res.excel, sheet_name=None)
    assert {"Summary", "Details", "Moments", "Files", "RunInfo"} <= set(sheets)
    files_sheet = sheets["Files"]
    assert (files_sheet["status"] == "ok").all()
    assert res.dashboard and len(res.plots) == 3 + 3
    assert (res.output_dir / "profile.resolved.kpi.yaml").exists()
    from openpyxl import load_workbook

    ws = load_workbook(res.excel)["Summary"]
    floats = [c for row in ws.iter_rows(min_row=2) for c in row if isinstance(c.value, float)]
    assert floats and all(c.number_format == "0.000" for c in floats)  # shown with 3 decimals


def test_bad_file_is_isolated(mf4_dir, tmp_path):
    folder, _ = mf4_dir
    broken = folder / "broken.mf4"
    broken.write_bytes(b"not an mf4")
    svc = RunService(PROFILE, [str(folder / "a.mf4"), str(broken)], tmp_path / "out")
    report = svc.preflight()
    assert any("broken.mf4" in e for e in report.errors)
    res = svc.run()
    statuses = {o.file.split("/")[-1]: o.status for o in res.outcomes}
    assert statuses == {"a.mf4": "ok", "broken.mf4": "error"}


def test_cli_validate_and_run(mf4_dir, tmp_path, capsys):
    from src.kpi_studio.cli import main

    folder, _ = mf4_dir
    assert main(["validate", str(PROFILE)]) == 0
    assert main(["run", str(PROFILE), str(folder), "-o", str(tmp_path / "o")]) == 0
    assert "Recordings OK: 2/2" in capsys.readouterr().out


def test_results_next_to_input_are_replaced(mf4_dir):
    from src.kpi_studio.app.run_service import OUTPUT_MARKER, input_results_dir

    folder, _ = mf4_dir
    files = [str(folder / "a.mf4"), str(folder / "b.mf4")]
    target = input_results_dir(files)
    assert target == folder.resolve() / "kpi_results"
    first = RunService(PROFILE, files, None).run()
    assert first.output_dir == target and (target / "kpi_results.xlsx").exists()
    assert (target / OUTPUT_MARKER).exists() and first.dashboard.startswith(str(target))
    (target / "stale.txt").write_text("from the previous run")
    second = RunService(PROFILE, files, None).run()
    assert second.output_dir == target and not (target / "stale.txt").exists()  # replaced, not merged
    assert sorted(p.name for p in folder.iterdir() if p.name.startswith("kpi_results")) == ["kpi_results"]


def test_foreign_folder_named_kpi_results_is_not_deleted(mf4_dir):
    folder, _ = mf4_dir
    files = [str(folder / "a.mf4")]
    mine = folder / "kpi_results"
    mine.mkdir()
    (mine / "notes.txt").write_text("user data")
    with pytest.raises(RuntimeError, match="not written by KPI Studio"):
        RunService(PROFILE, files, None).run()
    assert (mine / "notes.txt").exists()
