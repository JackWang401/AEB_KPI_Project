from src.kpi_studio.core.rename import rename_in_expression, rename_signal_references


def test_expression_rename_whole_names_only():
    assert rename_in_expression("rising(A == 3) and AB > 1", "A", "B") == "rising(B == 3) and AB > 1"
    assert rename_in_expression("lowpass(A, cutoff_hz=A2)", "A", "Z") == "lowpass(Z, cutoff_hz=A2)"
    assert rename_in_expression('f(x, A=1) + A', "A", "Z") == "f(x, A=1) + Z"
    assert rename_in_expression('mode == "A"', "A", "Z") == 'mode == "A"'
    assert rename_in_expression("A *", "A", "Z") == "Z *"  # unfinished expressions too
    assert rename_in_expression("A == \"x", "A", "Z") == "A == \"x"  # untokenizable text left alone


def test_profile_references_renamed():
    data = {
        "signals": {"New": {"channels": ["c"]}},
        "derived": {"d1": "New_x + Old", "d2": {"expr": "Old * 2", "unit": "m"}},
        "events": {"Old_ev": {"start": "rising(Old == 3)", "end": "falling(Old == 3)",
                              "description": "uses Old",
                              "moments": {"m1": {"last": "rising(Old == 3)"}, "m2": {"vehicle_stop": "Old"}}}},
        "kpis": [{"name": "Old", "event": "Old_ev", "value": "Old", "at": "m1"},
                 {"name": "k2", "event": "Old_ev", "reduce": "max", "of": "abs(Old)"}],
        "plots": [{"type": "timeseries", "event": "Old_ev", "signals": ["Old", "d1"]},
                  {"type": "scatter", "x": "Old", "y": "k2"}],
    }
    n = rename_signal_references(data, "Old", "New")
    ev = data["events"]["Old_ev"]
    assert ev["start"] == "rising(New == 3)" and ev["end"] == "falling(New == 3)"
    assert ev["moments"] == {"m1": {"last": "rising(New == 3)"}, "m2": {"vehicle_stop": "New"}}
    assert ev["description"] == "uses Old"
    assert data["derived"] == {"d1": "New_x + New", "d2": {"expr": "New * 2", "unit": "m"}}
    assert data["kpis"][0] == {"name": "Old", "event": "Old_ev", "value": "New", "at": "m1"}  # KPI name kept
    assert data["kpis"][1]["of"] == "abs(New)"
    assert data["plots"][0]["signals"] == ["New", "d1"] and data["plots"][1]["x"] == "Old"
    assert n == 9
