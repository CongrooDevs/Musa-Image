import pytest
from pathlib import Path

from musa.gallery import image_index, query_images
from musa.library import LibraryStore
from musa.state import StateStore, GenerationRecord


def _img(settings, name):
    p = settings.output_root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
    return str(p)


def _rec(id, ts, images, **kw):
    base = dict(id=id, ts=ts, type="generate", conversation_id="c_" + id[-4:], chat_metadata=["c"], prompt="p",
                style=None, style_text=None, aspect_hint=None, model="gemini-flash", project=None, subdir=None,
                refs=[], images=images, status="ok", code=None, gemini_text="", elapsed_s=1.0, parent_id=None,
                user_prompt=None)
    base.update(kw)
    return GenerationRecord(**base)


def test_index_merges_retry_download_and_hides_deleted_missing_and_unsaved(settings):
    st = StateStore(settings.state_dir)
    lib = LibraryStore(settings.state_dir)
    a = _img(settings, "a.jpg")
    b = _img(settings, "b.jpg")
    st.append(_rec("g_00000000000a", "2026-11-01T00:00:00+00:00", [{"index": 0, "path": None}], user_prompt="gato"))
    st.append(_rec("g_00000000000b", "2026-11-01T00:01:00+00:00", [{"index": 0, "path": a}], type="retry_download",
                   parent_id="g_00000000000a"))
    st.append(_rec("g_00000000000c", "2026-11-01T00:02:00+00:00",
                   [{"index": 0, "path": b}, {"index": 1, "path": str(settings.output_root / "gone.jpg")}]))
    lib.mark_deleted("g_00000000000c#0")
    idx = image_index(st, lib, settings.output_root)
    assert set(idx) == {"g_00000000000a#0"}
    e = idx["g_00000000000a#0"]
    assert e.path == Path(a) and e.record.id == "g_00000000000a" and e.record.user_prompt == "gato"


def test_query_filters_and_pagination(settings):
    st = StateStore(settings.state_dir)
    lib = LibraryStore(settings.state_dir)
    for i in range(5):
        st.append(_rec(f"g_00000000000{i}", f"2026-11-01T00:0{i}:00+00:00",
                       [{"index": 0, "path": _img(settings, f"{i}.jpg")}],
                       user_prompt=f"faro {i}" if i % 2 else f"gato {i}", style="post" if i < 2 else None))
    c = lib.create_collection("A")
    lib.add("g_000000000001#0", c.id)
    lib.set_favorite("g_000000000004#0", True)
    idx = image_index(st, lib, settings.output_root)
    keys = lambda r: [e.key for e in r[0]]
    assert keys(query_images(idx, lib)) == [f"g_00000000000{i}#0" for i in (4, 3, 2, 1, 0)]
    assert keys(query_images(idx, lib, query="FARO")) == ["g_000000000003#0", "g_000000000001#0"]
    assert keys(query_images(idx, lib, collection=c.id)) == ["g_000000000001#0"]
    assert keys(query_images(idx, lib, filter="in_collection")) == ["g_000000000001#0"]
    assert len(query_images(idx, lib, filter="no_collection")[0]) == 4
    assert keys(query_images(idx, lib, filter="favorites")) == ["g_000000000004#0"]
    assert keys(query_images(idx, lib, style="post")) == ["g_000000000001#0", "g_000000000000#0"]
    page, total = query_images(idx, lib, limit=2, offset=1)
    assert total == 5 and [e.key for e in page] == ["g_000000000003#0", "g_000000000002#0"]


def _seed(settings, specs):
    """specs: (id suffix, ts, kwargs) -> (idx, lib)."""
    st = StateStore(settings.state_dir)
    lib = LibraryStore(settings.state_dir)
    for sfx, ts, kw in specs:
        st.append(_rec(f"g_{sfx:0>12}", ts, [{"index": 0, "path": _img(settings, f"{sfx}.jpg")}], **kw))
    return image_index(st, lib, settings.output_root), lib


def _k(res):
    return [e.key.split("#")[0][-1] for e in res[0]]


def test_query_model_style_search_sort_and_combined(settings):
    idx, lib = _seed(settings, [
        ("1", "2026-11-01T00:00:00+00:00", dict(model="m-a", style="Poster", user_prompt="faro")),
        ("2", "2026-11-02T00:00:00+00:00", dict(model="m-b", style="poster-2", user_prompt="gato")),
        ("3", "2026-11-03T00:00:00+00:00", dict(model="m-a", style=None, user_prompt="faro azul")),
        ("4", "2026-11-03T00:00:00+00:00", dict(model="m-a", style="Poster", user_prompt="otro")),
    ])
    lib.set_favorite("g_000000000001#0", True)
    assert _k(query_images(idx, lib, model="m-a")) == ["4", "3", "1"]
    assert _k(query_images(idx, lib, model="M-A")) == []
    assert _k(query_images(idx, lib, query="POSTER")) == ["4", "2", "1"]
    assert _k(query_images(idx, lib, query="faro", model="m-a", style="Poster")) == ["1"]
    assert _k(query_images(idx, lib, sort="newest")) == ["4", "3", "2", "1"]
    assert _k(query_images(idx, lib, sort="oldest")) == ["1", "2", "3", "4"]
    assert _k(query_images(idx, lib, sort="favorites")) == ["1", "4", "3", "2"]
    assert _k(query_images(idx, lib, sort="favorites", filter="favorites", model="m-a")) == ["1"]


