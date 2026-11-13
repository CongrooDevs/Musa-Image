import json, pytest
from musa.errors import GIAError
from musa.library import LibraryStore

@pytest.fixture
def lib(settings):
    return LibraryStore(settings.state_dir)

def test_create_get_update_delete_collection(lib):
    c = lib.create_collection("Campaña otoño", "redes")
    assert c.id.startswith("col_") and len(c.id) == 12
    assert lib.get_collection("campaña OTOÑO") == c and lib.get_collection(c.id) == c
    lib.add("g_000000000001#0", c.id)
    u = lib.update_collection(c.id, name="Otoño 26")
    assert u.name == "Otoño 26" and u.id == c.id
    lib.delete_collection(c.id)
    assert lib.collections() == [] and lib.collections_of("g_000000000001#0") == []

def test_duplicate_or_empty_name_rejected(lib):
    lib.create_collection("A")
    for bad in ["a", "  ", ""]:
        with pytest.raises(GIAError) as e:
            lib.create_collection(bad)
        assert e.value.code == "invalid_collection"

def test_resolve_collection(lib):
    with pytest.raises(GIAError) as e:
        lib.resolve_collection("Nueva", create=False)
    assert e.value.code == "unknown_collection"
    c = lib.resolve_collection("Nueva", create=True)
    assert lib.resolve_collection("nueva", create=True) == c and len(lib.collections()) == 1

def test_membership_idempotent_and_members(lib):
    c = lib.create_collection("A"); d = lib.create_collection("B")
    assert lib.add("k#0", c.id) == [c.id]
    assert lib.add("k#0", c.id) == [c.id]
    assert sorted(lib.add("k#0", d.id)) == sorted([c.id, d.id])
    assert lib.members(c.id) == ["k#0"]
    assert lib.remove("k#0", c.id) == [d.id] and lib.remove("k#0", c.id) == [d.id]

def test_favorites_and_deleted(lib):
    c = lib.create_collection("A")
    lib.add("k#0", c.id); lib.set_favorite("k#0", True)
    assert lib.is_favorite("k#0") and lib.favorites() == {"k#0"}
    lib.mark_deleted("k#0")
    assert lib.is_deleted("k#0") and not lib.is_favorite("k#0") and lib.members(c.id) == []

def test_unknown_collection_ops(lib):
    for op in (lambda: lib.add("k#0", "col_00000000"), lambda: lib.update_collection("col_00000000", name="x"),
               lambda: lib.delete_collection("col_00000000")):
        with pytest.raises(GIAError) as e:
            op()
        assert e.value.code == "unknown_collection"

def test_corrupt_file_is_treated_as_empty_and_renamed(lib, settings):
    (settings.state_dir / "library.json").write_text("{not json")
    assert lib.collections() == []
    assert list(settings.state_dir.glob("library.json.corrupt-*"))


def test_invalid_shape_is_treated_as_corrupt(lib, settings):
    bad_payloads = [
        {"version": 1, "collections": [{"id": "col_00000000"}], "membership": {}, "favorites": [], "deleted": []},
        {"version": 1, "collections": [{"name": "sin id"}], "membership": {}, "favorites": [], "deleted": []},
        {"version": 1, "collections": "nope", "membership": {}, "favorites": [], "deleted": []},
        {"version": 1, "collections": [], "membership": [], "favorites": [], "deleted": []},
        {"version": 1, "collections": [], "membership": {}, "favorites": "x", "deleted": []},
        {"version": 1, "collections": [], "membership": {}, "favorites": [], "deleted": {}},
    ]
    for payload in bad_payloads:
        (settings.state_dir / "library.json").write_text(json.dumps(payload))
        assert lib.collections() == []
    assert list(settings.state_dir.glob("library.json.corrupt-*"))
def test_persisted_shape(lib, settings):
    c = lib.create_collection("A"); lib.add("k#0", c.id); lib.set_favorite("k#1", True)
    data = json.loads((settings.state_dir / "library.json").read_text())
    assert data["version"] == 1 and data["membership"] == {"k#0": [c.id]} and data["favorites"] == ["k#1"]
    assert lib.mtime() > 0

def test_activity_records_create_add_remove_rename_newest_first(lib):
    c = lib.create_collection("A")
    lib.add("k#0", c.id)
    lib.add("k#0", c.id)  # already a member: not logged
    lib.update_collection(c.id, name="B")
    lib.update_collection(c.id, description="d")  # not a rename: not logged
    lib.remove("k#0", c.id)
    lib.remove("k#0", c.id)  # not a member: not logged
    acts = lib.activity()
    assert [(a["kind"], a.get("key")) for a in acts] == [("removed", "k#0"), ("renamed", None),
                                                         ("added", "k#0"), ("created", None)]
    assert all(a["collection"] == c.id and a["ts"] for a in acts)
    assert acts[0]["ts"] >= acts[-1]["ts"]
    assert next(a for a in acts if a["kind"] == "created")["name"] == "A"
    assert next(a for a in acts if a["kind"] == "renamed")["name"] == "B"

def test_activity_filter_by_collection_and_limit(lib):
    c = lib.create_collection("A")
    d = lib.create_collection("B")
    lib.add("k#0", c.id)
    lib.add("k#0", d.id)
    assert {a["collection"] for a in lib.activity(c.id)} == {c.id}
    assert [a["kind"] for a in lib.activity(c.id)] == ["added", "created"]
    assert len(lib.activity(limit=1)) == 1

def test_activity_cap_is_500(lib):
    c = lib.create_collection("A")
    for i in range(505):
        lib.add(f"k#{i}", c.id)
    acts = lib.activity(limit=1000)
    assert len(acts) == 500
    assert acts[0]["key"] == "k#504" and acts[-1]["key"] == "k#5"

def test_delete_collection_drops_its_activity(lib):
    c = lib.create_collection("A")
    d = lib.create_collection("B")
    lib.add("k#0", c.id)
    lib.delete_collection(c.id)
    assert all(a["collection"] != c.id for a in lib.activity())
    assert any(a["collection"] == d.id for a in lib.activity())

def test_library_json_without_activity_loads_and_creates_it(lib, settings):
    c = lib.create_collection("A")
    data = json.loads((settings.state_dir / "library.json").read_text())
    assert isinstance(data["activity"], list)
    del data["activity"]
    (settings.state_dir / "library.json").write_text(json.dumps(data))
    assert lib.collections() == [c] and lib.activity() == []
    lib.add("k#0", c.id)
    assert [a["kind"] for a in lib.activity()] == ["added"]

def test_activity_not_a_list_is_treated_as_corrupt(lib, settings):
    (settings.state_dir / "library.json").write_text(json.dumps(
        {"version": 1, "collections": [], "membership": {}, "favorites": [], "deleted": [], "activity": {}}))
    assert lib.collections() == [] and list(settings.state_dir.glob("library.json.corrupt-*"))
