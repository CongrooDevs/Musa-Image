import json
import os
import threading

import pytest

from musa.errors import GIAError
from musa.prompts import PromptStore


def test_create_and_list(tmp_path):
    s = PromptStore(tmp_path)
    p = s.create("  Portrait ", " a face ")
    assert p["id"].startswith("pr_") and len(p["id"]) == 11
    assert p["name"] == "Portrait" and p["text"] == "a face" and p["uses"] == 0
    assert s.list() == [p]
    data = json.loads((tmp_path / "prompts.json").read_text())
    assert data["version"] == 1 and data["prompts"][0]["id"] == p["id"]


def test_list_order_uses_then_recent(tmp_path):
    s = PromptStore(tmp_path)
    a = s.create("a", "x")
    b = s.create("b", "x")
    c = s.create("c", "x")
    assert [p["id"] for p in s.list()] == [c["id"], b["id"], a["id"]]
    s.use(a["id"])
    s.use(a["id"])
    s.use(b["id"])
    assert [p["id"] for p in s.list()] == [a["id"], b["id"], c["id"]]


def test_partial_update(tmp_path):
    s = PromptStore(tmp_path)
    p = s.create("n", "t")
    assert s.update(p["id"], name="m")["text"] == "t"
    u = s.update(p["id"], text="u")
    assert u["name"] == "m" and u["text"] == "u" and u["created"] == p["created"]
    s.update(p["id"], name="M")  # same prompt, case-only rename allowed
    assert s.list()[0]["name"] == "M"


def test_delete_and_unknown(tmp_path):
    s = PromptStore(tmp_path)
    p = s.create("n", "t")
    s.delete(p["id"])
    assert s.list() == []
    for call in (lambda: s.delete("pr_x"), lambda: s.use("pr_x"), lambda: s.update("pr_x", name="a")):
        with pytest.raises(GIAError) as e:
            call()
        assert e.value.code == "unknown_prompt"


def test_use_increments(tmp_path):
    s = PromptStore(tmp_path)
    p = s.create("n", "t")
    assert s.use(p["id"])["uses"] == 1
    assert s.use(p["id"])["uses"] == 2
    assert PromptStore(tmp_path).list()[0]["uses"] == 2


@pytest.mark.parametrize("name,text", [("", "t"), ("   ", "t"), ("n", ""), ("n", "  "),
                                       ("x" * 81, "t"), ("n", "x" * 4001), (None, "t")])
def test_invalid_create(tmp_path, name, text):
    s = PromptStore(tmp_path)
    with pytest.raises(GIAError) as e:
        s.create(name, text)
    assert e.value.code == "invalid_prompt"
    assert s.list() == []


def test_limits_boundaries(tmp_path):
    s = PromptStore(tmp_path)
    s.create("x" * 80, "y" * 4000)
    with pytest.raises(GIAError):
        s.update(s.list()[0]["id"], name="x" * 81)
    with pytest.raises(GIAError):
        s.update(s.list()[0]["id"], text="")


def test_max_prompts(tmp_path):
    s = PromptStore(tmp_path)
    for i in range(200):
        s.create(f"p{i}", "t")
    with pytest.raises(GIAError) as e:
        s.create("one more", "t")
    assert e.value.code == "invalid_prompt"
    assert len(s.list()) == 200


def test_duplicate_name_case_insensitive(tmp_path):
    s = PromptStore(tmp_path)
    s.create("Hero", "t")
    with pytest.raises(GIAError) as e:
        s.create("hERO", "t2")
    assert e.value.code == "invalid_prompt"
    other = s.create("Other", "t")
    with pytest.raises(GIAError):
        s.update(other["id"], name="hero")
    assert len(s.list()) == 2


@pytest.mark.parametrize("content", ["{not json", "[]", '{"version":1,"prompts":{}}',
                                     '{"version":1,"prompts":[{"id":"pr_1"}]}'])
