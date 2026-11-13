# tests/test_refs.py
import pytest, respx, httpx
from pathlib import Path
from gemini_image_api.refs import resolve_refs, sniff_image_format, validate_local_ref, validate_trusted_image
from gemini_image_api.state import GenerationRecord
from gemini_image_api.errors import GIAError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32

def test_sniff():
    assert sniff_image_format(PNG) == "png"
    assert sniff_image_format(JPEG) == "jpeg"
    assert sniff_image_format(b"GIF89a" + b"\x00" * 10) == "gif"
    assert sniff_image_format(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8) == "webp"
    assert sniff_image_format(b"not an image") is None
    assert sniff_image_format(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>') == "svg"
    assert sniff_image_format(b'<?xml version="1.0"?><svg></svg>') == "svg"


def test_svg_ref_rejected_with_clear_message(settings, tmp_path):
    settings.allowed_ref_dirs = str(tmp_path)
    f = tmp_path / "logo.svg"
    f.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
    with pytest.raises(GIAError) as e:
        validate_local_ref(f, settings)
    assert e.value.code == "invalid_ref" and "SVG" in str(e.value) and "PNG or JPEG" in str(e.value)
    root = tmp_path / "up"; root.mkdir()
    g = root / "logo.svg"; g.write_bytes(f.read_bytes())
    with pytest.raises(GIAError) as e2:
        validate_trusted_image(g, root, settings)
    assert e2.value.code == "invalid_ref" and "SVG" in str(e2.value)

def test_local_ref_ok(settings, tmp_path):
    f = tmp_path / "foto.png"; f.write_bytes(PNG)
    # tmp_path no esta en allowed por defecto -> anyadir
    settings.allowed_ref_dirs = str(tmp_path)
    r = validate_local_ref(f, settings)
    assert r == f.resolve()

def test_local_ref_fuera_de_allowlist(settings, tmp_path):
    f = tmp_path / "foto.png"; f.write_bytes(PNG)
    with pytest.raises(GIAError) as e:
        validate_local_ref(f, settings)
    assert e.value.code == "ref_not_allowed"

def test_local_ref_no_imagen(settings, tmp_path):
    settings.allowed_ref_dirs = str(tmp_path)
    f = tmp_path / "x.txt"; f.write_bytes(b"hola")
    with pytest.raises(GIAError) as e:
        validate_local_ref(f, settings)
    assert e.value.code == "invalid_ref"

def test_local_ref_state_dir_prohibido(settings, tmp_path):
    f = settings.state_dir / "cookies" / "cookies.json"
    f.parent.mkdir(parents=True); f.write_bytes(b"{}")
    settings.allowed_ref_dirs = str(settings.state_dir)
    with pytest.raises(GIAError) as e:
        validate_local_ref(f, settings)
    assert e.value.code == "ref_not_allowed"

@respx.mock
async def test_resolve_refs_url_descarga_a_temp(settings, tmp_path, monkeypatch):
    # forzar DNS a 93.184.216.34 (publica de ejemplo)
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))])
    # the connection is pinned to the validated IP, so the mock matches the IP URL
    route = respx.get("https://93.184.216.34/foto.png").mock(return_value=httpx.Response(200, content=PNG))
    out = await resolve_refs(["https://example.com/foto.png"], settings, tmp_path)
    assert len(out) == 1 and out[0].path.suffix == ".png" and out[0].path.read_bytes() == PNG
    assert len(out[0].sha256) == 64
    assert route.calls.last.request.headers["host"] == "example.com"
    assert route.calls.last.request.extensions["sni_hostname"] == "example.com"
    assert route.calls.last.request.headers["user-agent"].startswith("gemini-image-api/")

@respx.mock
async def test_resolve_refs_url_ssrf_bloqueada(settings, tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))])
    respx.get("http://169.254.169.254/latest/meta-data").mock(return_value=httpx.Response(200, content=PNG))
    with pytest.raises(GIAError) as e:
        await resolve_refs(["http://169.254.169.254/latest/meta-data"], settings, tmp_path)
    assert e.value.code == "ssrf_blocked"

