import pytest
import yaml
from gemini_image_api.errors import GIAError
from gemini_image_api.style_store import UserStyleStore

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_create_text_only_and_unique_ids(settings):
    st = UserStyleStore(settings.state_dir)
    a = st.create(name="Cine Neón", description="neón", append="")
    b = st.create(name="Cine Neón", description="otra")
    assert (a.id, b.id) == ("cine-neon", "cine-neon-2") and a.append == "neón" and a.ref is None


def test_taken_ids_are_respected(settings):
    s = UserStyleStore(settings.state_dir).create(name="anime", taken_ids={"anime"})
    assert s.id == "anime-2"


def test_create_with_image_and_delete(settings):
    st = UserStyleStore(settings.state_dir)
    s = st.create(name="Ref", image=(PNG, "x.png"))
    assert s.ref.is_file() and s.ref.parent == st.path.parent and s.ref.suffix == ".png"
    st.delete(s.id)
    assert not s.ref.exists()
    on_disk = yaml.safe_load(st.path.read_text()) or {}
    assert "ref" not in (on_disk.get("styles") or {})


@pytest.mark.parametrize("bad", [(b"not an image", "x.png"), (PNG + b"\x00" * (21 * 1024 * 1024), "big.png")])
def test_bad_upload_rejected(settings, bad):
    with pytest.raises(GIAError) as e:
        UserStyleStore(settings.state_dir).create(name="x", image=bad)
    assert e.value.code == "invalid_style"


def test_create_with_svg_converts_to_png(settings):
    st = UserStyleStore(settings.state_dir)
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="80">'
           b'<rect width="100" height="80" fill="#E8B768"/></svg>')
    s = st.create(name="Logo vector", image=(svg, "logo.svg"))
    assert s.ref is not None and s.ref.suffix == ".png"
    assert s.ref.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_empty_name_and_unknown_delete(settings):
    st = UserStyleStore(settings.state_dir)
    for op in (lambda: st.create(name="  "), lambda: st.delete("nope")):
        with pytest.raises(GIAError) as e:
            op()
        assert e.value.code == "invalid_style"

def test_delete_does_not_unlink_outside_styles_dir(settings):
    st = UserStyleStore(settings.state_dir)
    st.path.write_text(yaml.safe_dump({"styles": {"x": {"name": "X", "ref": "../victim.txt"}}}))
    victim = settings.state_dir / "victim.txt"
    victim.write_text("keep me")
    st.delete("x")
    assert victim.read_text() == "keep me"
    assert "x" not in (yaml.safe_load(st.path.read_text()) or {}).get("styles", {})


def test_update_fields_keeps_id_and_image(settings):
    st = UserStyleStore(settings.state_dir)
    s = st.create(name="A", description="d", append="a", image=(PNG, "x.png"))
    u = st.update(s.id, name=" B ", description="d2", append="a2", family="foto")
    assert (u.id, u.name, u.description, u.append, u.family) == (s.id, "B", "d2", "a2", "foto")
    assert u.ref == s.ref and u.ref.is_file()
    assert yaml.safe_load(st.path.read_text())["styles"][s.id]["name"] == "B"


def test_update_replaces_image_and_removes_old(settings):
    st = UserStyleStore(settings.state_dir)
    s = st.create(name="A", image=(PNG, "x.png"))
    jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    u = st.update(s.id, image=(jpg, "y.jpg"))
    assert u.ref.suffix == ".jpg" and u.ref.is_file() and not s.ref.exists()
    assert not list(st.dir.glob(".*.tmp"))


def test_update_remove_image(settings):
    st = UserStyleStore(settings.state_dir)
    s = st.create(name="A", image=(PNG, "x.png"))
    u = st.update(s.id, remove_image=True)
    assert u.ref is None and not s.ref.exists()
    assert "ref" not in yaml.safe_load(st.path.read_text())["styles"][s.id]


def test_update_does_not_unlink_old_ref_outside_dir(settings):
    st = UserStyleStore(settings.state_dir)
    st.path.write_text(yaml.safe_dump({"styles": {"x": {"name": "X", "ref": "../victim.txt"}}}))
    victim = settings.state_dir / "victim.txt"
    victim.write_text("keep")
    st.update("x", image=(PNG, "n.png"))
    assert victim.read_text() == "keep"
    st.update("x", remove_image=True)
    assert victim.read_text() == "keep"


def test_update_unknown_id(settings):
    st = UserStyleStore(settings.state_dir)
    with pytest.raises(GIAError) as e:
        st.update("nope", name="z")
    assert e.value.code == "unknown_style"


def test_update_same_resolved_ref_is_not_deleted(settings):
    st = UserStyleStore(settings.state_dir)
    s = st.create(name="A", image=(PNG, "x.png"))
    data = yaml.safe_load(st.path.read_text())
    data["styles"][s.id]["ref"] = f"./{s.id}.png"
    st.path.write_text(yaml.safe_dump(data))
    u = st.update(s.id, image=(PNG, "y.png"))
    assert u.ref.is_file()


def test_update_write_failure_removes_new_image(settings, monkeypatch):
    st = UserStyleStore(settings.state_dir)
    s = st.create(name="A", image=(PNG, "x.png"))
    def boom(_data):
        raise OSError("disk full")
    monkeypatch.setattr(st, "_write", boom)
    with pytest.raises(OSError):
        st.update(s.id, image=(b"\xff\xd8\xff\xe0" + b"\x00" * 64, "y.jpg"))
    assert s.ref.is_file() and not (st.dir / f"{s.id}.jpg").exists()


def test_update_write_failure_same_extension_keeps_old_image(settings, monkeypatch):
    st = UserStyleStore(settings.state_dir)
    s = st.create(name="A", image=(PNG, "x.png"))
    before = s.ref.read_bytes()
    def boom(_data):
        raise OSError("disk full")
    monkeypatch.setattr(st, "_write", boom)
    with pytest.raises(OSError):
        st.update(s.id, image=(PNG + b"\x01" * 16, "y.png"))
    assert s.ref.read_bytes() == before
    assert not list(st.dir.glob(".*.tmp"))


def test_family_defaults_to_otros_on_create_and_update(settings):
    st = UserStyleStore(settings.state_dir)
    s = st.create(name="A", family="  ")
    assert s.family == "otros"
    assert yaml.safe_load(st.path.read_text())["styles"][s.id]["family"] == "otros"
    st.update(s.id, family="foto")
    assert st.update(s.id, family=" ").family == "otros"
    assert yaml.safe_load(st.path.read_text())["styles"][s.id]["family"] == "otros"


def test_update_svg_converted_and_bad_image_rejected(settings):
    st = UserStyleStore(settings.state_dir)
    s = st.create(name="A")
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
           b'<rect width="10" height="10" fill="#E8B768"/></svg>')
    u = st.update(s.id, image=(svg, "a.svg"))
    assert u.ref.suffix == ".png" and u.ref.read_bytes().startswith(b"\x89PNG")
    with pytest.raises(GIAError) as e:
        st.update(s.id, image=(b"nope", "a.png"))
    assert e.value.code == "invalid_style"
    with pytest.raises(GIAError):
        st.update(s.id, name="  ")
