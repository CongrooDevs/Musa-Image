import json
from pathlib import Path

import pytest

from musa.style_gems import StyleGemStore, default_instructions, gem_name
from musa.styles import Style


def _style(**kw) -> Style:
    base = dict(id="impresionismo", name="Impresionismo", family="arte", description="luz suave",
                append="pinceladas sueltas", ref=None, credit="", source="builtin")
    return Style(**{**base, **kw})


def test_gem_name_truncates_to_60():
    s = _style(name="X" * 80)
    assert gem_name(s) == f"Musa · {'X' * 80}"[:60]
    assert len(gem_name(s)) == 60
    assert gem_name(_style()) == "Musa · Impresionismo"


def test_default_instructions_template_without_ref():
    text = default_instructions(_style())
    assert text == (
        'You are an image-generation assistant that always works in the "Impresionismo" style.\n'
        "Style description: luz suave\n"
        "Visual rules: pinceladas sueltas\n"
        "Always answer by generating an image. Do not search the web. "
        "Keep any extra text to one short sentence.\n"
        "If the user attaches other images, keep their subject and layout and apply this style."
    )


def test_default_instructions_with_ref_and_credit():
    s = _style(ref=Path("/tmp/ref.jpg"), credit="Ada Lovelace")
    line = [l for l in default_instructions(s).splitlines() if l.startswith("The first attached")]
    assert line == ["The first attached image is the style reference (Ada Lovelace); "
                    "match its palette, brushwork and composition principles, never its subject."]


def test_default_instructions_with_ref_empty_credit_omits_parenthetical():
    s = _style(ref=Path("/tmp/ref.jpg"), credit="")
    line = [l for l in default_instructions(s).splitlines() if l.startswith("The first attached")]
    assert line == ["The first attached image is the style reference; "
                    "match its palette, brushwork and composition principles, never its subject."]


def test_store_roundtrip(tmp_path):
    store = StyleGemStore(tmp_path)
    assert store.get("a") is None and store.all() == {} and store.mtime() == 0.0
    entry = {"gem_id": "g1", "name": "Musa · A", "instructions": "i", "custom": False,
             "created": "t", "updated": "t"}
    store.put("a", entry)
    assert store.get("a") == entry
    assert store.all() == {"a": entry}
    assert store.mtime() > 0
    data = json.loads((tmp_path / "gems.json").read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["gems"]["a"]["gem_id"] == "g1"
    store.remove("a")
    assert store.get("a") is None and store.all() == {}
    store.remove("a")  # removing a missing entry is a no-op


def test_store_corrupt_file_becomes_empty_and_is_renamed(tmp_path):
    f = tmp_path / "gems.json"
    f.write_text("{not json", encoding="utf-8")
    store = StyleGemStore(tmp_path)
    assert store.all() == {}
    leftovers = list(tmp_path.glob("gems.json.corrupt-*"))
    assert len(leftovers) == 1 and not f.exists()


def test_store_wrong_shape_is_corrupt(tmp_path):
    (tmp_path / "gems.json").write_text(json.dumps({"version": 1, "gems": [1, 2]}), encoding="utf-8")
    store = StyleGemStore(tmp_path)
    assert store.all() == {}
    assert len(list(tmp_path.glob("gems.json.corrupt-*"))) == 1


def test_store_write_is_atomic_no_tmp_left(tmp_path):
    store = StyleGemStore(tmp_path)
    store.put("a", {"gem_id": "g"})
    store.put("b", {"gem_id": "h"})
    assert not (tmp_path / "gems.json.tmp").exists()
    assert set(store.all()) == {"a", "b"}


def test_store_missing_file_defaults(tmp_path):
    store = StyleGemStore(tmp_path / "sub")
    assert store.all() == {}
    assert (tmp_path / "sub").is_dir()
