import io

import pytest
from PIL import Image

from musa.errors import GIAError
from musa.storage import check_copy_dest, copy_file, safe_output_dir, save_image, slugify


def png_bytes(w=64, h=48, color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_slugify():
    assert slugify("Hola Mundo! 2026") == "hola-mundo-2026"
    assert len(slugify("a" * 200)) <= 40


def test_safe_output_dir_rejects_escape(tmp_path):
    root = tmp_path / "generated"
    with pytest.raises(GIAError):
        safe_output_dir(root, "../..", None)
    with pytest.raises(GIAError):
        safe_output_dir(root, None, "/etc")
    p = safe_output_dir(root, "clienteA", "sesion1")
    assert p == (root / "clienteA" / "sesion1").resolve() and p.is_dir()


def test_save_image_png(tmp_path):
    data = png_bytes(1408, 768)
    img = save_image(data, output_root=tmp_path / "generated", project=None, subdir=None,
                     prompt="un gato astronauta", index=0, meta={"prompt": "un gato astronauta"},
                     write_sidecar=False, thumb_size=512)
    assert img.path.exists() and img.path.suffix == ".png"
    assert "un-gato-astronauta_0" in img.path.name
    assert img.width == 1408 and img.height == 768 and img.format == "png" and img.full_size is True
    assert img.thumbnail is not None
    with Image.open(io.BytesIO(img.thumbnail)) as t:
        assert max(t.size) <= 512
    # tEXt metadata inside the PNG
    with Image.open(img.path) as im:
        assert im.text.get("prompt") == "un gato astronauta"
    assert not img.path.with_suffix(".json").exists()  # sidecar off


def test_save_image_optional_sidecar(tmp_path):
    img = save_image(png_bytes(), output_root=tmp_path / "g", project=None, subdir=None,
                     prompt="x", index=1, meta={"a": 1}, write_sidecar=True, thumb_size=512)
    side = img.path.with_suffix(".json")
    assert side.exists() and '"a": 1' in side.read_text()


def test_save_image_same_second_no_collision(tmp_path):
    kw = dict(output_root=tmp_path / "g", project=None, subdir=None, prompt="p", meta={},
              write_sidecar=False, thumb_size=512)
    a = save_image(png_bytes(), index=0, **kw)
    b = save_image(png_bytes(), index=0, **kw)
    assert a.path != b.path


def test_save_image_jpeg_extension(tmp_path):
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, "JPEG")
    img = save_image(buf.getvalue(), output_root=tmp_path / "g", project=None, subdir=None, prompt="p",
                     index=0, meta={}, write_sidecar=False, thumb_size=512)
    assert img.path.suffix == ".jpg" and img.format == "jpeg"


def test_copy_dest_relative_to_cwd_inside_allowlist(settings, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert check_copy_dest("assets/", settings) == (tmp_path / "assets").resolve()


def test_copy_dest_outside_allowlist_rejected(settings, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for bad in ["/etc/x.png", "../outside.png", str(settings.state_dir / "x.png")]:
        with pytest.raises(GIAError) as e:
            check_copy_dest(bad, settings)
        assert e.value.code == "path_not_allowed"


def test_copy_file_dir_and_no_overwrite(tmp_path):
    src = tmp_path / "a.jpg"; src.write_bytes(b"x")
    d = tmp_path / "out"; d.mkdir()
    first = copy_file(src, d)
    second = copy_file(src, d)
    assert first == d / "a.jpg" and second == d / "a_1.jpg" and second.read_bytes() == b"x"
    named = copy_file(src, tmp_path / "new" / "hero.jpg")
    assert named == tmp_path / "new" / "hero.jpg" and named.exists()


def test_copy_file_never_writes_through_a_planted_symlink(tmp_path):
    src = tmp_path / "a.jpg"; src.write_bytes(b"x")
    d = tmp_path / "out"; d.mkdir()
    outside = tmp_path / "outside.jpg"            # does not exist: the symlink is dangling
    (d / "a.jpg").symlink_to(outside)
    out = copy_file(src, d)
    assert out == d / "a_1.jpg" and out.read_bytes() == b"x"
    assert not outside.exists()                    # nothing written outside the target dir
