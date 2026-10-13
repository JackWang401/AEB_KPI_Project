"""One drive logged as several MF4 files (one per bus/ECU) is read as one recording."""

import datetime as dt

import numpy as np
import pytest

from src.kpi_studio.app.run_service import RunService, process_file
from src.kpi_studio.io import profile_store
from src.kpi_studio.io.mf4 import Recording, group_recordings, index_file, read_signals, recording_key

PROFILE = profile_store.builtin_profile_dir() / "aeb_basic.kpi.yaml"


def test_recording_key_and_grouping():
    files = ["/d/AADT-150453_002_ABD_Robot__20260826T151159.mf4",
             "/d/AADT-150453_002_MOTION_1__20260826T151158.mf4",
             "/d/AADT-150453_003_MOTION_1__20260826T161158.mf4",
             "/e/AADT-150453_002_MOTION_1__20260826T151158.mf4",
             "/d/250618_roadcast_stopgap.mf4"]
    assert recording_key(files[0]) == recording_key(files[1]) == "AADT-150453_002"
    assert recording_key(files[4]) == "250618_roadcast_stopgap"
    recs = group_recordings(files)
    assert [len(r.files) for r in recs] == [2, 1, 1, 1]  # other run and other folder stay apart
    assert recs[0].name.endswith("AADT-150453_002")
    assert [len(r.files) for r in group_recordings(files, combine=False)] == [1] * 5


def _split_recording(folder, shift=1.0):
    """Vehicle signals in a MOTION file, AEB signals in an ABD file that starts `shift` s later."""
    from asammdf import MDF, Signal

    t = np.arange(0.0, 20.0, 0.01)
    v = np.clip(40 / 3.6 - 8.0 * np.clip(t - 5.15, 0, None), 0, None)
    accel = np.where((t >= 5.15) & (v > 0), -8.0, 0.0)
    target = np.where((t >= 5.0) & (v > 0), -15.0, 0.0)
    start = dt.datetime(2026, 8, 26, 15, 11, 58, tzinfo=dt.timezone.utc)

    motion = MDF(version="4.10")
    motion.header.start_time = start
    motion.append([Signal(v, t, name="speedESC", unit="m/s")], acq_name="vehicleIOActuationFeedback")
    motion.append([Signal(accel, t, name="acceleration[0]", unit="m/s2"),
                   Signal(np.zeros_like(t), t, name="acceleration[1]", unit="m/s2")], acq_name="imuFrame")
    p1 = folder / "AADT-1_002_MOTION_1__20260826T151158.mf4"
    motion.save(str(p1), overwrite=True)

    abd = MDF(version="4.10")
    abd.header.start_time = start + dt.timedelta(seconds=shift)
    keep = t >= shift
    abd.append([Signal(target[keep], t[keep] - shift, name="payload/long_ctrl_accel_request", unit="m/s2")],
               acq_name="vehicleIOASILCommandE2EWrapper")
    p2 = folder / "AADT-1_002_ABD_Robot__20260826T151159.mf4"
    abd.save(str(p2), overwrite=True)
    return [str(p1), str(p2)]


def test_signals_from_two_files_share_one_time_axis(tmp_path):
    pytest.importorskip("asammdf")
    files = _split_recording(tmp_path)
    indexes = {f: index_file(f) for f in files}
    assert indexes[files[1]].abs_start - indexes[files[0]].abs_start == pytest.approx(1.0)
    rec = group_recordings(files, indexes)[0]
    assert len(rec.files) == 2
    _, cp = profile_store.load_compiled(PROFILE)
    raw, issues = read_signals(rec, cp.profile.signals, indexes)
    assert {"egoSpeed", "longActAccel", "aebTargetDecel"} <= set(raw)
    # the ABD file starts 1 s later: its first sample lands at t = 1 s on the common axis
    assert raw["aebTargetDecel"].timestamps[0] == pytest.approx(1.0)
    assert "(AADT-1_002_ABD_Robot" in raw["aebTargetDecel"].channel


def test_split_recording_gives_kpis_that_neither_file_gives_alone(tmp_path):
    pytest.importorskip("asammdf")
    files = _split_recording(tmp_path)
    _, cp = profile_store.load_compiled(PROFILE)
    combined = process_file(cp, Recording(str(tmp_path / "AADT-1_002"), files))
    assert combined.status == "ok", [i.message for i in combined.issues]
    assert combined.occurrences, "AEB event found when both files are read together"
    speed = [r for r in combined.results if r.kpi == "vehSpd"][0]
    assert speed.value == pytest.approx(40.0, abs=0.5)
    alone = process_file(cp, files[0])
    assert not alone.occurrences

    svc = RunService(PROFILE, files, tmp_path / "out")
    report = svc.preflight()
    assert len(report.recordings) == 1 and report.ok, report.text()
    res = svc.run()
    assert len(res.outcomes) == 1 and res.n_ok == 1


def test_text_enum_channel_reads_raw_values_and_labels(tmp_path):
    """A channel with a value-to-text conversion and no 'enum:' in the profile still plots."""
    from asammdf import MDF, Signal

    from src.kpi_studio.core.align import build_frame
    from src.kpi_studio.core.model import SignalDef
    from src.kpi_studio.io.mf4 import read_signals

    t = np.arange(0.0, 2.0, 0.01)
    vals = np.where(t < 1.0, 3, 1).astype("u1")
    conv = {"val_0": 0, "text_0": b"NotAvailable", "val_1": 1, "text_1": b"FailureDetectedReducedAvl",
            "val_2": 3, "text_2": b"Available", "default": b""}
    path = tmp_path / "enum.mf4"
    m = MDF(version="4.10")
    m.append([Signal(vals, t, name="LmcDrvLvl12LongEmerSrvAvl", conversion=conv)], acq_name="MOTION_1")
    m.save(path, overwrite=True)
    defs = {"srvAvl": SignalDef(channels=["LmcDrvLvl12LongEmerSrvAvl"], kind="discrete")}
    raw, issues = read_signals(str(path), defs)
    rs = raw["srvAvl"]
    assert rs.samples.dtype.kind in "SUO"  # physical values are text
    assert rs.labels == {1.0: "FailureDetectedReducedAvl", 3.0: "Available"}
    frame, align_issues, _ = build_frame(raw, defs, 100.0)
    v = frame.values["srvAvl"]
    assert not np.isnan(v).any() and set(np.unique(v)) == {1.0, 3.0}
    assert not [i for i in align_issues if i.severity == "warning"]
