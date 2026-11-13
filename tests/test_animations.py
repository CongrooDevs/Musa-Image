import json
import stat

from musa.animations import AnimationStore

K = "g_0123456789ab#0"


def _f(**kw):
    d = dict(cols=4, rows=2, mode="sprite", key_color="green", tolerance=25, fps=10, loop="loop",
             cells=[[0, 0, 10, 10]] * 8, detected_by="lanes", status="ok", frames=8)
    d.update(kw)
    return d


def test_put_get_all(tmp_path):
    s = AnimationStore(tmp_path)
    assert s.get(K) is None and s.all() == {} and s.mtime() == 0.0
    a = s.put(K, _f())
    assert s.get(K) == a and a["key"] == K and a["rev"] > 0
    assert a["created"] and a["updated"]
    assert s.all() == {K: a} and s.mtime() > 0
    assert stat.S_IMODE((tmp_path / "animations.json").stat().st_mode) == 0o600


def test_fps_loop_keep_rev_and_created(tmp_path):
    s = AnimationStore(tmp_path)
    a = s.put(K, _f())
    b = s.put(K, _f(fps=24, loop="pingpong"))
    assert b["rev"] == a["rev"] and b["created"] == a["created"] and b["fps"] == 24


def test_sheet_changes_bump_rev_strictly(tmp_path):
    s = AnimationStore(tmp_path)
    a = s.put(K, _f())
    prev = a["rev"]
    for change in (dict(tolerance=30), dict(cells=[[1, 1, 9, 9]] * 8), dict(mode="scene", key_color=None),
                   dict(key_color="magenta"), dict(cols=2, rows=4)):
        base = _f()
        base.update(change)
        cur = s.put(K, base)
        assert cur["rev"] > prev
        prev = cur["rev"]
        s.put(K, _f())  # reset (rev bumps again)
        prev = s.get(K)["rev"]


def test_rev_monotonic_after_remove_put(tmp_path):
    s = AnimationStore(tmp_path)
    revs = []
    for _ in range(5):
        revs.append(s.put(K, _f())["rev"])
        assert s.remove(K) is True
    assert revs == sorted(set(revs))
    assert s.remove(K) is False


def test_prune(tmp_path):
    s = AnimationStore(tmp_path)
    k2, k3 = "g_aaaaaaaaaaaa#0", "g_bbbbbbbbbbbb#1"
    for k in (K, k2, k3):
        s.put(k, _f())
    assert s.prune({K, k3}) == 1
    assert set(s.all()) == {K, k3}
    assert s.prune({K, k3}) == 0


def test_corrupt_file_is_moved_and_store_starts_empty(tmp_path):
    (tmp_path / "animations.json").write_text("{not json", encoding="utf-8")
    s = AnimationStore(tmp_path)
    assert s.all() == {}
    assert list(tmp_path.glob("animations.json.corrupt-*"))
    s.put(K, _f())
    assert json.loads((tmp_path / "animations.json").read_text())["version"] == 1
    (tmp_path / "animations.json").write_text('{"animations": []}', encoding="utf-8")
    assert s.get(K) is None
