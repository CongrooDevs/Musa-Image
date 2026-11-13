import json
from dataclasses import asdict
from datetime import datetime, timezone
from musa.state import StateStore, GenerationRecord, display_prompt

def rec(cid="c_1", **kw):
    base = dict(id="g_1", ts=datetime.now(timezone.utc).isoformat(), type="generate", conversation_id=cid,
                chat_metadata=["cid", "rid", "rcid"], prompt="p", style=None, style_text=None, aspect_hint=None,
                model="gemini-flash", project=None, subdir=None, refs=[], images=[{"path": "/tmp/a.png"}],
                status="ok", code=None, gemini_text="", elapsed_s=1.0, parent_id=None, user_prompt=None)
    base.update(kw)
    return GenerationRecord(**base)

def test_append_and_load(settings):
    st = StateStore(settings.state_dir)
    st.append(rec())
    st.append(rec(id="g_2", conversation_id="c_1", chat_metadata=["cid2", "rid2", "rcid2"]))
    allr = st.load_all()
    assert len(allr) == 2 and allr[1].chat_metadata[0] == "cid2"

def test_load_all_skips_corrupt_lines(settings, capsys):
    st = StateStore(settings.state_dir)
    st.append(rec())
    with st.file.open("a", encoding="utf-8") as f:
        f.write('{"id": "broken\n[1, 2]\n{"id": "x"}\n')
    assert [r.id for r in st.load_all()] == ["g_1"]
    assert st.conversation("c_1").id == "g_1"
    assert "corrupt" in capsys.readouterr().err

def test_conversation_devuelve_ultima_metadata(settings):
    st = StateStore(settings.state_dir)
    st.append(rec(chat_metadata=["old"]))
    st.append(rec(id="g_2", chat_metadata=["new"]))
    assert st.conversation("c_1").chat_metadata == ["new"]
    assert st.conversation("nope") is None

def test_count_images_today(settings):
    st = StateStore(settings.state_dir)
    st.append(rec())
    st.append(rec(id="g_2", ts="2000-01-01T00:00:00+00:00"))
    assert st.count_images_today() == 1


def test_count_images_since_dedups_retry(settings):
    st = StateStore(settings.state_dir)
    st.append(rec(id="g_a", ts="2026-11-10T01:00:00+00:00", images=[{"index": 0, "path": "/x/0.png"}]))
    st.append(rec(id="g_b", ts="2026-11-09T01:00:00+00:00", images=[{"index": 0, "path": "/x/1.png"}]))
    st.append(rec(id="g_r", ts="2026-11-10T02:00:00+00:00", type="retry_download", parent_id="g_a",
                  images=[{"index": 0, "path": "/x/0.png"}, {"index": 1, "path": "/x/2.png"}]))
    assert st.count_images_since("2026-11-10T00:00:00+00:00") == 2  # g_a#0 no se cuenta dos veces
    assert st.count_images_since("2026-11-09T00:00:00+00:00") == 3

def test_new_id(settings):
    a, b = StateStore.new_id("g"), StateStore.new_id("c")
    assert a.startswith("g_") and b.startswith("c_") and a != b and len(a) == 14

def test_line_roundtrip():
    r = rec()
    assert GenerationRecord.from_line(r.to_line()) == r

def test_conversation_borrada_devuelve_none(settings):
    st = StateStore(settings.state_dir)
    st.append(rec())
    st.append(rec(id="g_2", type="delete", chat_metadata=None))
    assert st.conversation("c_1") is None

def test_list_filters_and_order(settings):
    st = StateStore(settings.state_dir)
    st.append(rec(id="g_000000000001", ts="2026-11-01T10:00:00+00:00", prompt="red cat", project="a"))
    st.append(rec(id="g_000000000002", ts="2026-11-02T10:00:00+00:00", prompt="blue dog", project="b",
                  conversation_id="c_2"))
    st.append(rec(id="g_000000000003", ts="2026-11-03T10:00:00+00:00", prompt="Red fox", project="a",
                  conversation_id="c_3", status="error", images=[]))
    assert [r.id for r in st.list()] == ["g_000000000003", "g_000000000002", "g_000000000001"]
    assert [r.id for r in st.list(query="red")] == ["g_000000000003", "g_000000000001"]
    assert [r.id for r in st.list(project="a", status="ok")] == ["g_000000000001"]
    assert [r.id for r in st.list(since="2026-11-02")] == ["g_000000000003", "g_000000000002"]
    assert [r.id for r in st.list(limit=1, offset=1)] == ["g_000000000002"]