def test_query_date_range_mixed_zones_and_day_bounds(settings):
    idx, lib = _seed(settings, [
        ("1", "2026-11-08T23:59:59+00:00", {}),
        ("2", "2026-11-09T00:00:00+00:00", {}),
        ("3", "2026-11-09T23:59:59+00:00", {}),
        ("4", "2026-11-10T00:00:00+00:00", {}),
        ("5", "2026-11-09T22:00:00-05:00", {}),  # = 2026-11-10T03:00Z
        ("6", "2026-11-10T01:00:00+02:00", {}),  # = 2026-11-09T23:00Z
    ])
    assert sorted(_k(query_images(idx, lib, since="2026-11-09", until="2026-11-09"))) == ["2", "3", "6"]
    assert sorted(_k(query_images(idx, lib, since="2026-11-09"))) == ["2", "3", "4", "5", "6"]
    assert sorted(_k(query_images(idx, lib, until="2026-11-08"))) == ["1"]
    assert sorted(_k(query_images(idx, lib, since="2026-11-09T23:30:00+00:00"))) == ["3", "4", "5"]
    assert sorted(_k(query_images(idx, lib, since="2026-11-09T18:00:00-05:00",
                                  until="2026-11-10T00:00:00+00:00"))) == ["3", "4", "6"]
    assert _k(query_images(idx, lib, since="2026-11-10T00:00:00Z", until="2026-11-10T00:00:00Z")) == ["4"]


def test_query_invalid_values(settings):
    idx, lib = _seed(settings, [("1", "2026-11-01T00:00:00+00:00", {})])
    for kw in ({"sort": "random"}, {"filter": "x"}, {"limit": 0}, {"limit": -3}, {"since": "ayer"},
               {"until": "2026-13-45"}, {"since": "2026-11-10", "until": "2026-11-09"},
               {"since": "2026-11-09T10:00:00+00:00", "until": "2026-11-09T09:00:00+00:00"}):
        with pytest.raises(ValueError):
            query_images(idx, lib, **kw)
    assert query_images(idx, lib, limit=10_000)[1] == 1


def test_query_pagination_oldest_is_stable(settings):
    idx, lib = _seed(settings, [(str(i), "2026-11-01T00:00:00+00:00" if i < 3 else f"2026-11-0{i}T00:00:00+00:00", {})
                                for i in range(1, 6)])
    pages = [_k(query_images(idx, lib, sort="oldest", limit=2, offset=o)) for o in (0, 2, 4)]
    assert pages == [["1", "2"], ["3", "4"], ["5"]]
    assert query_images(idx, lib, sort="oldest", limit=2, offset=4)[1] == 5

def test_index_excludes_paths_outside_output_root(settings, tmp_path):
    st = StateStore(settings.state_dir)
    lib = LibraryStore(settings.state_dir)
    inside = _img(settings, "in.jpg")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
    st.append(_rec("g_00000000000a", "2026-11-01T00:00:00+00:00",
                   [{"index": 0, "path": inside}, {"index": 1, "path": str(outside)}]))
    idx = image_index(st, lib, settings.output_root)
    assert set(idx) == {"g_00000000000a#0"}


def test_query_sort_uses_real_time_across_zones_and_ties_by_key(settings):
    idx, lib = _seed(settings, [
        ("1", "2026-11-09T22:00:00-05:00", {}),  # 2026-11-10T03:00Z (newest, though text sorts first)
        ("2", "2026-11-10T00:00:00+00:00", {}),
        ("3", "2026-11-10T01:00:00+02:00", {}),  # 2026-11-09T23:00Z (oldest)
        ("4", "2026-11-10T00:00:00+00:00", {}),  # same instant as 2 -> key tie-break
    ])
    assert _k(query_images(idx, lib, sort="newest")) == ["1", "4", "2", "3"]
    assert _k(query_images(idx, lib, sort="oldest")) == ["3", "2", "4", "1"]
    lib.set_favorite("g_000000000003#0", True)
    lib.set_favorite("g_000000000002#0", True)
    lib.set_favorite("g_000000000004#0", True)
    assert _k(query_images(idx, lib, sort="favorites")) == ["4", "2", "3", "1"]


def test_query_style_names_and_validation_edges(settings):
    idx, lib = _seed(settings, [("1", "2026-11-01T00:00:00+00:00", dict(style="s1")),
                                ("2", "2026-11-02T00:00:00+00:00", dict(style="s2"))])
    assert _k(query_images(idx, lib, query="NEON", style_names={"s1": "Neon Glow"})) == ["1"]
    assert _k(query_images(idx, lib, query="neon")) == []
    with pytest.raises(ValueError, match="out of range"):
        query_images(idx, lib, until="9999-12-31")
    with pytest.raises(ValueError):
        query_images(idx, lib, until="9999-12-31T23:59:59.999999+00:00")
    with pytest.raises(ValueError):
        query_images(idx, lib, limit=None)


def test_query_day_end_and_unreadable_ts(settings):
    idx, lib = _seed(settings, [
        ("1", "2026-11-09T23:59:59.999999+00:00", {}),
        ("2", "2026-11-10T00:00:00+00:00", {}),
        ("3", "not-a-date", {}),
    ])
    assert _k(query_images(idx, lib, until="2026-11-09")) == ["1"]
    assert _k(query_images(idx, lib, since="2026-11-01")) == ["2", "1"]
    assert sorted(_k(query_images(idx, lib))) == ["1", "2", "3"]
    assert _k(query_images(idx, lib, sort="oldest"))[0] == "3"
