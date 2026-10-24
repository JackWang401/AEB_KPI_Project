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


def test_derived_with_unit_and_description(tmp_path):
    from src.kpi_studio.core.model import derived_to_yaml, signal_units

    prof = write(tmp_path / "p.kpi.yaml", {
        "name": "P", "signals": {"speed": {"channels": ["v"], "unit": "m/s"}},
        "derived": {"plain": "speed * 2",
                    "speedKph": {"expr": "speed * 3.6", "unit": "km/h", "description": "speed in km/h"}},
    })
    resolved, cp = ps.load_compiled(prof)
    assert {n for n, _ in cp.derived} == {"plain", "speedKph"}
    assert resolved.derived["speedKph"].description == "speed in km/h"
    assert signal_units(resolved) == {"speed": "m/s", "plain": "", "speedKph": "km/h"}
    assert derived_to_yaml(resolved.derived["plain"]) == "speed * 2"  # short form kept
    assert derived_to_yaml(resolved.derived["speedKph"])["unit"] == "km/h"


def test_saved_profile_is_tidy_and_loads_the_same(tmp_path):
    import yaml

    from src.kpi_studio.core.model import Profile
    from src.kpi_studio.io import profile_store

    path = profile_store.builtin_profile_dir() / "aeb_example.kpi.yaml"
    model = Profile.model_validate(profile_store.inline_libraries(yaml.safe_load(path.read_text()), path.parent))
    text = profile_store.to_yaml(model)
    again = Profile.model_validate(yaml.safe_load(text))
    assert again.model_dump() == model.model_dump()  # defaults left out, nothing lost
    lines = text.splitlines()
    assert lines[:2] == ["schema_version: 1", f"name: {model.name}"]
    assert "highlight: false" not in text and "start_rule: first" not in text and "required: true" not in text
    assert "" in lines and not any(len(line) > 100 and "\n" in line for line in lines)
    assert all(not line.startswith((" ", "-")) or line.strip() for line in lines)  # blank lines are empty