def test_list_hides_deleted_conversations_and_delete_records(settings):
    st = StateStore(settings.state_dir)
    st.append(rec(id="g_000000000001", conversation_id="c_x"))
    st.append(rec(id="g_000000000002", conversation_id="c_x", type="delete", chat_metadata=None, images=[]))
    st.append(rec(id="g_000000000003", conversation_id="c_y"))
    assert [r.id for r in st.list()] == ["g_000000000003"]

def test_list_keeps_records_after_a_later_delete(settings):
    st = StateStore(settings.state_dir)
    st.append(rec(id="g_000000000001", ts="2026-11-01T10:00:00+00:00"))
    st.append(rec(id="g_000000000002", ts="2026-11-02T10:00:00+00:00", type="delete", chat_metadata=None, images=[]))
    st.append(rec(id="g_000000000003", ts="2026-11-03T10:00:00+00:00"))
    assert [r.id for r in st.list()] == ["g_000000000003"]


def test_list_limit_is_capped(settings):
    st = StateStore(settings.state_dir)
    for i in range(120):
        st.append(rec(id=f"g_{i:012x}", conversation_id=f"c_{i}"))
    assert len(st.list(limit=500)) == 100

def test_generation_lookup(settings):
    st = StateStore(settings.state_dir)
    st.append(rec(id="g_00000000000a"))
    assert st.generation("g_00000000000a").id == "g_00000000000a" and st.generation("g_nope") is None

def test_user_prompt_roundtrip_and_old_lines_load(settings):
    st = StateStore(settings.state_dir)
    st.append(rec(id="g_000000000001", user_prompt="un gato"))
    old_line = json.dumps({k: v for k, v in asdict(rec(id="g_000000000002")).items() if k != "user_prompt"})
    with st.file.open("a", encoding="utf-8") as f:
        f.write(old_line + "\n")
    got = {r.id: r for r in st.load_all()}
    assert got["g_000000000001"].user_prompt == "un gato"
    assert got["g_000000000002"].user_prompt is None

def test_gem_id_roundtrip_and_old_lines_load(settings):
    st = StateStore(settings.state_dir)
    st.append(rec(id="g_000000000001", gem_id="gx"))
    old_line = json.dumps({k: v for k, v in asdict(rec(id="g_000000000002")).items() if k != "gem_id"})
    with st.file.open("a", encoding="utf-8") as f:
        f.write(old_line + "\n")
    got = {r.id: r for r in st.load_all()}
    assert got["g_000000000001"].gem_id == "gx"
    assert got["g_000000000002"].gem_id is None

def test_display_prompt():
    assert display_prompt(rec(prompt="Generate an image (do not search the web): a cat anime style", user_prompt="a cat")) == "a cat"
    assert display_prompt(rec(prompt="Generate an image (do not search the web): a cat anime style")) == "a cat anime style"

def test_visible_records_hides_before_delete_and_has_no_limit(settings):
    st = StateStore(settings.state_dir)
    for i in range(150):
        st.append(rec(id=f"g_{i:012x}", conversation_id=f"c_{i}", ts=f"2026-11-01T00:{i // 60:02d}:{i % 60:02d}+00:00"))
    st.append(rec(id="g_00000000ffff", conversation_id="c_0", type="delete", chat_metadata=None, images=[],
                  ts="2026-11-02T00:00:00+00:00"))
    vis = st.visible_records()
    assert len(vis) == 149 and all(r.type != "delete" for r in vis) and vis[0].ts <= vis[-1].ts

def test_images_per_day_returns_consecutive_days(settings):
    from datetime import date, timedelta
    st = StateStore(settings.state_dir)
    days = st.images_per_day(7, today=date(2026, 11, 10))
    assert [d["date"] for d in days] == [(date(2026, 11, 10) - timedelta(days=i)).isoformat()
                                          for i in range(6, -1, -1)]
    assert all(d["images"] == 0 for d in days)

def test_images_per_day_counts_images_with_path_regardless_of_status(settings):
    from datetime import date
    st = StateStore(settings.state_dir)
    st.append(rec(id="g_a", ts="2026-11-09T10:00:00+00:00", status="ok",
                  images=[{"index": 0, "path": "/x/0.png"}, {"index": 1, "path": None}, {"index": 2}]))
    st.append(rec(id="g_b", ts="2026-11-09T11:00:00+00:00", status="error", code="no_image",
                  images=[{"index": 0, "path": "/x/1.png"}]))
    st.append(rec(id="g_c", ts="2026-11-10T09:00:00+00:00", status="ok",
                  images=[{"index": 0, "path": "/x/2.png"}]))
    days = {d["date"]: d["images"] for d in st.images_per_day(7, today=date(2026, 11, 10))}
    assert days["2026-11-09"] == 2 and days["2026-11-10"] == 1

