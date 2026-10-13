import pytest
import yaml

from src.kpi_studio.core.validate import ProfileError
from src.kpi_studio.io import profile_store as ps


def write(path, data):
    path.write_text(yaml.safe_dump(data))
    return path


LIB = {
    "name": "mylib",
    "signals": {"speed": {"channels": ["v"], "unit": "m/s"}, "req": {"channels": ["r"], "kind": "discrete"}},
    "events": {"go": {"start": "req > 0", "moments": {"stop": {"vehicle_stop": "speed"}}}},
    "kpis": [{"name": "v0", "event": "go", "value": "speed", "at": "start"},
             {"name": "dur", "event": "go", "duration": ["start", "stop"]}],
}


def test_imports_use_and_rename(tmp_path):
    write(tmp_path / "mylib.kpi-lib.yaml", LIB)
    prof = write(tmp_path / "p.kpi.yaml", {
        "name": "P", "imports": ["mylib"],
        "signals": {"speed": {"channels": ["SPEED_OTHER_LOGGER"], "unit": "km/h"}},
        "kpis": [{"use": "mylib.v0", "as": "speedAtGo"}, {"use": "mylib.dur"},
                 {"name": "mine", "event": "mylib.go", "reduce": "max", "of": "speed"}],
    })
    resolved, cp = ps.load_compiled(prof)
    assert [k.name for k in resolved.kpis] == ["speedAtGo", "dur", "mine"]
    assert set(resolved.events) == {"mylib.go"}
    assert resolved.signals["speed"].channels == ["SPEED_OTHER_LOGGER"]  # profile override wins
    assert not resolved.imports


def test_conflicting_libraries(tmp_path):
    write(tmp_path / "a.kpi-lib.yaml", {"name": "a", "signals": {"x": {"channels": ["x1"]}}})
    write(tmp_path / "b.kpi-lib.yaml", {"name": "b", "signals": {"x": {"channels": ["x2"]}}})
    prof = write(tmp_path / "p.kpi.yaml", {"name": "P", "imports": ["a", "b"]})
    with pytest.raises(ProfileError, match="defined differently"):
        ps.load_compiled(prof)


def test_unknown_use(tmp_path):
    write(tmp_path / "mylib.kpi-lib.yaml", LIB)
    prof = write(tmp_path / "p.kpi.yaml", {"name": "P", "imports": ["mylib"], "kpis": [{"use": "mylib.nope"}]})
    with pytest.raises(ProfileError, match="no KPI 'nope'"):
        ps.load_compiled(prof)


def test_newer_schema_rejected(tmp_path):
    prof = write(tmp_path / "p.kpi.yaml", {"name": "P", "schema_version": 99})
    with pytest.raises(ProfileError, match="newer"):
        ps.load_profile(prof)


def test_round_trip(tmp_path):
    write(tmp_path / "mylib.kpi-lib.yaml", LIB)
    prof = write(tmp_path / "p.kpi.yaml", {"name": "P", "imports": ["mylib"], "kpis": [{"use": "mylib.*"}]})
    resolved, _ = ps.load_compiled(prof)
    out = tmp_path / "resolved.kpi.yaml"
    ps.save_profile(resolved, out)
    again, cp = ps.load_compiled(out)
    assert again == resolved


@pytest.mark.parametrize("path", sorted(ps.builtin_profile_dir().glob("*.kpi.yaml")) +
                         sorted(ps.builtin_library_dir().glob("*.kpi-lib.yaml")), ids=lambda p: p.name)
def test_builtin_files_are_valid(path):
    if path.name.endswith(ps.LIBRARY_SUFFIX):
        lib = ps.load_library(path)
        # a library is valid when a profile importing all of it compiles
        from src.kpi_studio.core.model import Profile

        imports = ["vehicle", lib.name] if lib.name != "vehicle" else ["vehicle"]
        prof = Profile(name="check", imports=imports, kpis=[{"use": f"{lib.name}.*"}] if lib.kpis else [])
        ps.load_compiled(prof, base_dir=ps.builtin_library_dir())
    else:
        ps.load_compiled(path)