async def test_resolve_refs_max_count(settings, tmp_path):
    settings.allowed_ref_dirs = str(tmp_path)
    files = []
    for i in range(12):
        f = tmp_path / f"f{i}.png"; f.write_bytes(PNG); files.append(str(f))
    with pytest.raises(GIAError) as e:
        await resolve_refs(files, settings, tmp_path)
    assert e.value.code == "invalid_ref" and "max_refs" in str(e.value.details)

def _dns(monkeypatch, mapping):
    import socket
    def fake(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)

@respx.mock
async def test_redirect_to_blocked_ip(settings, tmp_path, monkeypatch):
    _dns(monkeypatch, {"example.com": "93.184.216.34", "meta.internal": "169.254.169.254"})
    respx.get("https://93.184.216.34/a.png").mock(
        return_value=httpx.Response(302, headers={"location": "http://meta.internal/latest"}))
    meta = respx.get("http://169.254.169.254/latest").mock(return_value=httpx.Response(200, content=PNG))
    with pytest.raises(GIAError) as e:
        await resolve_refs(["https://example.com/a.png"], settings, tmp_path)
    assert e.value.code == "ssrf_blocked" and not meta.called

@respx.mock
async def test_redirect_followed_to_public(settings, tmp_path, monkeypatch):
    _dns(monkeypatch, {"example.com": "93.184.216.34", "cdn.example.org": "93.184.216.35"})
    respx.get("https://93.184.216.34/a.png").mock(
        return_value=httpx.Response(302, headers={"location": "https://cdn.example.org/b.png"}))
    respx.get("https://93.184.216.35/b.png").mock(return_value=httpx.Response(200, content=PNG))
    out = await resolve_refs(["https://example.com/a.png"], settings, tmp_path)
    assert out[0].path.read_bytes() == PNG

@respx.mock
async def test_download_exceeds_max_bytes(settings, tmp_path, monkeypatch):
    _dns(monkeypatch, {"example.com": "93.184.216.34"})
    settings.max_ref_bytes = 100
    respx.get("https://93.184.216.34/big.png").mock(return_value=httpx.Response(200, content=PNG + b"\x00" * 500))
    with pytest.raises(GIAError) as e:
        await resolve_refs(["https://example.com/big.png"], settings, tmp_path)
    assert e.value.code == "invalid_ref"

@respx.mock
async def test_url_jpeg_extension(settings, tmp_path, monkeypatch):
    _dns(monkeypatch, {"example.com": "93.184.216.34"})
    respx.get("https://93.184.216.34/p").mock(return_value=httpx.Response(200, content=JPEG))
    out = await resolve_refs(["https://example.com/p"], settings, tmp_path)
    assert out[0].path.suffix == ".jpg"

async def test_cgnat_blocked(settings, tmp_path, monkeypatch):
    _dns(monkeypatch, {"example.com": "100.64.0.1"})
    with pytest.raises(GIAError) as e:
        await resolve_refs(["https://example.com/p.png"], settings, tmp_path)
    assert e.value.code == "ssrf_blocked"

def test_local_symlink_rejected(settings, tmp_path):
    settings.allowed_ref_dirs = str(tmp_path)
    real = tmp_path / "real.png"; real.write_bytes(PNG)
    link = tmp_path / "link.png"; link.symlink_to(real)
    with pytest.raises(GIAError) as e:
        validate_local_ref(link, settings)
    assert e.value.code == "ref_not_allowed"

def test_local_too_large(settings, tmp_path):
    settings.allowed_ref_dirs = str(tmp_path)
    settings.max_ref_bytes = 10
    f = tmp_path / "big.png"; f.write_bytes(PNG)
    with pytest.raises(GIAError) as e:
        validate_local_ref(f, settings)
    assert e.value.code == "invalid_ref"

def _trusted_root(tmp_path):
    root = tmp_path / "uploads"; root.mkdir()
    return root

def test_trusted_image_ok_inside_root(settings, tmp_path):
    root = _trusted_root(tmp_path)
    f = root / "u_0123456789ab.png"; f.write_bytes(PNG)
    assert validate_trusted_image(f, root, settings) == f.resolve()

def test_trusted_image_rejects_symlink(settings, tmp_path):
    root = _trusted_root(tmp_path)
    real = tmp_path / "real.png"; real.write_bytes(PNG)
    link = root / "u_0123456789ab.png"; link.symlink_to(real)
    with pytest.raises(GIAError) as e:
        validate_trusted_image(link, root, settings)
    assert e.value.code == "invalid_ref"

