import os
import stat
import time

import pytest

from musa.errors import GIAError
from musa.uploads import UploadStore


def test_save_png_returns_id_and_file_permissions(settings, png_bytes):
    store = UploadStore(settings.state_dir, settings)
    uid = store.save(png_bytes)
    assert uid.startswith("u_") and len(uid) == len("u_") + 12
    f = store.dir / f"{uid}.png"
    assert f.read_bytes() == png_bytes
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.dir.stat().st_mode) == 0o700


def test_save_rejects_non_image(settings):
    store = UploadStore(settings.state_dir, settings)
    with pytest.raises(GIAError) as e:
        store.save(b"this is definitely not an image")
    assert e.value.code == "invalid_image"


def test_save_converts_svg_to_png(settings):
    store = UploadStore(settings.state_dir, settings)
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60">'
           b'<rect width="120" height="60" fill="#E8B768"/></svg>')
    uid = store.save(svg)
    p = store.path(uid)
    assert p.suffix == ".png"
    assert p.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_save_rejects_broken_svg(settings, monkeypatch):
    import musa.uploads as up
    monkeypatch.setattr(up, "svg_to_png", lambda b: (_ for _ in ()).throw(ValueError("boom")))
    store = UploadStore(settings.state_dir, settings)
    with pytest.raises(GIAError) as e:
        store.save(b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
    assert e.value.code == "invalid_image" and "SVG" in str(e.value)
    assert list(store.dir.iterdir()) == []


def test_save_rejects_empty(settings):
    store = UploadStore(settings.state_dir, settings)
    with pytest.raises(GIAError) as e:
        store.save(b"")
    assert e.value.code == "invalid_image"


def test_save_rejects_over_20_mb(settings):
    store = UploadStore(settings.state_dir, settings)
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (UploadStore.MAX_BYTES - 8 + 1)
    assert len(big) > UploadStore.MAX_BYTES
    with pytest.raises(GIAError) as e:
        store.save(big)
    assert e.value.code == "invalid_image"


def test_path_rejects_bad_format_and_missing(settings, png_bytes):
    store = UploadStore(settings.state_dir, settings)
    store.save(png_bytes)
    for bad in ("", "u_xyz", "u_0123456789ABC", "../cookies/cookies.json", "u_0123456789ab"):
        with pytest.raises(GIAError) as e:
            store.path(bad)
        assert e.value.code == "unknown_upload", bad


def test_path_resolves_saved_upload(settings, png_bytes):
    store = UploadStore(settings.state_dir, settings)
    uid = store.save(png_bytes)
    assert store.path(uid) == store.dir / f"{uid}.png"


def test_purge_removes_only_old_files(settings, png_bytes):
    store = UploadStore(settings.state_dir, settings)
    old_id = store.save(png_bytes)
    old_file = store.path(old_id)
    old_ts = time.time() - 3600
    os.utime(old_file, (old_ts, old_ts))
    assert store.purge(max_age_s=600) == 1
    assert not old_file.exists()
    new_id = store.save(png_bytes)
    assert store.purge(max_age_s=600) == 0
    assert store.path(new_id).exists()
