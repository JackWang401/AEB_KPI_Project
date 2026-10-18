"""Reading speed: cached header indexes, cheap time ranges, one batched read per file."""

from src.kpi_studio.io import mf4


def test_index_cached_on_disk(mf4_dir, monkeypatch):
    path = str(mf4_dir[0] / "a.mf4")
    first = mf4.index_file(path)
    assert first.ok and first.channels
    monkeypatch.setattr(mf4, "_open", lambda p, blocks=None: (_ for _ in ()).throw(AssertionError("file opened again")))
    again = mf4.index_file(path)
    assert [c.name for c in again.channels] == [c.name for c in first.channels]
    assert (again.start, again.end) == (first.start, first.end)


def test_index_cache_follows_file_changes(mf4_dir):
    from mf4_factory import aeb_recording

    path = mf4_dir[0] / "a.mf4"
    before = mf4.index_file(path)
    aeb_recording(path, speed_kph=40, duration=30.0)
    after = mf4.index_file(path)
    assert after.end > before.end + 5


def test_time_range_matches_full_read(mf4_dir):
    path = str(mf4_dir[0] / "b.mf4")
    idx = mf4.index_file(path, cache=False)
    mdf = mf4._open(path)
    try:
        masters = [mdf.get_master(gi) for gi in range(len(mdf.groups))]
    finally:
        mdf.close()
    masters = [m for m in masters if len(m)]
    assert idx.start == min(float(m[0]) for m in masters)
    assert idx.end == max(float(m[-1]) for m in masters)


def test_one_select_per_file(mf4_dir, monkeypatch):
    from src.kpi_studio.io import profile_store

    path = str(mf4_dir[0] / "a.mf4")
    _, cp = profile_store.load_compiled(profile_store.builtin_profile_dir() / "aeb_basic.kpi.yaml")
    calls = []
    real_open = mf4._open

    def counting_open(p, blocks=None):
        mdf = real_open(p, blocks)
        real_select = mdf.select

        def select(channels, *a, **k):
            calls.append(len(channels))
            return real_select(channels, *a, **k)

        mdf.select = select
        return mdf

    monkeypatch.setattr(mf4, "_open", counting_open)
    raw, _ = mf4.read_signals(path, cp.profile.signals)
    assert raw and calls == [len(raw)]


def test_only_needed_groups_opened(mf4_dir):
    import numpy as np

    path = str(mf4_dir[0] / "a.mf4")
    idx = mf4.index_file(path)
    assert idx.block_addrs and len(idx.block_addrs) > 1
    ch = idx.channels[0]
    full = mf4._open(path)
    try:
        ref = full.get(ch.name, group=ch.group, index=ch.index)
    finally:
        full.close()
    lazy = mf4._open(path, {idx.block_addrs[ch.group]})
    try:
        others = [gi for gi in idx.block_addrs if idx.block_addrs[gi] != idx.block_addrs[ch.group]]
        assert others and all(not lazy.groups[gi].data_blocks for gi in others)
        got = lazy.get(ch.name, group=ch.group, index=ch.index)
    finally:
        lazy.close()
    assert np.array_equal(got.samples, ref.samples) and np.array_equal(got.timestamps, ref.timestamps)
    # a plain open afterwards still resolves every group
    again = mf4._open(path)
    try:
        assert all(again.groups[gi].data_blocks or not again.groups[gi].channel_group.cycles_nr for gi in others)
    finally:
        again.close()


def test_wrong_block_addresses_fall_back_to_full_open(mf4_dir):
    from src.kpi_studio.io import profile_store

    path = str(mf4_dir[0] / "a.mf4")
    _, cp = profile_store.load_compiled(profile_store.builtin_profile_dir() / "aeb_basic.kpi.yaml")
    good, _ = mf4.read_signals(path, cp.profile.signals)
    idx = mf4.index_file(path)
    idx.block_addrs = {gi: 1 for gi in idx.block_addrs}  # no group has data there
    raw, _ = mf4.read_signals(path, cp.profile.signals, idx)
    assert set(raw) == set(good)
    assert all(len(raw[n].samples) == len(good[n].samples) for n in good)