def test_trusted_image_rejects_outside_root(settings, tmp_path):
    root = _trusted_root(tmp_path)
    outside = tmp_path / "foto.png"; outside.write_bytes(PNG)
    with pytest.raises(GIAError) as e:
        validate_trusted_image(outside, root, settings)
    assert e.value.code == "invalid_ref"
    with pytest.raises(GIAError) as e2:
        validate_trusted_image(root / ".." / "foto.png", root, settings)
    assert e2.value.code == "invalid_ref"

def test_trusted_image_rejects_missing_file(settings, tmp_path):
    root = _trusted_root(tmp_path)
    with pytest.raises(GIAError) as e:
        validate_trusted_image(root / "u_0123456789ab.png", root, settings)
    assert e.value.code == "invalid_ref"

def test_trusted_image_rejects_too_large(settings, tmp_path):
    root = _trusted_root(tmp_path)
    settings.max_ref_bytes = 8
    f = root / "u_0123456789ab.png"; f.write_bytes(PNG)
    with pytest.raises(GIAError) as e:
        validate_trusted_image(f, root, settings)
    assert e.value.code == "invalid_ref"

def test_trusted_image_rejects_non_image(settings, tmp_path):
    root = _trusted_root(tmp_path)
    f = root / "u_0123456789ab.png"; f.write_bytes(b"not an image at all")
    with pytest.raises(GIAError) as e:
        validate_trusted_image(f, root, settings)
    assert e.value.code == "invalid_ref"

def _gen(path, **kw):
    base = dict(id="g_0123456789ab", ts="2026-11-09T00:00:00+00:00", type="generate", conversation_id="c_1",
                chat_metadata=["c"], prompt="p", style=None, style_text=None, aspect_hint=None, model=None,
                project=None, subdir=None, refs=[], images=[{"index": 0, "path": str(path)}],
                status="ok", code=None, gemini_text="", elapsed_s=1.0, parent_id=None)
    base.update(kw)
    return GenerationRecord(**base)

async def test_gen_ref_resolves_to_saved_image(settings, tmp_path):
    img = settings.output_root / "x.png"; img.parent.mkdir(parents=True, exist_ok=True); img.write_bytes(PNG)
    rec = _gen(img)
    out = await resolve_refs(["gen:g_0123456789ab"], settings, tmp_path, lookup={rec.id: rec}.get)
    assert out[0].path == img.resolve() and out[0].source == "gen:g_0123456789ab"

async def test_gen_ref_index_and_errors(settings, tmp_path):
    img = settings.output_root / "y.png"; img.parent.mkdir(parents=True, exist_ok=True); img.write_bytes(PNG)
    rec = _gen(img, images=[{"index": 0, "path": None}, {"index": 1, "path": str(img)}])
    lookup = {rec.id: rec}.get
    assert (await resolve_refs(["gen:g_0123456789ab#1"], settings, tmp_path, lookup=lookup))[0].path == img.resolve()
    for bad, needle in [("gen:g_0123456789ab", "not saved"), ("gen:g_0123456789ab#5", "index"),
                        ("gen:g_ffffffffffff", "unknown"), ("gen:nope", "format")]:
        with pytest.raises(GIAError) as e:
            await resolve_refs([bad], settings, tmp_path, lookup=lookup)
        assert e.value.code == "invalid_ref" and needle in str(e.value)

async def test_gen_ref_without_lookup_is_invalid(settings, tmp_path):
    with pytest.raises(GIAError) as e:
        await resolve_refs(["gen:g_0123456789ab"], settings, tmp_path)
    assert e.value.code == "invalid_ref"

async def test_gen_ref_to_deleted_image_is_invalid(settings, tmp_path):
    img = settings.output_root / "d.png"; img.parent.mkdir(parents=True, exist_ok=True); img.write_bytes(PNG)
    rec = _gen(img)
    deleted = {"g_0123456789ab#0"}
    with pytest.raises(GIAError) as e:
        await resolve_refs(["gen:g_0123456789ab"], settings, tmp_path, lookup={rec.id: rec}.get, is_deleted=deleted.__contains__)
    assert e.value.code == "invalid_ref" and "deleted" in str(e.value)
