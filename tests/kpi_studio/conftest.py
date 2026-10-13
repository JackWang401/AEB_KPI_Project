import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def mf4_dir(tmp_path):
    from mf4_factory import aeb_recording

    info = {
        "a": aeb_recording(tmp_path / "a.mf4", speed_kph=40, aeb_at=(5.0, 13.0)),
        "b": aeb_recording(tmp_path / "b.mf4", speed_kph=60, aeb_at=(4.0,), speed_unit="km/h", with_gap=False),
    }
    return tmp_path, info