def test_images_per_day_not_saved_origin_counts_saved_image_then_retry_the_rest(settings):
    from datetime import date
    st = StateStore(settings.state_dir)
    st.append(rec(id="g_a", ts="2026-11-09T10:00:00+00:00", status="generated_not_saved", code="generated_not_saved",
                  images=[{"index": 0, "path": "/x/0.png"}, {"index": 1, "path": None}]))
    st.append(rec(id="g_r", ts="2026-11-10T10:00:00+00:00", type="retry_download", parent_id="g_a",
                  status="ok", images=[{"index": 1, "path": "/x/1.png"}]))
    days = {d["date"]: d["images"] for d in st.images_per_day(7, today=date(2026, 11, 10))}
    assert days["2026-11-09"] == 1  # image 0 was already saved on the origin day
    assert days["2026-11-10"] == 1  # only image 1 is new on the retry day

def test_images_per_day_retry_counts_only_previously_unsaved(settings):
    from datetime import date
    st = StateStore(settings.state_dir)
    st.append(rec(id="g_a", ts="2026-11-08T10:00:00+00:00", status="ok",
                  images=[{"index": 0, "path": "/x/0.png"}, {"index": 1, "path": None}]))
    st.append(rec(id="g_r", ts="2026-11-09T10:00:00+00:00", type="retry_download", parent_id="g_a",
                  status="ok", images=[{"index": 0, "path": "/x/0b.png"}, {"index": 1, "path": "/x/1.png"}]))
    days = {d["date"]: d["images"] for d in st.images_per_day(7, today=date(2026, 11, 10))}
    assert days["2026-11-08"] == 1  # only index 0 was saved on the origin day
    assert days["2026-11-09"] == 1  # only index 1 is new on the retry day

def test_images_per_day_uses_local_time(settings, monkeypatch):
    import os, time
    from datetime import date
    orig_tz = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "Etc/GMT-14")  # UTC+14: 2026-11-09 12:00 UTC is 2026-11-10 02:00 local
        time.tzset()
        st = StateStore(settings.state_dir)
        st.append(rec(id="g_a", ts="2026-11-09T12:00:00+00:00", status="ok",
                      images=[{"index": 0, "path": "/x/0.png"}]))
        days = {d["date"]: d["images"] for d in st.images_per_day(7, today=date(2026, 11, 10))}
        assert days["2026-11-10"] == 1 and days["2026-11-09"] == 0
    finally:
        if orig_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = orig_tz
        time.tzset()


def test_list_since_compares_datetimes_across_zones(settings):
    st = StateStore(settings.state_dir)
    # == 2026-11-01T22:30Z (before since) but sorts after the since text
    st.append(rec(id="g_000000000001", conversation_id="c_1", ts="2026-11-02T00:30:00+02:00"))
    # == 2026-11-02T01:30Z (after since) but sorts before the since text
    st.append(rec(id="g_000000000002", conversation_id="c_2", ts="2026-11-01T23:30:00-02:00"))
    assert [r.id for r in st.list(since="2026-11-02T00:00:00+00:00")] == ["g_000000000002"]
    assert [r.id for r in st.list(since="2026-11-02")] == ["g_000000000002"]


def test_list_since_invalid_raises_invalid_query(settings):
    import pytest
    from musa.errors import GIAError
    with pytest.raises(GIAError) as ei:
        StateStore(settings.state_dir).list(since="not-a-date")
    assert ei.value.code == "invalid_query"


def test_list_limit_zero_or_negative_is_empty(settings):
    st = StateStore(settings.state_dir)
    st.append(rec())
    assert st.list(limit=0) == [] and st.list(limit=-3) == []
    assert len(st.list(limit=1)) == 1


def test_order_uses_datetimes_not_text_and_unparsable_ts_sorts_oldest(settings):
    st = StateStore(settings.state_dir)
    # UTC instants: bad < b(01:00Z) < a(01:30Z) ... but text order differs ("...-02:00" sorts before "...+00:00")
    st.append(rec(id="g_00000000000a", conversation_id="c_a", ts="2026-11-02T01:30:00+00:00"))
    st.append(rec(id="g_00000000000b", conversation_id="c_b", ts="2026-11-01T23:00:00-02:00"))  # 01:00Z
    st.append(rec(id="g_00000000000c", conversation_id="c_c", ts="garbage"))
    assert [r.id for r in st.visible_records()] == ["g_00000000000c", "g_00000000000b", "g_00000000000a"]
    assert [r.id for r in st.list(limit=10)] == ["g_00000000000a", "g_00000000000b", "g_00000000000c"]