def test_corrupt_file_moved_aside(tmp_path, content):
    (tmp_path / "prompts.json").write_text(content)
    s = PromptStore(tmp_path)
    assert s.list() == []
    assert list(tmp_path.glob("prompts.json.corrupt-*"))
    s.create("n", "t")
    assert len(PromptStore(tmp_path).list()) == 1


def test_concurrent_instances(tmp_path):
    stores = [PromptStore(tmp_path), PromptStore(tmp_path)]
    errs = []

    def work(s, tag):
        try:
            for i in range(15):
                s.create(f"{tag}{i}", "t")
        except Exception as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=work, args=(s, t)) for s, t in zip(stores, "ab")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    assert len(PromptStore(tmp_path).list()) == 30


def test_mtime_changes(tmp_path):
    s = PromptStore(tmp_path)
    assert s.mtime() == 0.0
    p = s.create("n", "t")
    first = s.mtime()
    assert first > 0
    os.utime(s.file, (first - 100, first - 100))
    old = s.mtime()
    s.use(p["id"])
    assert s.mtime() > old


def test_mtime_tolerates_stat_race(tmp_path, monkeypatch):
    s = PromptStore(tmp_path)
    s.create("n", "t")

    def racing(self, *a, **k):
        raise FileNotFoundError("renamed away")

    monkeypatch.setattr(type(s.file), "stat", racing)
    assert s.mtime() == 0.0


def test_file_is_private_and_tmp_cleaned_on_failure(tmp_path, monkeypatch):
    s = PromptStore(tmp_path)
    s.create("n", "t")
    assert (s.file.stat().st_mode & 0o777) == 0o600

    def boom(*a, **k):
        raise RuntimeError("disk")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(RuntimeError):
        s.create("m", "t")
    assert not list(tmp_path.glob("*.tmp"))


def test_missing_version_is_filled(tmp_path):
    (tmp_path / "prompts.json").write_text('{"prompts": []}')
    s = PromptStore(tmp_path)
    s.create("n", "t")
    assert json.loads(s.file.read_text())["version"] == 1


def test_list_tiebreak_same_created_and_uses(tmp_path):
    rows = [{"id": f"pr_{i}", "name": f"n{i}", "text": "t", "created": "2026-01-01T00:00:00+00:00", "uses": 0}
            for i in range(3)]
    (tmp_path / "prompts.json").write_text(json.dumps({"version": 1, "prompts": rows}))
    assert [p["id"] for p in PromptStore(tmp_path).list()] == ["pr_2", "pr_1", "pr_0"]


def test_update_noop_does_not_write(tmp_path):
    s = PromptStore(tmp_path)
    p = s.create("n", "t")
    os.utime(s.file, (1000, 1000))
    assert s.update(p["id"]) == p
    assert s.mtime() == 1000
    with pytest.raises(GIAError) as e:
        s.update("pr_nope")
    assert e.value.code == "unknown_prompt"


def test_two_corruptions_same_second_keep_both(tmp_path):
    s = PromptStore(tmp_path)
    for _ in range(2):
        s.file.write_text("{bad")
        assert s.list() == []
    assert len(list(tmp_path.glob("prompts.json.corrupt-*"))) == 2


def test_id_shaped_name_rejected(tmp_path):
    s = PromptStore(tmp_path)
    with pytest.raises(GIAError) as e:
        s.create("pr_12345678", "t")
    assert e.value.code == "invalid_prompt" and e.value.details["prompt"] == "pr_12345678"
    p = s.create("ok", "t")
    with pytest.raises(GIAError) as e:
        s.update(p["id"], name="pr_deadbeef")
    assert e.value.code == "invalid_prompt"
    s.create("pr_short", "t")  # not id-shaped


def test_error_details_use_prompt_key(tmp_path):
    s = PromptStore(tmp_path)
    s.create("Dup", "t")
    with pytest.raises(GIAError) as e:
        s.create("dup", "t")
    assert e.value.details == {"prompt": "dup"}
    with pytest.raises(GIAError) as e:
        s.use("pr_00000000")
    assert e.value.details == {"prompt": "pr_00000000"}
