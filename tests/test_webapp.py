import asyncio
import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from gemini_image_api.webapp import create_app
from gemini_image_api.client import Backend, GenerateResult
from gemini_image_api.library import LibraryStore
from gemini_image_api.service import ImageService
from gemini_image_api.state import StateStore, GenerationRecord
from gemini_image_api.storage import SavedImage
from gemini_image_api.style_store import UserStyleStore

PORT = 8765
JPG = b"\xff\xd8\xff" + b"0" * 32
H = {"host": f"127.0.0.1:{PORT}"}
M = {**H, "origin": f"http://127.0.0.1:{PORT}"}

QUOTA = {"tier": "PRO", "window_5h_pct": 8.0, "weekly_pct": 12.0,
         "resets_at_5h": "2026-11-10T03:28:00+00:00", "resets_at_weekly": "2026-11-17T00:00:00+00:00"}


class FakeBackend(Backend):
    """No network: status()/quota() are counted fakes; generate() appends a real
    record and returns a real saved image, gated by an asyncio.Event."""

    def __init__(self, settings, img: Path):
        self.settings = settings
        self.img = img
        self.gate = asyncio.Event()
        self.gate.set()
        self.status_calls = 0
        self.quota_calls = 0
        self.valid = True
        self.cookie_present = True
        self.gems: dict[str, dict] = {}   # in-memory Gems (task 4)
        self._gem_seq = 0
        self.quota_data = dict(QUOTA)
        self.models_raw = [
            {"model_name": "gemini-flash", "available": True, "display_name": "Respuestas rápidas"},
            {"model_name": "gemini-pro", "available": True, "display_name": "Razonamiento complejo"},
            {"model_name": "gemini-carbon", "available": True},
            {"model_name": "gemini-4-hydrogen", "available": True, "display_name": "Nuevo"},
            {"model_name": "gemini-x-future", "available": True},
            {"model_name": "gemini-flash-lite", "available": False},
        ]
        self.models_calls = 0
        self.models_fail = False
        self.aliases: dict[str, str] = {}

    async def list_models(self):
        self.models_calls += 1
        if self.models_fail:
            raise RuntimeError("upstream down")
        return [dict(m) for m in self.models_raw]

    async def resolve_model_name(self, name):
        return self.aliases.get(name, name)

    async def status(self):
        self.status_calls += 1
        if not self.valid:
            return {"cookie_present": self.cookie_present, "valid": False,
                    "code": "auth_expired", "message": "session expired"}
        return {"cookie_present": self.cookie_present, "valid": True,
                "account_status": "AVAILABLE", "quota": dict(self.quota_data)}

    async def quota(self):
        self.quota_calls += 1
        return dict(self.quota_data)

    async def generate(self, prompt, refs, *, model, **kw):
        await self.gate.wait()
        st = StateStore(self.settings.state_dir)
        rec = GenerationRecord(id=StateStore.new_id("g"), ts=datetime.now(timezone.utc).isoformat(),
                               type="generate", conversation_id="c_1", chat_metadata=["c"],
                               prompt=prompt, style=None, style_text=None,
                               aspect_hint=kw.get("aspect_hint"), model=model, project=None,
                               subdir=None, refs=[], images=[{"index": 0, "path": str(self.img)}],
                               status="ok", code=None, gemini_text="done", elapsed_s=2.0,
                               parent_id=None, user_prompt=prompt)
        st.append(rec)
        img = SavedImage(self.img, 800, 600, self.img.stat().st_size, "JPEG", True, None)
        return GenerateResult("ok", None, "c_1", [img],
                              "done", 2.0, ["c"], record_id=rec.id)

    async def create_gem(self, name, instructions, description=""):
        self._gem_seq += 1
        gid = f"gem_{self._gem_seq:03d}"
        self.gems[gid] = {"name": name, "instructions": instructions, "description": description}
        return gid

    async def update_gem(self, gem_id, name, instructions, description=""):
        if gem_id in self.gems:
            self.gems[gem_id].update(name=name, instructions=instructions, description=description)

    async def delete_gem(self, gem_id):
        self.gems.pop(gem_id, None)

    async def gem_exists(self, gem_id):
        return gem_id in self.gems


@pytest.fixture
def env(settings, tmp_path):
    from PIL import Image
    settings.styles_file = None
    st, lib, us = StateStore(settings.state_dir), LibraryStore(settings.state_dir), UserStyleStore(settings.state_dir)
    settings.output_root.mkdir(parents=True, exist_ok=True)
    p = settings.output_root / "a.jpg"; Image.new("RGB", (800, 600), "red").save(p, "JPEG")
    st.append(GenerationRecord(id="g_00000000000a", ts="2026-11-10T00:00:00+00:00", type="generate", conversation_id="c_1",
        chat_metadata=["c"], prompt="Generate an image (do not search the web): un gato", style=None, style_text=None,
        aspect_hint=None, model="gemini-flash", project=None, subdir=None, refs=[], images=[{"index": 0, "path": str(p), "width": 800, "height": 600}],
        status="ok", code=None, gemini_text="", elapsed_s=1.0, parent_id=None, user_prompt="un gato"))
    svc = ImageService(FakeBackend(settings, p), settings, st, lib, us)
    app = create_app(settings=settings, service=svc, library=lib, user_styles=us, key="k3y", port=PORT, poll_interval=0.05)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as c:  # one portal: the queue worker survives across requests
        c.cookies.set("gia_key", "k3y")
        yield c, settings, lib, p
        app.state.stop_event.set()
        portal = getattr(c, "portal", None)
        if portal is not None:
            portal.call(app.state.ctx.aclose)


def test_requires_key_host_and_origin(env):
    c, *_ = env
    c2 = TestClient(c.app, base_url=f"http://127.0.0.1:{PORT}")
    assert c2.get("/api/state", headers=H).status_code == 401
    assert c.get("/api/state", headers={"host": "evil.com"}).status_code == 403
    assert c.post("/api/collections", json={"name": "x"}, headers={**H, "origin": "http://evil.com"}).status_code == 403
    assert c.post("/api/collections", json={"name": "x"}, headers=H).status_code == 403   # no Origin
    assert c2.get("/static/app.css", headers=H).status_code == 200                      # static: no key


def test_key_bootstrap_sets_cookie(env):
    c, *_ = env
    c2 = TestClient(c.app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
    r = c2.get("/?key=k3y", headers=H)
    assert r.status_code == 303 and "gia_key=k3y" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]


def test_generations_item_shape(env):
    c, *_ = env
    d = c.get("/api/generations", headers=H).json()
    assert d["total"] == 1
    it = d["items"][0]
    assert it["key"] == "g_00000000000a#0" and it["prompt"] == "un gato" and it["style_name"] is None
    assert it["ratio"] is None  # seeded record has no aspect_hint
    assert it["ref"] == "gen:g_00000000000a#0" and it["favorite"] is False and it["collections"] == []
    assert c.get(it["image_url"], headers=H).status_code == 200
    t = c.get(it["thumb_url"], headers=H)
    assert t.status_code == 200 and t.headers["content-type"] == "image/jpeg"


def test_never_serves_paths_outside_output_root(env, settings, tmp_path):
    c, s, lib, _ = env
    outside = tmp_path / "secret.jpg"; outside.write_bytes(JPG)
    StateStore(s.state_dir).append(GenerationRecord(id="g_00000000000b", ts="2026-11-10T00:01:00+00:00", type="generate",
        conversation_id="c_2", chat_metadata=["c"], prompt="p", style=None, style_text=None, aspect_hint=None, model=None,
        project=None, subdir=None, refs=[], images=[{"index": 0, "path": str(outside)}], status="ok", code=None,
        gemini_text="", elapsed_s=1.0, parent_id=None))
    assert c.get("/api/image/g_00000000000b/0", headers=H).status_code == 404
    assert c.get("/api/image/..%2f..%2fetc/0", headers=H).status_code == 404


def test_favorites_collections_and_delete(env):
    c, s, lib, p = env
    assert c.put("/api/favorites/g_00000000000a/0", headers=M).json()["favorite"] is True
    col = c.post("/api/collections", json={"name": "Otoño"}, headers=M)
    assert col.status_code == 201
    cid = col.json()["id"]
    assert c.post("/api/collections", json={"name": "otoño"}, headers=M).status_code == 409
    assert c.post(f"/api/collections/{cid}/items", json={"key": "g_00000000000a#0"}, headers=M).json()["collections"] == [cid]
    lst = c.get("/api/collections", headers=H).json()
    assert lst[0]["count"] == 1 and len(lst[0]["covers"]) == 1
    assert c.get(f"/api/generations?collection={cid}", headers=H).json()["total"] == 1
    assert c.delete("/api/images/g_00000000000a/0", headers=M).json()["deleted"] is True
    assert not p.exists() and c.get("/api/generations", headers=H).json()["total"] == 0
    assert c.get("/api/image/g_00000000000a/0", headers=H).status_code == 404


def test_styles_three_ways_and_builtin_protected(env):
    c, *_ = env
    a = c.post("/api/styles", json={"name": "Neón", "description": "neón"}, headers=M)
    assert a.status_code == 201 and a.json()["source"] == "user" and a.json()["has_ref"] is False
    assert a.json()["append"] == "neón"
    assert next(x for x in c.get("/api/styles", headers=H).json() if x["id"] == "neon")["append"] == "neón"
    b = c.post("/api/styles", json={"name": "Desde galería", "from_key": "g_00000000000a#0"}, headers=M)
    assert b.json()["has_ref"] is True and c.get(b.json()["cover_url"], headers=H).status_code == 200
    up = c.post("/api/styles", data={"name": "Subida"}, files={"file": ("x.jpg", JPG, "image/jpeg")}, headers=M)
    assert up.status_code == 201 and up.json()["has_ref"] is True
    bad = c.post("/api/styles", data={"name": "Mala"}, files={"file": ("x.jpg", b"nope", "image/jpeg")}, headers=M)
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_style"
    assert c.delete("/api/styles/neon", headers=M).status_code == 200  # los estilos propios se borran (botón siempre visible)
    ids = {s["id"] for s in c.get("/api/styles", headers=H).json()}
    assert "neon" not in ids and {"desde-galeria", "subida"} <= ids


def test_sse_emits_changed_on_library_write(env):
    c, s, lib, _ = env
    with c.stream("GET", "/api/events", headers=H) as r:
        assert r.status_code == 200
        lines = r.iter_lines()
        time.sleep(0.1); lib.create_collection("Z")
        got = ""
        for _ in range(50):
            line = next(lines)
            if line.startswith("event: changed"):
                got = line; break
        assert got == "event: changed"


def test_smoke_index_and_static_assets(env):
    c, *_ = env
    r = c.get("/", headers=H)
    assert r.status_code == 200
    assert "/static/app.js" in r.text and "/static/app.css" in r.text
    js = c.get("/static/app.js", headers=H)
    assert js.status_code == 200 and "/api/events" in js.text
    for lic in ("Inter-OFL.txt", "CormorantGaramond-OFL.txt"):
        ofl = c.get(f"/static/assets/fonts/{lic}", headers=H)
        assert ofl.status_code == 200 and "SIL OPEN FONT LICENSE" in ofl.text


def test_invalid_json_body_is_400(env):
    c, *_ = env
    r = c.post("/api/collections", content=b"{nope", headers={**M, "content-type": "application/json"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_json"
    r2 = c.post("/api/collections", content=b"[1,2]", headers={**M, "content-type": "application/json"})
    assert r2.status_code == 400 and r2.json()["code"] == "invalid_json"
    col = c.post("/api/collections", json={"name": "Tmp"}, headers=M).json()
    r3 = c.patch(f"/api/collections/{col['id']}", content=b"nope", headers={**M, "content-type": "application/json"})
    assert r3.status_code == 400 and r3.json()["code"] == "invalid_json"
    r4 = c.post(f"/api/collections/{col['id']}/items", content=b"42", headers={**M, "content-type": "application/json"})
    assert r4.status_code == 400 and r4.json()["code"] == "invalid_json"
    r5 = c.post("/api/styles", content=b"{bad", headers={**M, "content-type": "application/json"})
    assert r5.status_code == 400 and r5.json()["code"] == "invalid_json"


def test_unrelated_query_param_still_serves_page_with_cookie(env):
    c, *_ = env
    r = c.get("/?monkey=x", headers=H)
    assert r.status_code == 200 and "<html" in r.text


def test_bootstrap_cookie_is_samesite_strict(env):
    c, *_ = env
    c2 = TestClient(c.app, base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
    r = c2.get("/?key=k3y", headers=H)
    sc = r.headers["set-cookie"].lower()
    assert r.status_code == 303 and "samesite=strict" in sc and "httponly" in sc


def test_500_does_not_leak_exception_details(env, monkeypatch):
    from starlette.routing import Route
    c, *_ = env

    async def boom(request):
        raise RuntimeError("secret db password")

    c.app.routes.append(Route("/api/boom", boom, methods=["GET"]))
    c3 = TestClient(c.app, base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False)
    c3.cookies.set("gia_key", "k3y")
    r = c3.get("/api/boom", headers=H)
    assert r.status_code == 500
    body = r.json()
    assert body["code"] == "internal_error" and body["message"] == "internal server error"
    assert "secret" not in r.text and "RuntimeError" not in r.text


def test_delete_image_also_removes_thumb(env, settings):
    c, *_ = env
    t = c.get("/api/thumb/g_00000000000a/0", headers=H)
    assert t.status_code == 200
    thumb = settings.state_dir / "thumbs" / "g_00000000000a_0.jpg"
    assert thumb.exists()
    assert c.delete("/api/images/g_00000000000a/0", headers=M).json()["deleted"] is True
    assert not thumb.exists()


def test_upload_over_20mb_is_422(env):
    from gemini_image_api.style_store import MAX_IMAGE_BYTES
    c, *_ = env
    big = JPG + b"0" * MAX_IMAGE_BYTES  # valid jpeg magic, MAX+3 bytes total
    r = c.post("/api/styles", data={"name": "Gorda"}, files={"file": ("big.jpg", big, "image/jpeg")}, headers=M)
    assert r.status_code == 422 and r.json()["code"] == "invalid_style"


def test_generations_filter_by_collection_name_and_id(env):
    c, *_ = env
    cid = c.post("/api/collections", json={"name": "Otoño"}, headers=M).json()["id"]
    c.post(f"/api/collections/{cid}/items", json={"key": "g_00000000000a#0"}, headers=M)
    by_id = c.get("/api/generations", params={"collection": cid}, headers=H).json()
    by_name = c.get("/api/generations", params={"collection": "Otoño"}, headers=H).json()
    assert by_id["total"] == by_name["total"] == 1
    assert by_id["items"][0]["key"] == by_name["items"][0]["key"] == "g_00000000000a#0"
    assert c.get("/api/generations", params={"collection": "no-existe"}, headers=H).status_code == 404


def test_style_cover_from_custom_catalog(env, settings, tmp_path):
    from PIL import Image
    c, s, *_ = env
    img = tmp_path / "cover.jpg"; Image.new("RGB", (10, 10), "blue").save(img, "JPEG")
    custom = tmp_path / "mystyles.yaml"
    custom.write_text('styles:\n  mio:\n    name: "Mío"\n    family: otros\n'
                      '    description: d\n    append: a\n    ref: "cover.jpg"\n', encoding="utf-8")
    s.styles_file = str(custom)
    styles = {x["id"]: x for x in c.get("/api/styles", headers=H).json()}
    assert styles["mio"]["has_ref"] is True
    r = c.get(styles["mio"]["cover_url"], headers=H)
    assert r.status_code == 200


def test_web_errors_have_full_payload_shape(env):
    c, *_ = env
    c2 = TestClient(c.app, base_url=f"http://127.0.0.1:{PORT}")
    fields = {"status", "code", "message", "details", "retryable", "next_action"}
    r401 = c2.get("/api/state", headers=H).json()
    assert r401["status"] == "error" and r401["code"] == "unauthorized"
    assert fields <= set(r401) and r401["retryable"] is False and r401["next_action"]
    r400 = c.post("/api/collections", content=b"{bad", headers={**M, "content-type": "application/json"}).json()
    assert r400["code"] == "invalid_json" and fields <= set(r400) and r400["retryable"] is False
    r404 = c.get("/api/image/g_00000000000f/0", headers=H).json()
    assert r404["code"] == "unknown_image" and fields <= set(r404) and r404["retryable"] is False


# -- task 4: models, uploads, jobs, quota, usage, SSE jobs ----------------------

def _backend(c):
    return c.app.state.ctx.service.backend


def _wait_job(c, jid, statuses=("done", "error"), timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        jobs = {j["id"]: j for j in c.get("/api/jobs", headers=H).json()["jobs"]}
        if jid in jobs and jobs[jid]["status"] in statuses:
            return jobs[jid]
        time.sleep(0.02)
    raise AssertionError(f"job {jid} did not reach {statuses}")


def test_models_endpoint(env):
    c, *_ = env
    r = c.get("/api/models", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["models"] == [
        {"id": "auto", "name": "Auto", "description": ""},
        {"id": "gemini-flash", "name": "Flash", "description": "Respuestas rápidas"},
        {"id": "gemini-pro", "name": "Pro", "description": "Razonamiento complejo"},
        {"id": "gemini-carbon", "name": "Carbon", "description": ""},
        {"id": "gemini-4-hydrogen", "name": "Hydrogen", "description": "Nuevo"},
        {"id": "gemini-x-future", "name": "gemini-x-future", "description": ""},
    ]  # unavailable gemini-flash-lite is left out; unknown ids keep their model_name
    assert body["default"] == {"requested": "gemini-flash", "resolved": "gemini-flash"}


def test_models_default_resolved_uses_backend_alias(env):
    c, *_ = env
    _backend(c).aliases["gemini-flash"] = "gemini-4-flash"
    assert c.get("/api/models", headers=H).json()["default"] == {
        "requested": "gemini-flash", "resolved": "gemini-4-flash"}


def test_models_cached_60s_with_injected_clock(env):
    c, *_ = env
    ctx, b = c.app.state.ctx, _backend(c)
    t = [1000.0]
    ctx._now = lambda: t[0]
    c.get("/api/models", headers=H)
    t[0] += 59
    c.get("/api/models", headers=H)
    assert b.models_calls == 1
    t[0] += 2  # 61 s after the first fetch
    b.models_raw = [{"model_name": "gemini-pro", "available": True}]
    ids = [m["id"] for m in c.get("/api/models", headers=H).json()["models"]]
    assert b.models_calls == 2 and ids == ["auto", "gemini-pro"]


def test_models_falls_back_to_static_list_when_backend_fails(env):
    c, *_ = env
    b = _backend(c)
    b.models_fail = True
    body = c.get("/api/models", headers=H).json()
    assert [m["id"] for m in body["models"]] == ["auto", "gemini-flash", "gemini-pro"]
    assert body["default"]["requested"] == "gemini-flash"
    c.get("/api/models", headers=H)
    assert b.models_calls == 2  # failures are not cached
    b.models_fail = False
    assert len(c.get("/api/models", headers=H).json()["models"]) == 6


def test_jobs_validate_model_against_live_list(env):
    c, *_ = env
    b = _backend(c)
    r = c.post("/api/jobs", json={"prompt": "x", "model": "gemini-carbon"}, headers=M)
    assert r.status_code == 202 and r.json()["jobs"][0]["model"] == "gemini-carbon"
    _wait_job(c, r.json()["jobs"][0]["id"])
    r = c.post("/api/jobs", json={"prompt": "x", "model": "gemini-flash-lite"}, headers=M)  # unavailable
    assert r.status_code == 404 and r.json()["code"] == "unknown_model"
    assert "gemini-carbon" in r.json()["details"]["valid"] and "auto" in r.json()["details"]["valid"]
    n = b.models_calls
    c.post("/api/jobs", json={"prompt": "x"}, headers=M)  # no model: no list call
    assert b.models_calls == n


def test_queue_keeps_live_models_when_backend_fails_later(env):
    c, *_ = env
    ctx, b = c.app.state.ctx, _backend(c)
    t = [1000.0]
    ctx._now = lambda: t[0]
    c.get("/api/models", headers=H)
    t[0] += 120  # cache expired
    b.models_fail = True
    r = c.post("/api/jobs", json={"prompt": "x", "model": "gemini-carbon"}, headers=M)
    assert r.status_code == 202, r.text
    _wait_job(c, r.json()["jobs"][0]["id"])


def test_real_account_registry_without_default_model(env):
    c, *_ = env
    b = _backend(c)
    b.models_raw = [{"model_name": n, "available": True} for n in
                    ("gemini-flash-lite", "gemini-4-hydrogen", "gemini-pro", "gemini-carbon")]
    b.aliases["gemini-flash"] = "gemini-flash-lite"
    d = c.get("/api/models", headers=H).json()["default"]
    assert d == {"requested": "gemini-flash", "resolved": "gemini-flash-lite"}
    r = c.post("/api/jobs", json={"prompt": "x", "model": "gemini-flash"}, headers=M)
    assert r.status_code == 202, r.text
    _wait_job(c, r.json()["jobs"][0]["id"])


def test_upload_thumb_and_bad_uploads(env, settings):
    from PIL import Image
    c, *_ = env
    buf = io.BytesIO()
    Image.new("RGB", (100, 80), "blue").save(buf, "PNG")
    r = c.post("/api/uploads", files={"file": ("up.png", buf.getvalue(), "image/png")}, headers=M)
    assert r.status_code == 201
    uid = r.json()["id"]
    assert uid.startswith("u_") and r.json()["thumb_url"] == f"/api/uploads/{uid}/thumb"
    t = c.get(f"/api/uploads/{uid}/thumb", headers=H)
    assert t.status_code == 200 and t.headers["content-type"] == "image/jpeg"
    assert (settings.state_dir / "thumbs" / f"upload_{uid}.jpg").exists()
    bad = c.post("/api/uploads", files={"file": ("x.bin", b"not an image", "application/octet-stream")}, headers=M)
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_image"
    r404 = c.get("/api/uploads/u_zzz/thumb", headers=H)  # malformed uid
    assert r404.status_code == 404 and r404.json()["code"] == "unknown_upload"
    r404b = c.get("/api/uploads/u_000000000000/thumb", headers=H)  # well-formed, missing
    assert r404b.status_code == 404 and r404b.json()["code"] == "unknown_upload"
    # un archivo guardado que Pillow no puede decodificar (p. ej. SVG) → 404, no 500
    up = settings.state_dir / "uploads"; up.mkdir(exist_ok=True)
    (up / "u_000000000001.svg").write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    r = c.get("/api/uploads/u_000000000001/thumb", headers=H)
    assert r.status_code == 404 and r.json()["code"] == "invalid_image"


def test_job_submit_202_lists_and_finishes(env):
    c, *_ = env
    r = c.post("/api/jobs", json={"prompt": "un gato", "n": 2, "model": "auto", "ratio": "1:1"}, headers=M)
    assert r.status_code == 202
    body = r.json()
    assert body["batch"].startswith("b_") and len(body["jobs"]) == 2
    assert all(j["batch"] == body["batch"] and j["status"] == "queued" for j in body["jobs"])
    assert body["jobs"][0]["model"] is None  # "auto" resolves to None
    lst = c.get("/api/jobs", headers=H)
    assert lst.status_code == 200
    payload = lst.json()
    assert "eta_s" in payload and {j["id"] for j in payload["jobs"]} >= {j["id"] for j in body["jobs"]}
    for j in body["jobs"]:
        done = _wait_job(c, j["id"])
        assert done["status"] == "done" and done["record_id"]
        assert done["images"] == [f"{done['record_id']}#0"]


def test_job_validation_error_codes(env):
    c, *_ = env
    def post(payload=None, raw=None):
        if raw is not None:
            return c.post("/api/jobs", content=raw, headers={**M, "content-type": "application/json"})
        return c.post("/api/jobs", json=payload, headers=M)
    r = post({"prompt": "   "})
    assert r.status_code == 400 and r.json()["code"] == "invalid_query"
    r = post({"prompt": "x", "ratio": "7:5"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_aspect"
    r = post({"prompt": "x", "model": "gpt-4o"})
    assert r.status_code == 404 and r.json()["code"] == "unknown_model"
    r = post({"prompt": "x", "n": 9})
    assert r.status_code == 400 and r.json()["code"] == "invalid_query"
    r = post({"prompt": "x", "refs": [{"gen": "gen:g_00000000000a#0"}] * 4})
    assert r.status_code == 400 and r.json()["code"] == "invalid_ref"
    r = post({"prompt": "x", "style": "nope"})
    assert r.status_code == 404 and r.json()["code"] == "unknown_style"
    r = post({"prompt": "x", "collection": "col_00000000"})
    assert r.status_code == 404 and r.json()["code"] == "unknown_collection"
    r = post(raw=b"{nope")
    assert r.status_code == 400 and r.json()["code"] == "invalid_json"
    r = post({"prompt": "x", "refs": [{"weird": 1}]})
    assert r.status_code == 400 and r.json()["code"] == "invalid_query"


def test_cancel_job_codes(env):
    c, *_ = env
    b = _backend(c)
    b.gate.clear()  # the first job stays running, the second stays queued
    r = c.post("/api/jobs", json={"prompt": "uno", "n": 2}, headers=M)
    assert r.status_code == 202
    j0, j1 = (j["id"] for j in r.json()["jobs"])
    try:
        _wait_job(c, j0, statuses=("running",), timeout=5.0)
        r = c.delete(f"/api/jobs/{j0}", headers=M)
        assert r.status_code == 409 and r.json()["code"] == "job_running"
        r = c.delete(f"/api/jobs/{j1}", headers=M)
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        r = c.delete("/api/jobs/j_000000", headers=M)
        assert r.status_code == 404 and r.json()["code"] == "unknown_job"
    finally:
        b.gate.set()
        _wait_job(c, j0, timeout=5.0)


def test_queue_full_is_429(env):
    c, *_ = env
    b = _backend(c)
    b.gate.clear()
    try:
        assert c.post("/api/jobs", json={"prompt": "a", "n": 8}, headers=M).status_code == 202
        assert c.post("/api/jobs", json={"prompt": "b", "n": 8}, headers=M).status_code == 202
        r = c.post("/api/jobs", json={"prompt": "c", "n": 8}, headers=M)
        assert r.status_code == 429 and r.json()["code"] == "queue_full"
        assert set(r.json()) >= {"status", "code", "message", "details", "retryable", "next_action"}
    finally:
        b.gate.set()


def test_quota_cache_refresh_ttl_and_unavailable(env):
    c, *_ = env
    b = _backend(c)
    r = c.get("/api/quota", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert {k: body[k] for k in QUOTA} == QUOTA
    assert body["account_status"] == "AVAILABLE" and body["cookie_present"] is True
    # 1 imagen en la ventana (seed 2026-11-10T00:00 >= 2026-11-09T22:28), 8% usado → 92% libre → ≈11
    assert body["images_used_5h"] == 1 and body["est_images_left_5h"] == 11
    assert b.status_calls == 1
    c.get("/api/quota", headers=H)
    assert b.status_calls == 1  # second call served from the 60 s cache
    c.get("/api/quota", params={"refresh": "1"}, headers=H)
    assert b.status_calls == 2  # refresh bypasses the cache
    c.app.state.ctx.quota_cached_at = 0.0  # simulate TTL expiry (clock is injected)
    c.get("/api/quota", headers=H)
    assert b.status_calls == 3
    b.valid = False
    u = c.get("/api/quota", params={"refresh": "1"}, headers=H).json()
    assert u["available"] is False and u["message"] and u["next_action"]


def test_quota_cache_invalidated_when_job_finishes(env):
    c, *_ = env
    b = _backend(c)
    c.get("/api/quota", headers=H)
    assert b.status_calls == 1
    r = c.post("/api/jobs", json={"prompt": "un gato"}, headers=M)
    _wait_job(c, r.json()["jobs"][0]["id"])
    c.get("/api/quota", headers=H)
    assert b.status_calls == 2  # finished job invalidated the cache


def test_usage_endpoint(env):
    c, *_ = env
    r = c.get("/api/usage", headers=H)
    assert r.status_code == 200
    u = r.json()
    assert len(u["per_day"]) == 7 and all(set(d) == {"date", "images"} for d in u["per_day"])
    act = u["activity"]
    assert len(act) == 1
    a = act[0]
    assert a["prompt"] == "un gato" and a["model"] == "gemini-flash"
    assert a["style"] is None and a["style_name"] is None
    assert a["images"] == 1 and a["elapsed_s"] == 1.0 and a["status"] == "ok"
    st = u["storage"]
    assert st["output_bytes"] > 0 and st["disk_free"] > 0 and st["disk_total"] > 0


def test_collection_activity_endpoint(env):
    c, *_ = env
    cid = c.post("/api/collections", json={"name": "Otoño"}, headers=M).json()["id"]
    c.post(f"/api/collections/{cid}/items", json={"key": "g_00000000000a#0"}, headers=M)
    r = c.get(f"/api/collections/{cid}/activity", headers=H)
    assert r.status_code == 200
    kinds = [a["kind"] for a in r.json()["activity"]]
    assert kinds == ["added", "created"]  # newest first
    r404 = c.get("/api/collections/col_ffffffff/activity", headers=H)
    assert r404.status_code == 404 and r404.json()["code"] == "unknown_collection"


def test_quota_without_cookie_reports_cookie_present_false(env):
    c, *_ = env
    b = _backend(c)
    b.cookie_present = False
    u = c.get("/api/quota", params={"refresh": "1"}, headers=H).json()
    assert u["available"] is False and u["cookie_present"] is False
    assert u["next_action"] == "Run `gia login` to sign in, then retry."


def test_sse_emits_jobs_event(env):
    c, *_ = env
    with c.stream("GET", "/api/events", headers=H) as r:
        assert r.status_code == 200
        lines = r.iter_lines()
        time.sleep(0.1)
        c.post("/api/jobs", json={"prompt": "ola sse"}, headers=M)
        ev, data = None, None
        for _ in range(200):
            line = next(lines)
            if line.startswith("event: "):
                ev = line[len("event: "):]
            elif line.startswith("data: ") and ev == "jobs":
                data = json.loads(line[len("data: "):])
                break
        assert data is not None and "jobs" in data and "eta_s" in data
        assert data["jobs"] and data["jobs"][0]["prompt"] == "ola sse"


def test_oversized_content_length_rejected_before_parsing(env):
    from gemini_image_api.style_store import MAX_IMAGE_BYTES
    from gemini_image_api.uploads import UploadStore
    c, *_ = env
    big_up = str(UploadStore.MAX_BYTES + 1024 * 1024)
    r = c.post("/api/uploads", content=b"x", headers={**M, "content-length": big_up})
    assert r.status_code == 422 and r.json()["code"] == "invalid_image"
    big_style = str(MAX_IMAGE_BYTES + 1024 * 1024)
    r2 = c.post("/api/styles", content=b"x",
                headers={**M, "content-length": big_style,
                         "content-type": "multipart/form-data; boundary=x"})
    assert r2.status_code == 422 and r2.json()["code"] == "invalid_style"
    # at the cap (with slack) it is not rejected up front; the file itself is then rejected
    ok_len = str(UploadStore.MAX_BYTES + 64 * 1024)
    r3 = c.post("/api/uploads", files={"file": ("x.bin", b"not an image", "application/octet-stream")},
                headers={**M, "content-length": ok_len})
    assert r3.status_code == 422 and r3.json()["code"] == "invalid_image"  # parsed, then bad magic


# -- task 4: style gems over the web API -----------------------------------------

ERR6 = {"status", "code", "message", "details", "retryable", "next_action"}


def test_styles_list_includes_gem_field(env):
    c, *_ = env
    c.post("/api/styles", json={"name": "Propio", "description": "d"}, headers=M)
    styles = c.get("/api/styles", headers=H).json()
    assert styles and all(st.get("gem") is None for st in styles)


def test_put_gem_enable_disable_and_instructions(env):
    c, s, lib, _ = env
    sid = c.post("/api/styles", json={"name": "Propio", "description": "d"}, headers=M).json()["id"]
    # invalid_json: not JSON, and enabled missing / wrong type
    r = c.put(f"/api/styles/{sid}/gem", content=b"{bad", headers={**M, "content-type": "application/json"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_json"
    assert ERR6 <= set(r.json())
    r = c.put(f"/api/styles/{sid}/gem", json={}, headers=M)
    assert r.status_code == 400 and r.json()["code"] == "invalid_json"
    r = c.put(f"/api/styles/{sid}/gem", json={"enabled": "yes"}, headers=M)
    assert r.status_code == 400 and r.json()["code"] == "invalid_json"
    # unknown_style -> 404 with the 6-field payload
    r = c.put("/api/styles/nope/gem", json={"enabled": True}, headers=M)
    assert r.status_code == 404 and r.json()["code"] == "unknown_style" and ERR6 <= set(r.json())
    assert c.get("/api/styles/nope/gem/instructions", headers=H).status_code == 404
    # enable: 200 with the updated style, gem present, backend got create_gem
    b = _backend(c)
    r = c.put(f"/api/styles/{sid}/gem", json={"enabled": True}, headers=M)
    assert r.status_code == 200
    st = r.json()
    assert st["id"] == sid and st["gem"] and st["gem"]["gem_id"] in b.gems
    assert st["gem"]["custom"] is False and st["gem"]["name"].startswith("Musa · ")
    # GET /api/styles reflects the gem
    assert next(x for x in c.get("/api/styles", headers=H).json() if x["id"] == sid)["gem"] is not None
    # default instructions come from the template
    d = c.get(f"/api/styles/{sid}/gem/instructions", headers=H).json()
    assert d["custom"] is False and "image-generation assistant" in d["instructions"]
    # custom instructions
    r = c.put(f"/api/styles/{sid}/gem", json={"enabled": True, "instructions": "texto propio"}, headers=M)
    assert r.status_code == 200 and r.json()["gem"]["custom"] is True
    assert b.gems[st["gem"]["gem_id"]]["instructions"] == "texto propio"
    d = c.get(f"/api/styles/{sid}/gem/instructions", headers=H).json()
    assert d == {"instructions": "texto propio", "custom": True}
    # empty instructions restore the template
    r = c.put(f"/api/styles/{sid}/gem", json={"enabled": True, "instructions": ""}, headers=M)
    assert r.json()["gem"]["custom"] is False
    # disable: gem gone locally and in the backend
    r = c.put(f"/api/styles/{sid}/gem", json={"enabled": False}, headers=M)
    assert r.status_code == 200 and r.json()["gem"] is None
    assert not b.gems
    assert next(x for x in c.get("/api/styles", headers=H).json() if x["id"] == sid)["gem"] is None


def test_sse_changed_payload_includes_gems_mtime(env):
    c, s, lib, _ = env
    sid = c.post("/api/styles", json={"name": "Propio", "description": "d"}, headers=M).json()["id"]
    with c.stream("GET", "/api/events", headers=H) as r:
        assert r.status_code == 200
        lines = r.iter_lines()
        time.sleep(0.1)
        c.put(f"/api/styles/{sid}/gem", json={"enabled": True}, headers=M)
        ev, data = None, None
        for _ in range(200):
            line = next(lines)
            if line.startswith("event: "):
                ev = line[len("event: "):]
            elif line.startswith("data: ") and ev == "changed":
                data = json.loads(line[len("data: "):])
                break
        assert data is not None
        assert {"generations", "library", "styles", "gems"} <= set(data)
        assert data["gems"] > 0


def test_put_style_json_multipart_builtin_and_sse(env):
    from PIL import Image
    c, s, lib, _ = env
    sid = c.post("/api/styles", json={"name": "Propio", "description": "d"}, headers=M).json()["id"]
    with c.stream("GET", "/api/events", headers=H) as r:
        lines = r.iter_lines()
        time.sleep(0.1)
        before = s.state_dir.joinpath("styles", "styles.yaml").stat().st_mtime
        r1 = c.put(f"/api/styles/{sid}", json={"name": "Nuevo", "append": "neon glow"}, headers=M)
        assert r1.status_code == 200 and r1.json()["name"] == "Nuevo" and r1.json()["id"] == sid
        ev, data = None, None
        for _ in range(200):
            line = next(lines)
            if line.startswith("event: "):
                ev = line[len("event: "):]
            elif line.startswith("data: ") and ev == "changed":
                data = json.loads(line[len("data: "):])
                break
        assert data is not None and data["styles"] > before
    assert c.put(f"/api/styles/{sid}", json={"family": "  "}, headers=M).json()["family"] == "otros"
    assert c.put(f"/api/styles/{sid}", files={"family": (None, "")}, headers=M).json()["family"] == "otros"
    assert c.post("/api/jobs", json={"prompt": 5}, headers=M).json()["code"] == "invalid_query"
    buf = io.BytesIO(); Image.new("RGB", (8, 8), "blue").save(buf, "PNG")
    r2 = c.put(f"/api/styles/{sid}", data={"description": "otra"}, files={"file": ("x.png", buf.getvalue(), "image/png")},
               headers=M)
    assert r2.status_code == 200 and r2.json()["has_ref"] is True and r2.json()["description"] == "otra"
    assert r2.json()["name"] == "Nuevo"  # unset field kept
    r3 = c.put(f"/api/styles/{sid}", json={"remove_image": True}, headers=M)
    assert r3.status_code == 200 and r3.json()["has_ref"] is False
    assert c.put(f"/api/styles/{sid}", json={"name": "  "}, headers=M).status_code == 400
    assert c.put(f"/api/styles/{sid}", json={"name": 3}, headers=M).json()["code"] == "invalid_json"
    assert c.put("/api/styles/nope", json={"name": "x"}, headers=M).status_code == 404
    import yaml
    (s.state_dir / "base.yaml").write_text(yaml.safe_dump({"styles": {"anime": {"description": "d", "append": "a"}}}))
    s.styles_file = s.state_dir / "base.yaml"
    rb = c.put("/api/styles/anime", json={"name": "x"}, headers=M)
    assert rb.status_code == 403 and rb.json()["code"] == "invalid_style"


def test_put_style_oversized_multipart_is_422(env):
    from gemini_image_api.style_store import MAX_IMAGE_BYTES
    c, *_ = env
    sid = c.post("/api/styles", json={"name": "Propio", "description": "d"}, headers=M).json()["id"]
    big = JPG + b"0" * MAX_IMAGE_BYTES
    r = c.put(f"/api/styles/{sid}", files={"file": ("x.jpg", big, "image/jpeg")}, headers=M)
    assert r.status_code == 422 and r.json()["code"] == "invalid_style"


def test_style_preview_and_templates_are_read_only(env):
    c, *_ = env
    sid = c.post("/api/styles", json={"name": "Propio", "append": "neon glow"}, headers=M).json()["id"]
    r = c.get(f"/api/styles/{sid}/preview", params={"prompt": "un gato", "aspect": "1:1"}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "un gato" in body["final_prompt"] and "neon glow" in body["final_prompt"]
    assert body["aspect_hint"] == "1:1" and body["has_ref"] is False
    assert _backend(c).quota_calls == 0 and _backend(c).status_calls == 0
    assert "<prompt>" in c.get(f"/api/styles/{sid}/preview", headers=H).json()["final_prompt"]
    assert c.get("/api/styles/nope/preview", headers=H).status_code == 404
    assert c.get(f"/api/styles/{sid}/preview", params={"aspect": "7:7"}, headers=H).status_code == 400
    t = c.get("/api/templates", headers=H)
    assert t.status_code == 200
    assert set(t.json()["anuncio_producto"]) == {"description", "vars"}
    assert t.json()["anuncio_producto"]["vars"] == ["producto", "fondo", "estilo"]
    assert c.post("/api/templates", json={}, headers=M).status_code == 405


def test_job_with_template_and_edit_of(env):
    c, *_ = env
    b = _backend(c)
    edits = []

    async def edit(conversation_id, prompt, refs, *, model, **kw):
        edits.append((conversation_id, prompt))
        return await b.generate(prompt, refs, model=model, **kw)
    b.edit = edit
    v = {"producto": "taza", "fondo": "blanco", "estilo": "suave"}
    r = c.post("/api/jobs", json={"template": "anuncio_producto", "template_vars": v}, headers=M)
    assert r.status_code == 202, r.text
    job = r.json()["jobs"][0]
    assert job["template"] == "anuncio_producto" and job["template_vars"] == v
    _wait_job(c, job["id"])
    assert c.post("/api/jobs", json={"template": "anuncio_producto"}, headers=M).json()["code"] == "template_missing_var"
    assert c.post("/api/jobs", json={"template": "nope"}, headers=M).status_code == 404
    bad = c.post("/api/jobs", json={"prompt": "x", "edit_of": "gen:g_000000000000#0"}, headers=M)
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_ref"
    assert c.post("/api/jobs", json={"prompt": "x", "edit_of": "gen:g_00000000000a#0", "n": 2},
                  headers=M).json()["code"] == "invalid_query"
    ok = c.post("/api/jobs", json={"prompt": "más rojo", "edit_of": "gen:g_00000000000a#0"}, headers=M)
    assert ok.status_code == 202 and ok.json()["jobs"][0]["edit_of"] == "gen:g_00000000000a#0"
    _wait_job(c, ok.json()["jobs"][0]["id"])
    assert [e[0] for e in edits] == ["c_1"]


def test_quota_estimate_counts_utc_window(env):
    """resets_at_5h en otra zona horaria: la ventana se calcula en UTC, no comparando texto."""
    c, *_ = env
    b = _backend(c)
    b.quota_data = {**QUOTA, "resets_at_5h": "2026-11-10T03:28:00-05:00"}  # = 08:28 UTC; ventana desde 03:28 UTC
    c.app.state.ctx.quota_cache = None
    body = c.get("/api/quota", params={"refresh": "1"}, headers=H).json()
    # la imagen semilla (2026-11-10T00:00:00+00:00) es ANTERIOR a las 03:28 UTC → no cuenta
    assert body["images_used_5h"] == 0 and body["est_images_left_5h"] is None


def test_estimate_images_left_direct_mixed_offsets(settings):
    """_estimate_images_left sin cliente web: compara fechas, no texto."""
    from gemini_image_api.state import StateStore, GenerationRecord
    from gemini_image_api.webapp import _estimate_images_left
    st = StateStore(settings.state_dir)
    def rec(i, ts):
        st.append(GenerationRecord(id=f"g_{i:012x}", ts=ts, type="generate", conversation_id=f"c{i}",
                    chat_metadata=["c"], prompt="p", style=None, style_text=None, aspect_hint=None,
                    model=None, project=None, subdir=None, refs=[],
                    images=[{"index": 0, "path": f"/x/{i}.jpg"}], status="ok", code=None,
                    gemini_text="", elapsed_s=1.0, parent_id=None, user_prompt=None))
    rec(1, "2026-11-10T01:00:00+00:00")   # fuera de la ventana real (ventana: 03:28–08:28 UTC)
    rec(2, "2026-11-10T05:00:00+00:00")   # dentro
    q = {"resets_at_5h": "2026-11-10T03:28:00-05:00", "window_5h_pct": 20.0}  # = 08:28 UTC
    est, used = _estimate_images_left(q, st)
    assert used == 1 and est == 4  # 80% libre × (1 img / 20% usado) = 4
    # pct 0 o sin imágenes → None
    assert _estimate_images_left({**q, "window_5h_pct": 0.0}, st) == (None, 1)
    assert _estimate_images_left(q, StateStore(settings.state_dir / "vacia")) [0] is None
    # fecha inválida → None, sin explotar
    assert _estimate_images_left({"resets_at_5h": "no-es-fecha", "window_5h_pct": 10.0}, st) == (None, 0)


# -- saved prompts and search over the web API ---------------------------------

def _sse_changed(c, action, timeout_lines=300):
    """Open /api/events, run `action`, return the first `changed` payload."""
    with c.stream("GET", "/api/events", headers=H) as r:
        assert r.status_code == 200
        lines = r.iter_lines()
        time.sleep(0.1)
        action()
        ev = None
        deadline = time.monotonic() + 10
        for _ in range(timeout_lines):
            line = next(lines)  # SSE pings (every 15 s) also wake this loop
            if time.monotonic() > deadline:
                break
            if line.startswith("event: "):
                ev = line[len("event: "):]
            elif line.startswith("data: ") and ev == "changed":
                return json.loads(line[len("data: "):])
    raise AssertionError("no `changed` SSE event within 10 s / %d lines" % timeout_lines)


def test_prompts_crud_and_use(env):
    c, *_ = env
    assert c.get("/api/prompts", headers=H).json() == []
    r = c.post("/api/prompts", json={"name": "Gato", "text": "un gato naranja"}, headers=M)
    assert r.status_code == 201
    p = r.json()
    assert set(p) == {"id", "name", "text", "created", "uses"} and p["uses"] == 0
    assert c.get("/api/prompts", headers=H).json() == [p]
    u = c.put(f"/api/prompts/{p['id']}", json={"text": "un gato negro"}, headers=M)
    assert u.status_code == 200 and u.json()["text"] == "un gato negro" and u.json()["name"] == "Gato"
    u2 = c.put(f"/api/prompts/{p['id']}", json={"name": "Gato2"}, headers=M)
    assert u2.json()["name"] == "Gato2" and u2.json()["text"] == "un gato negro"
    used = c.post(f"/api/prompts/{p['id']}/use", headers=M)
    assert used.status_code == 200 and used.json()["uses"] == 1 and used.json()["text"] == "un gato negro"
    d = c.delete(f"/api/prompts/{p['id']}", headers=M)
    assert d.status_code == 200 and d.json() == {"deleted": p["id"]}
    assert c.get("/api/prompts", headers=H).json() == []


def test_prompts_errors_have_six_fields(env):
    c, *_ = env
    c.post("/api/prompts", json={"name": "Gato", "text": "x"}, headers=M)
    cases = [
        c.post("/api/prompts", json={"name": "", "text": "x"}, headers=M),
        c.post("/api/prompts", json={"name": "Otro", "text": ""}, headers=M),
        c.post("/api/prompts", json={"name": "gato", "text": "dup"}, headers=M),
        c.post("/api/prompts", json={"name": 5, "text": "x"}, headers=M),
        c.put("/api/prompts/pr_00000000", json={"text": "x"}, headers=M),
        c.delete("/api/prompts/pr_00000000", headers=M),
        c.post("/api/prompts/pr_00000000/use", headers=M),
        c.post("/api/prompts", content=b"{nope", headers={**M, "content-type": "application/json"}),
    ]
    assert [r.status_code for r in cases] == [400, 400, 400, 400, 404, 404, 404, 400]
    assert [r.json()["code"] for r in cases] == ["invalid_prompt"] * 4 + ["unknown_prompt"] * 3 + ["invalid_json"]
    for r in cases:
        assert set(r.json()) == ERR6 and r.json()["retryable"] is False
    from gemini_image_api.webapp import WEB_NEXT_ACTION
    assert cases[0].json()["next_action"] == WEB_NEXT_ACTION["invalid_prompt"]
    assert cases[4].json()["next_action"] == WEB_NEXT_ACTION["unknown_prompt"]


def test_prompts_big_legit_body_is_accepted(env):
    c, *_ = env
    text = "😀" * 4000  # escaped as surrogate pairs: ~48 KB of JSON
    raw = json.dumps({"name": "Grande", "text": text}).encode()
    assert 45_000 < len(raw) < 64 * 1024
    r = c.post("/api/prompts", content=raw, headers={**M, "content-type": "application/json"})
    assert r.status_code == 201 and r.json()["text"] == text
    over = c.post("/api/prompts", content=b"{}", headers={**M, "content-length": str(129 * 1024),
                                                          "content-type": "application/json"})
    assert over.status_code == 400 and "128 KB" in over.json()["message"]


def test_prompts_bad_field_types_and_missing_origin(env):
    c, *_ = env
    pid = c.post("/api/prompts", json={"name": "Gato", "text": "x"}, headers=M).json()["id"]
    for body in ({"text": 7}, {"name": None}):
        r = c.put(f"/api/prompts/{pid}", json=body, headers=M)
        assert r.status_code == 400 and r.json()["code"] == "invalid_prompt", body
    assert c.put(f"/api/prompts/{pid}", json={"text": "y"}, headers=H).status_code == 403
    assert c.delete(f"/api/prompts/{pid}", headers=H).status_code == 403
    assert c.post(f"/api/prompts/{pid}/use", headers=H).status_code == 403
    assert c.get("/api/prompts", headers=H).json()[0]["text"] == "x"  # nothing changed


def test_prompts_oversized_body_and_middleware(env):
    c, *_ = env
    r = c.post("/api/prompts", content=b"{}", headers={**M, "content-length": str(1024 * 1024),
                                                       "content-type": "application/json"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_prompt" and set(r.json()) == ERR6
    c2 = TestClient(c.app, base_url=f"http://127.0.0.1:{PORT}")
    assert c2.get("/api/prompts", headers=H).status_code == 401
    assert c.post("/api/prompts", json={"name": "a", "text": "b"}, headers=H).status_code == 403  # no Origin


def test_sse_changed_includes_prompts_mtime(env):
    c, *_ = env
    store = c.app.state.ctx.service.prompts
    data = _sse_changed(c, lambda: c.post("/api/prompts", json={"name": "A", "text": "uno"}, headers=M))
    assert "prompts" in data and data["prompts"] == store.mtime() > 0
    pid = c.get("/api/prompts", headers=H).json()[0]["id"]
    before = store.mtime()
    time.sleep(0.02)
    data = _sse_changed(c, lambda: c.put(f"/api/prompts/{pid}", json={"text": "dos"}, headers=M))
    assert data["prompts"] > before
    before = store.mtime()
    time.sleep(0.02)
    data = _sse_changed(c, lambda: c.delete(f"/api/prompts/{pid}", headers=M))
    assert data["prompts"] > before


@pytest.fixture
def search_env(env, settings):
    from PIL import Image
    c, s, lib, _ = env
    sid = c.post("/api/styles", json={"name": "Neón Violeta", "description": "d"}, headers=M).json()["id"]
    st = StateStore(settings.state_dir)

    def rec(gid, ts, model, prompt, style=None):
        p = settings.output_root / f"{gid}.jpg"
        Image.new("RGB", (10, 10), "blue").save(p, "JPEG")
        st.append(GenerationRecord(id=gid, ts=ts, type="generate", conversation_id="c", chat_metadata=["c"],
            prompt=prompt, style=style, style_text=None, aspect_hint=None, model=model, project=None,
            subdir=None, refs=[], images=[{"index": 0, "path": str(p), "width": 10, "height": 10}],
            status="ok", code=None, gemini_text="", elapsed_s=1.0, parent_id=None, user_prompt=prompt))

    rec("g_00000000000b", "2026-11-11T12:00:00+00:00", "gemini-pro", "una casa", style=sid)
    rec("g_00000000000c", "2026-11-12T12:00:00+00:00", "gemini-pro", "un barco")
    return c, sid


def _ids(c, **params):
    r = c.get("/api/generations", params=params, headers=H)
    assert r.status_code == 200, r.text
    return [i["gen_id"][-1] for i in r.json()["items"]]


def test_generations_model_sort_and_dates(search_env):
    c, _ = search_env
    assert sorted(_ids(c, model="gemini-pro")) == ["b", "c"]
    assert _ids(c, model="gemini-flash") == ["a"]
    assert _ids(c, model="nope") == []
    assert _ids(c) == ["c", "b", "a"]
    assert _ids(c, sort="newest") == ["c", "b", "a"]
    assert _ids(c, sort="oldest") == ["a", "b", "c"]
    c.put("/api/favorites/g_00000000000a/0", headers=M)
    assert _ids(c, sort="favorites") == ["a", "c", "b"]
    assert _ids(c, since="2026-11-11") == ["c", "b"]
    assert _ids(c, until="2026-11-11") == ["b", "a"]  # date-only until includes the whole day
    assert _ids(c, since="2026-11-11", until="2026-11-11") == ["b"]
    assert _ids(c, since="2026-11-11T13:00:00+00:00") == ["c"]
    assert _ids(c, model="gemini-pro", since="2026-11-12") == ["c"]


def test_generations_query_matches_style_name(search_env):
    c, sid = search_env
    assert _ids(c, query="violeta") == ["b"]
    assert _ids(c, query=sid) == ["b"]
    assert _ids(c, query="barco") == ["c"]


def test_generations_invalid_query_is_400_six_fields(search_env):
    c, _ = search_env
    bad = [{"limit": "0"}, {"limit": "abc"}, {"since": "not-a-date"}, {"until": "2026-13-45"},
           {"sort": "random"}, {"filter": "weird"}, {"since": "2026-11-12", "until": "2026-11-01"},
           {"since": "9" * 40}]
    for params in bad:
        r = c.get("/api/generations", params=params, headers=H)
        assert r.status_code == 400, params
        body = r.json()
        assert set(body) == ERR6 and body["code"] == "invalid_query", params


def test_collection_listing_accepts_search_params(search_env):
    c, _ = search_env
    cid = c.post("/api/collections", json={"name": "Mix"}, headers=M).json()["id"]
    for k in ("g_00000000000a#0", "g_00000000000b#0", "g_00000000000c#0"):
        c.post(f"/api/collections/{cid}/items", json={"key": k}, headers=M)
    assert _ids(c, collection=cid, sort="oldest") == ["a", "b", "c"]
    assert _ids(c, collection=cid, model="gemini-pro", query="barco") == ["c"]
    assert _ids(c, collection="Mix", since="2026-11-12") == ["c"]
    r = c.get("/api/generations", params={"collection": cid, "limit": "0"}, headers=H)
    assert r.status_code == 400 and r.json()["code"] == "invalid_query"


# -- batch actions and zip download -------------------------------------------

A, B, C = "g_00000000000a#0", "g_00000000000b#0", "g_00000000000c#0"


def _batch(c, **body):
    return c.post("/api/images/batch", json=body, headers=M)


def test_batch_favorite_and_unfavorite(search_env):
    c, _ = search_env
    r = _batch(c, action="favorite", keys=[A, B])
    assert r.status_code == 200
    assert r.json() == {"results": [{"key": A, "ok": True}, {"key": B, "ok": True}]}
    assert sorted(_ids(c, filter="favorites")) == ["a", "b"]
    assert _batch(c, action="unfavorite", keys=[A]).json()["results"] == [{"key": A, "ok": True}]
    assert _ids(c, filter="favorites") == ["b"]


def test_batch_add_and_remove_collection(search_env):
    c, _ = search_env
    r = _batch(c, action="add_to_collection", keys=[A, C], collection="Lote")
    assert r.status_code == 200 and all(x["ok"] for x in r.json()["results"])
    assert sorted(_ids(c, collection="Lote")) == ["a", "c"]
    r = _batch(c, action="remove_from_collection", keys=[A], collection="Lote")
    assert r.json()["results"] == [{"key": A, "ok": True}]
    assert _ids(c, collection="Lote") == ["c"]


def test_batch_delete_removes_file_and_thumb(search_env, settings):
    c, _ = search_env
    assert c.get("/api/thumb/g_00000000000b/0", headers=H).status_code == 200
    ctx = c.app.state.ctx
    path, thumb = ctx.entry("g_00000000000b", 0).path, ctx.thumb_path("g_00000000000b", 0)
    assert path.exists() and thumb.exists()
    r = _batch(c, action="delete", keys=[B])
    assert r.json()["results"] == [{"key": B, "ok": True}]
    assert not path.exists() and not thumb.exists()
    assert "b" not in _ids(c)


def test_batch_partial_success_reports_per_key_errors(search_env):
    c, _ = search_env
    r = _batch(c, action="favorite", keys=[A, "g_ffffffffffff#0", B])
    assert r.status_code == 200
    res = r.json()["results"]
    assert [x["ok"] for x in res] == [True, False, True]
    assert res[1]["key"] == "g_ffffffffffff#0" and res[1]["error"]["code"] == "invalid_ref"
    assert sorted(_ids(c, filter="favorites")) == ["a", "b"]


def test_batch_global_errors_have_six_fields(search_env):
    c, _ = search_env
    many = [f"g_{i:012x}#0" for i in range(201)]
    cases = [
        (_batch(c, action="explode", keys=[A]), 400, "invalid_query"),
        (_batch(c, action="favorite", keys=many), 400, "invalid_query"),
        (_batch(c, action="favorite", keys=[]), 400, "invalid_query"),
        (_batch(c, action="favorite", keys="g_00000000000a#0"), 400, "invalid_query"),
        (_batch(c, action="add_to_collection", keys=[A]), 400, "invalid_query"),
        (_batch(c, action="add_to_collection", keys=[A], collection=5), 400, "invalid_query"),
        (_batch(c, action="remove_from_collection", keys=[A], collection="Nada"), 404, "unknown_collection"),
        (c.post("/api/images/batch", content=b"{nope", headers={**M, "content-type": "application/json"}),
         400, "invalid_json"),
        (c.post("/api/images/batch", content=b"{}", headers={**M, "content-length": str(1024 * 1024),
                                                            "content-type": "application/json"}),
         400, "invalid_query"),
    ]
    for r, status, code in cases:
        assert (r.status_code, r.json()["code"]) == (status, code), r.text
        assert set(r.json()) == ERR6
    assert _ids(c, filter="favorites") == []  # nothing ran


def test_batch_and_zip_require_key_and_origin(search_env):
    c, _ = search_env
    anon = TestClient(c.app, base_url=f"http://127.0.0.1:{PORT}")
    for path, body in (("/api/images/batch", {"action": "favorite", "keys": [A]}),
                       ("/api/images/zip", {"keys": [A]})):
        assert anon.post(path, json=body, headers=M).status_code == 401
        assert c.post(path, json=body, headers=H).status_code == 403  # key ok, no Origin
    assert _ids(c, filter="favorites") == []


def test_sse_changed_after_batch(search_env):
    c, _ = search_env
    lib = c.app.state.ctx.library
    before = lib.mtime()
    time.sleep(0.02)
    data = _sse_changed(c, lambda: _batch(c, action="favorite", keys=[A]))
    assert data["library"] > before


def _tmp_zips(settings):
    d = Path(settings.state_dir) / "tmp_downloads"
    return list(d.glob("*")) if d.exists() else []


def test_zip_download_headers_content_and_cleanup(search_env, settings):
    import re
    import zipfile
    c, _ = search_env
    r = c.post("/api/images/zip", json={"keys": [A, B, C]}, headers=M)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["cache-control"] == "no-store"
    m = re.fullmatch(r'attachment; filename="([A-Za-z0-9._-]+\.zip)"', r.headers["content-disposition"])
    assert m, r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = zf.namelist()
        assert len(names) == 3 and zf.testzip() is None
        for n in names:
            assert "/" not in n and ".." not in n and n.endswith(".jpg")
        assert any(n.startswith("2026-11-10_") and "gato" in n for n in names)
        assert any(n.startswith("2026-11-11_") and "casa" in n for n in names)
        assert zf.read(names[0])[:3] == b"\xff\xd8\xff"
    assert _tmp_zips(settings) == []  # temp file removed once sent


def test_zip_temp_deleted_when_send_fails(search_env, settings):
    c, _ = search_env
    payload = json.dumps({"keys": [A, B]}).encode()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": "/api/images/zip", "raw_path": b"/api/images/zip",
             "query_string": b"", "root_path": "", "server": ("127.0.0.1", PORT),
             "client": ("127.0.0.1", 5000),
             "headers": [(b"host", f"127.0.0.1:{PORT}".encode()),
                         (b"origin", f"http://127.0.0.1:{PORT}".encode()),
                         (b"cookie", b"gia_key=k3y"), (b"content-type", b"application/json"),
                         (b"content-length", str(len(payload)).encode())]}
    state = {"sent": 0, "created": []}

    calls = []

    async def receive():
        calls.append(1)
        if len(calls) == 1:
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.sleep(3600)  # FileResponse listens for a disconnect; it never comes

    async def send(msg):
        state["sent"] += 1
        if msg["type"] == "http.response.body":
            state["created"] = _tmp_zips(settings)  # the temp file exists while it is being sent
            raise ConnectionResetError("client went away")

    with pytest.raises(Exception):  # the send failure (possibly wrapped in an exception group)
        asyncio.run(asyncio.wait_for(c.app(scope, receive, send), 20))
    assert state["created"] and state["sent"] >= 2
    assert _tmp_zips(settings) == []


def test_zip_unknown_keys_is_404_without_orphans(search_env, settings):
    c, _ = search_env
    r = c.post("/api/images/zip", json={"keys": [A, "g_ffffffffffff#0", "g_eeeeeeeeeeee#3"]}, headers=M)
    assert r.status_code == 404 and set(r.json()) == ERR6 and r.json()["code"] == "invalid_ref"
    assert r.json()["details"]["missing"] == ["g_ffffffffffff#0", "g_eeeeeeeeeeee#3"]
    assert _tmp_zips(settings) == []
    _batch(c, action="delete", keys=[B])
    r2 = c.post("/api/images/zip", json={"keys": [B]}, headers=M)  # deleted image
    assert r2.status_code == 404 and r2.json()["details"]["missing"] == [B]


def test_zip_invalid_requests_and_no_client_paths(search_env, settings, tmp_path):
    c, _ = search_env
    secret = tmp_path / "secret.jpg"; secret.write_bytes(JPG)
    bad = [{"keys": []}, {"keys": "x"}, {"keys": [str(secret)]}, {"keys": ["../../etc/passwd"]},
           {"keys": [A, A]}, {"keys": [f"g_{i:012x}#0" for i in range(101)]}, {}]
    for body in bad:
        r = c.post("/api/images/zip", json=body, headers=M)
        assert r.status_code == 400 and r.json()["code"] == "invalid_query", body
        assert set(r.json()) == ERR6
    r = c.post("/api/images/zip", content=b"{nope", headers={**M, "content-type": "application/json"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_json"
    assert _tmp_zips(settings) == []

def test_manifest_and_update_endpoints(search_env):
    c, _ = search_env
    r = c.get("/manifest.json")
    assert r.status_code == 200
    assert "application/manifest+json" in r.headers["content-type"]
    data = r.json()
    assert "Musa" in data["name"]
    assert data["display"] == "standalone"

    from unittest.mock import patch
    with patch("gemini_image_api.updates.check_update", return_value={"has_update": True, "latest_sha": "abc"}):
        r2 = c.get("/api/update")
        assert r2.status_code == 200
        assert r2.json() == {"has_update": True, "latest_sha": "abc"}

def test_index_revalidates_and_versions_assets(search_env):
    c, _ = search_env
    r = c.get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    assert "/static/app.js?v=" in r.text and "/static/app.css?v=" in r.text
    assert c.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert c.get("/static/app.css").headers["cache-control"] == "no-cache"


# -- animations ----------------------------------------------------------------------------
_SHEET_JPEG: list[bytes] = []


def _sheet_jpeg() -> bytes:
    if not _SHEET_JPEG:
        from sprite_synth import walk_sheet
        buf = io.BytesIO()
        walk_sheet(True).save(buf, "JPEG")
        _SHEET_JPEG.append(buf.getvalue())
    return _SHEET_JPEG[0]


class SheetBackend(FakeBackend):
    """generate/edit write a synthetic 4x2 walk sheet into output_root and record it like the client."""

    def __init__(self, settings, img):
        super().__init__(settings, img)
        self.sent: list[tuple] = []

    def _save(self, kind, cid, prompt, kw):
        rid = StateStore.new_id("g")
        p = self.settings.output_root / f"{rid}_0.jpg"
        p.write_bytes(_sheet_jpeg())
        StateStore(self.settings.state_dir).append(GenerationRecord(
            id=rid, ts=datetime.now(timezone.utc).isoformat(), type=kind, conversation_id=cid,
            chat_metadata=["c"], prompt=prompt, style=None, style_text=None, aspect_hint=kw.get("aspect_hint"),
            model="gemini-flash", project=None, subdir=None, refs=[], images=[{"index": 0, "path": str(p)}],
            status="ok", code=None, gemini_text="", elapsed_s=1.0, parent_id=None,
            user_prompt=kw.get("slug_source")))
        img = SavedImage(p, 1376, 768, p.stat().st_size, "JPEG", True, None)
        return GenerateResult("ok", None, cid, [img], "done", 1.0, ["c"], record_id=rid)

    async def generate(self, prompt, refs, *, model, **kw):
        self.sent.append(("generate", prompt, kw))
        return self._save("generate", StateStore.new_id("c"), prompt, kw)

    async def edit(self, conversation_id, prompt, refs, *, model, **kw):
        self.sent.append(("edit", prompt, kw))
        return self._save("edit", conversation_id, prompt, kw)


@pytest.fixture
def anim(env):
    c, s, lib, p = env
    c.app.state.ctx.service.backend = SheetBackend(s, p)
    return c, s


def _animate_job(c, prompt="un robot caminando", **animation):
    r = c.post("/api/jobs", json={"prompt": prompt, "animation": {"grid": "4x2", **animation}}, headers=M)
    assert r.status_code == 202, r.text
    job = _wait_job(c, r.json()["jobs"][0]["id"])
    assert job["status"] == "done", job
    return job


def _akey(job):
    return job["images"][0].replace("#", "/")


def test_animation_job_prompt_aspect_record_and_gallery_item(anim):
    c, s = anim
    r = c.post("/api/jobs", json={"prompt": "un robot caminando", "ratio": "1:1",
                                  "animation": {"grid": "4x2"}}, headers=M)
    assert r.status_code == 202
    j = r.json()["jobs"][0]
    assert j["ratio"] == "16:9" and j["animation"] == {"grid": "4x2", "mode": "sprite", "key_color": "green"}
    job = _wait_job(c, j["id"])
    assert job["status"] == "done" and job["warning"] is None
    kind, sent, kw = _backend(c).sent[-1]
    assert kw["aspect_hint"] == "16:9" and "4 columns x 2 rows" in sent
    rec = c.app.state.ctx.service.state.generation(job["record_id"])
    assert rec.user_prompt == "un robot caminando"
    items = {i["key"]: i for i in c.get("/api/generations", headers=H).json()["items"]}
    it = items[job["images"][0]]
    assert it["prompt"] == "un robot caminando"
    a = it["animation"]
    assert set(a) == {"frames", "fps", "loop", "mode", "status", "rev", "frame_w", "frame_h", "sheet_url"}
    assert a["frames"] == 8 and a["fps"] == 10 and a["loop"] == "loop" and a["mode"] == "sprite"
    gen, i = job["images"][0].split("#")
    assert a["sheet_url"] == f"/api/animations/{gen}/{i}/sheet.png?rev={a['rev']}"
    assert items["g_00000000000a#0"]["animation"] is None


def test_job_body_never_accepts_meta_or_display_prompt(anim):
    c, _ = anim
    r = c.post("/api/jobs", json={"prompt": "x", "meta": {"a": 1}, "display_prompt": "z"}, headers=M)
    j = r.json()["jobs"][0]
    assert j["prompt"] == "x" and "meta" not in j


def test_job_animation_errors(anim):
    c, _ = anim
    bodies = [{"prompt": "x", "animation": {"grid": "9x9"}},
              {"prompt": "x", "animation": {"grid": "4x2", "extra": 1}},
              {"prompt": "x", "animation": "4x2"},
              {"prompt": "x", "template": "nope", "animation": {"grid": "4x2"}},
              {"prompt": "x", "edit_of": "gen:g_00000000000a#0", "animation": {"grid": "4x2"}}]
    for body in bodies:
        r = c.post("/api/jobs", json=body, headers=M)
        assert r.status_code == 400 and r.json()["code"] == "invalid_animation", body


def test_animation_convert_adjust_and_sheet(anim):
    from PIL import Image
    c, _ = anim
    u = "/api/animations/g_00000000000a/0"
    r = c.get(u, headers=H)
    assert r.status_code == 404 and r.json()["code"] == "unknown_animation"
    r = c.put(u, json={"cols": 4, "rows": 2}, headers=M)
    assert r.status_code == 200, r.text
    f = r.json()
    assert f["frames"] == 8 and f["fps"] == 10 and f["sheet_url"] == f"{u}/sheet.png?rev={f['rev']}"
    assert c.get(u, headers=H).json()["rev"] == f["rev"]
    sheet = c.get(f["sheet_url"], headers=H)
    assert sheet.status_code == 200 and sheet.headers["content-type"] == "image/png"
    assert sheet.headers["cache-control"] == "private, max-age=3600"
    assert Image.open(io.BytesIO(sheet.content)).size == (8 * f["frame_w"], f["frame_h"])
    f2 = c.put(u, json={"fps": 24, "loop": "pingpong"}, headers=M).json()
    assert f2["rev"] == f["rev"] and f2["fps"] == 24 and f2["loop"] == "pingpong"
    f3 = c.put(u, json={"tolerance": 50}, headers=M).json()
    assert f3["rev"] != f["rev"] and f3["fps"] == 24
    f4 = c.put(u, json={"mode": "scene", "cols": 2, "rows": 2}, headers=M).json()
    assert f4["mode"] == "scene" and f4["key_color"] is None and f4["frames"] == 4
    s4 = c.get(f4["sheet_url"], headers=H)
    assert Image.open(io.BytesIO(s4.content)).size == (4 * f4["frame_w"], f4["frame_h"])
    assert c.put(u, json={"redetect": True}, headers=M).status_code == 200


def test_animation_regenerates_missing_cache(anim, settings):
    c, s = anim
    u = "/api/animations/g_00000000000a/0"
    f = c.put(u, json={"cols": 2, "rows": 2}, headers=M).json()
    for p in (s.state_dir / "anim_cache").glob("*.png"):
        p.unlink()
    assert c.get(f["sheet_url"], headers=H).status_code == 200
    assert list((s.state_dir / "anim_cache").glob("*.png"))


def test_animation_errors_and_origin(anim):
    c, s = anim
    u = "/api/animations/g_00000000000a/0"
    bad = [{"cols": 4}, {"cols": 4, "rows": 2, "fps": 1}, {"cols": 1, "rows": 1}, {"cols": 4, "rows": 2, "mode": "x"},
           {"cols": 4, "rows": 2, "tolerance": 99}, {"cols": 4, "rows": 2, "bogus": 1},
           {"cols": 4, "rows": 2, "redetect": "yes"}, {"cols": True, "rows": 2}]
    for body in bad:
        r = c.put(u, json=body, headers=M)
        assert r.status_code == 400 and r.json()["code"] == "invalid_animation", body
    for path in ("/api/animations/g_ffffffffffff/0", "/api/animations/bad/0", "/api/animations/g_00000000000a/9"):
        for method in ("get", "delete"):
            r = getattr(c, method)(path, headers=M)
            assert r.status_code == 404 and r.json()["code"] == "unknown_image", (method, path)
        assert c.put(path, json={"cols": 2, "rows": 2}, headers=M).status_code == 404
        assert c.get(path + "/sheet.png", headers=H).status_code == 404
        assert c.get(path + "/export.zip", headers=H).status_code == 404
    for suffix in ("/sheet.png", "/export.zip"):
        r = c.get(u + suffix, headers=H)
        assert r.status_code == 404 and r.json()["code"] == "unknown_animation"
    assert c.put(u, json={"cols": 2, "rows": 2}, headers=H).status_code == 403
    assert c.delete(u, headers=H).status_code == 403
    anon = TestClient(c.app, base_url=f"http://127.0.0.1:{PORT}")
    assert anon.get(u, headers=H).status_code == 401


def test_animation_undecodable_image_is_422(anim, settings):
    c, s = anim
    bad = settings.output_root / "bad.jpg"; bad.write_bytes(JPG)
    StateStore(s.state_dir).append(GenerationRecord(
        id="g_00000000000b", ts="2026-11-10T00:01:00+00:00", type="generate", conversation_id="c_b",
        chat_metadata=["c"], prompt="x", style=None, style_text=None, aspect_hint=None, model="m", project=None,
        subdir=None, refs=[], images=[{"index": 0, "path": str(bad)}], status="ok", code=None, gemini_text="",
        elapsed_s=1.0, parent_id=None, user_prompt="x"))
    r = c.put("/api/animations/g_00000000000b/0", json={"cols": 2, "rows": 2}, headers=M)
    assert r.status_code == 422 and r.json()["code"] == "invalid_image"


def test_animation_export_zip(anim, settings):
    import zipfile
    from PIL import Image
    c, s = anim
    job = _animate_job(c, prompt="Un Robot Caminando!")
    u = f"/api/animations/{_akey(job)}"
    f = c.get(u, headers=H).json()
    r = c.get(u + "/export.zip", headers=H)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == 'attachment; filename="un-robot-caminando-animacion.zip"'
    assert r.headers["cache-control"] == "no-store"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == ["un-robot-caminando.json", "un-robot-caminando.png"]
        assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())
        meta = json.loads(zf.read("un-robot-caminando.json"))
        size = Image.open(io.BytesIO(zf.read("un-robot-caminando.png"))).size
    assert meta == {"image": "un-robot-caminando.png", "frameWidth": f["frame_w"], "frameHeight": f["frame_h"],
                    "frames": 8, "columns": 8, "rows": 1, "fps": 10, "loop": "loop", "transparent": True}
    assert size == (meta["frames"] * meta["frameWidth"], meta["frameHeight"])
    assert _tmp_zips(settings) == []


def test_delete_animation_keeps_image(anim):
    c, s = anim
    job = _animate_job(c)
    gen, i = job["images"][0].split("#")
    u = f"/api/animations/{gen}/{i}"
    r = c.delete(u, headers=M)
    assert r.status_code == 200 and r.json() == {"key": job["images"][0], "deleted": True}
    assert c.get(f"/api/image/{gen}/{i}", headers=H).status_code == 200
    assert c.get(u, headers=H).status_code == 404
    assert c.delete(u, headers=M).json()["code"] == "unknown_animation"
    items = {x["key"]: x for x in c.get("/api/generations", headers=H).json()["items"]}
    assert items[job["images"][0]]["animation"] is None


def test_deleting_image_or_batch_deletes_animation(anim):
    c, s = anim
    store = c.app.state.ctx.service.animations
    j1, j2 = _animate_job(c), _animate_job(c)
    k1, k2 = j1["images"][0], j2["images"][0]
    assert store.get(k1) and store.get(k2)
    assert c.delete(f"/api/images/{k1.replace('#', '/')}", headers=M).status_code == 200
    assert store.get(k1) is None and store.get(k2) is not None
    r = c.post("/api/images/batch", json={"action": "delete", "keys": [k2]}, headers=M)
    assert r.json()["results"][0]["ok"] and store.get(k2) is None
    assert not list((s.state_dir / "anim_cache").glob("*.png"))


def test_editing_an_animated_image_appends_suffix_and_creates_ficha(anim):
    c, s = anim
    job = _animate_job(c)
    key = job["images"][0]
    u = f"/api/animations/{key.replace('#', '/')}"
    c.put(u, json={"fps": 17, "loop": "pingpong", "tolerance": 33}, headers=M)
    r = c.post("/api/jobs", json={"prompt": "ponle sombrero", "edit_of": f"gen:{key}"}, headers=M)
    assert r.status_code == 202
    done = _wait_job(c, r.json()["jobs"][0]["id"])
    assert done["status"] == "done"
    kind, sent, kw = _backend(c).sent[-1]
    assert kind == "edit" and "ponle sombrero" in sent
    assert "Keep the exact same sprite sheet layout" in sent and kw["slug_source"] == "ponle sombrero"
    new_key = done["images"][0]
    assert new_key != key
    new = c.get(f"/api/animations/{new_key.replace('#', '/')}", headers=H).json()
    assert (new["cols"], new["rows"], new["fps"], new["loop"]) == (4, 2, 17, "pingpong")
    assert c.get(f"/api/animations/{new_key.replace('#', '/')}", headers=H).json()["rev"] == new["rev"]


def test_sse_changed_includes_animations_mtime(anim):
    c, s = anim
    store = c.app.state.ctx.service.animations
    data = _sse_changed(c, lambda: c.put("/api/animations/g_00000000000a/0", json={"cols": 2, "rows": 2}, headers=M))
    assert data["animations"] == store.mtime() and data["animations"] > 0


def test_delete_image_race_returns_404(anim, monkeypatch):
    from gemini_image_api.errors import GIAError, INVALID_REF
    c, s = anim

    def gone(ref):
        raise GIAError(INVALID_REF, "image vanished")
    monkeypatch.setattr(c.app.state.ctx.service, "delete_image", gone)
    r = c.delete("/api/images/g_00000000000a/0", headers=M)
    assert r.status_code == 404 and r.json()["code"] == "unknown_image"


def test_create_app_prunes_orphan_animations(anim):
    c, s = anim
    ctx = c.app.state.ctx
    store = ctx.service.animations
    store.put("g_ffffffffffff#0", {"cols": 2, "rows": 2, "mode": "sprite", "fps": 10})
    store.put("g_00000000000a#0", {"cols": 2, "rows": 2, "mode": "sprite", "fps": 10})
    create_app(settings=s, service=ctx.service, library=ctx.library, user_styles=ctx.user_styles,
               key="k3y", port=PORT)
    assert store.get("g_ffffffffffff#0") is None and store.get("g_00000000000a#0") is not None


# -- compositions (spec 2) ---------------------------------------------------------------

class CompBackend(FakeBackend):
    """generate/edit write composition_set() images into output_root: the master on a generation without
    refs, a piece (green) on 'Isolate only'/'solid flat', the plate (background) otherwise."""

    def __init__(self, settings, img):
        super().__init__(settings, img)
        from sprite_synth import composition_set, walk_sheet
        self.master, self.plate, self.piece, _ = composition_set()
        self.sheet = walk_sheet(True)
        self.sent: list[tuple] = []
        self.fail_prompts: dict[str, str] = {}   # prompt substring -> error code (raised once)

    def pick(self, prompt):
        if "sprite sheet" in prompt:
            return self.sheet
        if "Compose the scene" in prompt:
            return self.master
        return self.piece if "solid flat" in prompt else self.plate

    def save(self, kind, cid, prompt, kw, image):
        rid = StateStore.new_id("g")
        p = self.settings.output_root / f"{rid}_0.png"
        image.save(p)
        StateStore(self.settings.state_dir).append(GenerationRecord(
            id=rid, ts=datetime.now(timezone.utc).isoformat(), type=kind, conversation_id=cid,
            chat_metadata=["c"], prompt=prompt, style=None, style_text=None, aspect_hint=kw.get("aspect_hint"),
            model="gemini-flash", project=None, subdir=None, refs=[], images=[{"index": 0, "path": str(p)}],
            status="ok", code=None, gemini_text="", elapsed_s=1.0, parent_id=None,
            user_prompt=kw.get("slug_source")))
        img = SavedImage(p, image.width, image.height, p.stat().st_size, "PNG", True, None)
        return GenerateResult("ok", None, cid, [img], "done", 1.0, ["c"], record_id=rid)

    def _maybe_fail(self, prompt):
        from gemini_image_api.errors import GIAError
        for sub in list(self.fail_prompts):
            if sub in prompt:
                raise GIAError(self.fail_prompts.pop(sub), "injected")

    async def generate(self, prompt, refs, *, model, **kw):
        await self.gate.wait()
        self.sent.append(("generate", prompt, [Path(r.path).name for r in refs]))
        self._maybe_fail(prompt)
        return self.save("generate", StateStore.new_id("c"), prompt, kw, self.pick(prompt))

    async def edit(self, conversation_id, prompt, refs, *, model, **kw):
        await self.gate.wait()
        self.sent.append(("edit", prompt, [Path(r.path).name for r in refs]))
        self._maybe_fail(prompt)
        return self.save("edit", conversation_id, prompt, kw, self.pick(prompt))


@pytest.fixture
def comp(env):
    c, s, lib, p = env
    b = CompBackend(s, p)
    c.app.state.ctx.service.backend = b
    return c, s, b


def _comp(c, cid):
    r = c.get(f"/api/compositions/{cid}", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def _wait_comp(c, cid, pred, timeout=20.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        d = _comp(c, cid)
        if pred(d):
            return d
        time.sleep(0.05)
    raise AssertionError(f"composition condition not met: {_comp(c, cid)}")


def _settled(d):
    return d["master"]["status"] in ("done", "error") and all(
        ly["status"] in ("done", "error") for ly in d["layers"]) and all(
        ly["status"] != "pending" for ly in d["layers"]) or d["master"]["status"] == "error"


def _create_comp(c, **over):
    body = {"name": "Escena", "prompt": "Hermes entre tiendas", "pieces": ["Hermes", "Tarro"], **over}
    r = c.post("/api/compositions", json=body, headers=M)
    assert r.status_code == 201, r.text
    return r.json()


def _flow(c):
    d = _create_comp(c)
    return _wait_comp(c, d["id"], lambda x: all(ly["status"] == "done" for ly in x["layers"]))


def test_composition_create_and_full_flow(comp):
    c, s, b = comp
    b.gate.clear()
    d = _create_comp(c)
    assert d["id"].startswith("cmp_") and d["master"]["status"] in ("queued", "running")
    assert [ly["status"] for ly in d["layers"]] == ["pending"] * 3 and d["layers"][0]["role"] == "background"
    assert d["master_url"] is None and all(ly["image_url"] is None and ly["source_url"] is None for ly in d["layers"])
    lst = c.get("/api/compositions", headers=H).json()["items"]
    assert lst[0]["id"] == d["id"] and lst[0]["progress"] == {"done": 0, "total": 3, "errors": 0}
    assert lst[0]["cover_url"] is None and set(lst[0]) == {"id", "name", "rev", "progress", "cover_url", "created", "updated"}
    b.gate.set()
    d = _wait_comp(c, d["id"], lambda x: all(ly["status"] == "done" for ly in x["layers"]))
    assert [x[0] for x in b.sent] == ["generate", "edit", "generate", "generate"]
    assert "Compose the scene" in b.sent[0][1] and "Hermes; Tarro" in b.sent[0][1] and b.sent[0][2] == []
    assert "Remove Hermes, Tarro completely" in b.sent[1][1]
    master_file = f"{d['master']['image'].split('#')[0]}_0.png"
    assert "Isolate only Hermes" in b.sent[2][1] and b.sent[2][2] == [master_file]
    assert "Isolate only Tarro" in b.sent[3][1] and b.sent[3][2] == [master_file]
    assert (d["width"], d["height"]) == (800, 600) and d["master_url"] == "/api/image/" + d["master"]["image"].replace("#", "/")
    assert c.get(d["master_url"], headers=H).status_code == 200
    for ly in d["layers"]:
        assert ly["image_url"] == f"/api/compositions/{d['id']}/layers/{ly['id']}.png?rev={ly['rev']}"
        assert ly["source_url"].startswith("/api/image/g_") and ly["file"] and ly["bbox"] and ly["offset"] == [0, 0]
        assert "owner_pid" not in d and "source_exists" not in ly
    assert "master_exists" not in d
    png = c.get(d["layers"][1]["image_url"].split("?")[0], headers=H)
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert png.headers["cache-control"] == "private, max-age=3600"
    from PIL import Image
    with Image.open(io.BytesIO(png.content)) as im:
        assert im.mode == "RGBA" and im.size == (d["layers"][1]["bbox"][2] - d["layers"][1]["bbox"][0],
                                                 d["layers"][1]["bbox"][3] - d["layers"][1]["bbox"][1])
    pv = c.get(f"/api/compositions/{d['id']}/preview.png", headers=H)
    assert pv.status_code == 200 and pv.headers["cache-control"] == "private, max-age=3600"
    lst = c.get("/api/compositions", headers=H).json()["items"][0]
    assert lst["progress"] == {"done": 3, "total": 3, "errors": 0}
    assert lst["cover_url"] == f"/api/compositions/{d['id']}/preview.png?rev={d['rev']}"
    # gallery: display prompts and the «Composición · Escena» collection
    prompts = {i["prompt"] for i in c.get("/api/generations", headers=H).json()["items"]}
    assert {"Escena · imagen completa", "Escena · Fondo", "Escena · Hermes", "Escena · Tarro"} <= prompts
    lib = c.app.state.ctx.library
    coll = lib.get_collection(d["collection"])
    assert coll.name == "Composición · Escena" and len(lib.members(coll.id)) == 4
    assert _create_comp(c)["collection"] != d["collection"]


def test_composition_cover_is_master_thumb_when_no_layer_is_done(comp):
    c, s, b = comp
    b.fail_prompts["Remove Hermes"] = "usage_limit"   # stop code: the background fails and the queue drains
    d = _create_comp(c)
    d = _wait_comp(c, d["id"], lambda x: all(ly["status"] == "error" for ly in x["layers"]))
    assert {ly["error"]["code"] for ly in d["layers"]} == {"usage_limit"}
    item = c.get("/api/compositions", headers=H).json()["items"][0]
    assert item["progress"] == {"done": 0, "total": 3, "errors": 3}
    assert item["cover_url"] == "/api/thumb/" + d["master"]["image"].replace("#", "/")
    assert c.get(item["cover_url"], headers=H).status_code == 200


def test_composition_queue_full_creates_nothing(comp):
    c, s, b = comp
    b.gate.clear()
    for n in (8, 8, 2):
        assert c.post("/api/jobs", json={"prompt": "x", "n": n}, headers=M).status_code == 202
    colls = len(c.get("/api/collections", headers=H).json())
    r = c.post("/api/compositions", json={"prompt": "p", "pieces": ["a", "b"]}, headers=M)
    assert r.status_code == 429 and r.json()["code"] == "queue_full"
    assert c.get("/api/compositions", headers=H).json() == {"items": []}
    assert len(c.get("/api/collections", headers=H).json()) == colls
    b.gate.set()


def test_composition_create_validation_and_errors(comp):
    c, s, b = comp
    bad = [{"prompt": "p", "pieces": []}, {"prompt": "p", "pieces": list("abcdefg")},
           {"prompt": "p", "pieces": ["a", "A"]}, {"prompt": "", "pieces": ["a"]},
           {"prompt": "p", "pieces": ["a"], "refs": [{"gen": "gen:g_1#0"}] * 4},
           {"prompt": "p", "pieces": ["a"], "extra": 1}, {"prompt": "p", "pieces": "a"}]
    for body in bad:
        r = c.post("/api/compositions", json=body, headers=M)
        assert r.status_code == 400 and r.json()["code"] == "invalid_composition" and set(r.json()) == ERR6, body
    r = c.post("/api/compositions", json={"prompt": "p", "pieces": ["a"], "style": "nope"}, headers=M)
    assert r.status_code == 404 and r.json()["code"] == "unknown_style"
    r = c.post("/api/compositions", json={"prompt": "p", "pieces": ["a"], "model": "gemini-nope"}, headers=M)
    assert r.status_code == 404 and r.json()["code"] == "unknown_model"
    assert c.get("/api/compositions", headers=H).json() == {"items": []}
    assert c.post("/api/compositions", json={"prompt": "p", "pieces": ["a"]}, headers=H).status_code == 403
    r = c.get("/api/compositions/cmp_00000000", headers=H)
    assert r.status_code == 404 and r.json()["code"] == "unknown_composition"
    r = c.post("/api/compositions", content=b"{nope", headers={**M, "content-type": "application/json"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_json"
    r = c.post("/api/compositions", content=b"{}", headers={**M, "content-length": str(1024 * 1024),
                                                            "content-type": "application/json"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_composition"


def test_composition_master_failure_and_retry(comp):
    c, s, b = comp
    b.fail_prompts["Compose the scene"] = "content_blocked"
    d = _create_comp(c)
    d = _wait_comp(c, d["id"], lambda x: x["master"]["status"] == "error")
    assert d["master"]["error"]["code"] == "content_blocked" and all(ly["status"] == "pending" for ly in d["layers"])
    assert c.get("/api/compositions", headers=H).json()["items"][0]["progress"]["errors"] == 1
    r = c.post(f"/api/compositions/{d['id']}/retry", headers=M)
    assert r.status_code == 202
    d = _wait_comp(c, d["id"], lambda x: all(ly["status"] == "done" for ly in x["layers"]))
    assert c.post(f"/api/compositions/{d['id']}/retry", headers=M).json()["code"] == "invalid_composition"


def test_composition_regenerate_background_uses_reference_when_not_head(comp):
    c, s, b = comp
    d = _flow(c)
    bg = d["layers"][0]
    b.sent.clear()
    r = c.post(f"/api/compositions/{d['id']}/layers/{bg['id']}/regenerate", json={}, headers=M)
    assert r.status_code == 202 and r.json()["job"].startswith("j_")
    d2 = _wait_comp(c, d["id"], lambda x: x["layers"][0]["rev"] != bg["rev"] and x["layers"][0]["status"] == "done")
    kind, prompt, refs = b.sent[0]
    master_file = f"{d['master']['image'].split('#')[0]}_0.png"
    assert kind == "generate" and prompt.count("From the reference image, remove Hermes, Tarro") == 1
    assert refs == [master_file] and d2["layers"][0]["file"] != bg["file"]
    # no body at all is fine for regenerate (key_color optional)
    hermes = d["layers"][1]
    r = c.post(f"/api/compositions/{d['id']}/layers/{hermes['id']}/regenerate", json={"key_color": "magenta"}, headers=M)
    assert r.status_code == 202
    d3 = _wait_comp(c, d["id"], lambda x: x["layers"][1]["rev"] != hermes["rev"] and x["layers"][1]["status"] == "done")
    assert d3["layers"][1]["key_color"] == "magenta" and "magenta #FF00FF" in b.sent[1][1]
    for body in ({"key_color": "blue"}, {"nope": 1}):
        r = c.post(f"/api/compositions/{d['id']}/layers/{hermes['id']}/regenerate", json=body, headers=M)
        assert r.status_code == 400 and r.json()["code"] == "invalid_composition"
    r = c.post(f"/api/compositions/{d['id']}/layers/{bg['id']}/regenerate", json={"key_color": "green"}, headers=M)
    assert r.status_code == 400


def test_composition_correct_edit_when_head_reference_when_not(comp):
    c, s, b = comp
    d = _flow(c)
    hermes = d["layers"][1]
    b.sent.clear()
    url = f"/api/compositions/{d['id']}/layers/{hermes['id']}/correct"
    assert c.post(url, json={"prompt": "alas más grandes"}, headers=M).status_code == 202
    d2 = _wait_comp(c, d["id"], lambda x: x["layers"][1]["rev"] != hermes["rev"] and x["layers"][1]["status"] == "done")
    kind, prompt, refs = b.sent[0]
    assert kind == "edit" and "alas más grandes Keep the solid flat chroma green" in prompt and refs == []
    # someone continues the layer's conversation elsewhere: its image is no longer the head
    b.save("edit", d2["layers"][1]["conversation_id"], "x", {}, b.piece)
    assert c.post(url, json={"prompt": "tono azul"}, headers=M).status_code == 202
    _wait_comp(c, d["id"], lambda x: x["layers"][1]["rev"] != d2["layers"][1]["rev"] and x["layers"][1]["status"] == "done")
    kind, prompt, refs = b.sent[1]
    assert kind == "generate" and "Same image as the reference, with this change: tono azul." in prompt
    assert refs == [f"{d2['layers'][1]['image'].split('#')[0]}_0.png"]
    for body in ({}, {"prompt": ""}, {"prompt": "x" * 2001}, {"prompt": "x", "extra": 1}):
        r = c.post(url, json=body, headers=M)
        assert r.status_code == 400 and r.json()["code"] == "invalid_composition"


def test_composition_busy_layer_is_400(comp):
    c, s, b = comp
    d = _flow(c)
    hermes = d["layers"][1]
    base = f"/api/compositions/{d['id']}/layers/{hermes['id']}"
    b.gate.clear()
    assert c.post(base + "/regenerate", json={}, headers=M).status_code == 202
    assert _comp(c, d["id"])["layers"][1]["status"] in ("queued", "running")
    for path, body in (("/regenerate", {}), ("/correct", {"prompt": "x"})):
        r = c.post(base + path, json=body, headers=M)
        assert r.status_code == 400 and r.json()["code"] == "invalid_composition"
    assert c.patch(base, json={"tolerance": 40}, headers=M).status_code == 400
    b.gate.set()
    _wait_comp(c, d["id"], lambda x: x["layers"][1]["status"] == "done" and x["layers"][1]["rev"] != hermes["rev"])


def test_composition_failed_regenerate_keeps_layer_with_notice(comp):
    c, s, b = comp
    d = _flow(c)
    hermes = d["layers"][1]
    b.fail_prompts["Isolate only Hermes"] = "content_blocked"
    c.post(f"/api/compositions/{d['id']}/layers/{hermes['id']}/regenerate", json={}, headers=M)
    d2 = _wait_comp(c, d["id"], lambda x: x["layers"][1]["error"] is not None)
    ly = d2["layers"][1]
    assert ly["status"] == "done" and ly["error"]["code"] == "content_blocked" and ly["image_url"].endswith(f"rev={hermes['rev']}")


def test_composition_patch_order_visibility_offset(comp):
    c, s, b = comp
    d = _flow(c)
    cid, ids = d["id"], [ly["id"] for ly in d["layers"]]
    r = c.patch(f"/api/compositions/{cid}", json={"order": [ids[0], ids[2], ids[1]]}, headers=M)
    assert r.status_code == 200 and [ly["id"] for ly in r.json()["layers"]] == [ids[0], ids[2], ids[1]]
    assert r.json()["rev"] > d["rev"]
    for body in ({"order": [ids[1], ids[0], ids[2]]}, {"order": ids[:2]}, {"order": "x"}, {"nope": 1}, {"name": ""}):
        r = c.patch(f"/api/compositions/{cid}", json=body, headers=M)
        assert r.status_code == 400 and r.json()["code"] == "invalid_composition", body
    r = c.patch(f"/api/compositions/{cid}", json={"name": " Nueva "}, headers=M)
    assert r.json()["name"] == "Nueva"
    lurl = f"/api/compositions/{cid}/layers/{ids[1]}"
    old = _comp(c, cid)
    r = c.patch(lurl, json={"visible": False, "offset": [4, -6]}, headers=M)
    ly = next(x for x in r.json()["layers"] if x["id"] == ids[1])
    assert r.status_code == 200 and ly["visible"] is False and ly["offset"] == [4, -6]
    assert ly["rev"] > next(x for x in old["layers"] if x["id"] == ids[1])["rev"] and r.json()["rev"] > old["rev"]
    assert ly["image_url"].endswith(f"rev={ly['rev']}")
    r = c.patch(lurl, json={"tolerance": 40}, headers=M)
    assert r.status_code == 200
    assert next(x for x in r.json()["layers"] if x["id"] == ids[1])["tolerance"] == 40
    for body in ({"offset": [201, 0]}, {"offset": [1]}, {"tolerance": 4}, {"visible": "no"}, {"extra": 1}):
        r = c.patch(lurl, json=body, headers=M)
        assert r.status_code == 400 and r.json()["code"] == "invalid_composition", body
    assert c.patch(f"/api/compositions/{cid}/layers/{ids[0]}", json={"visible": False}, headers=M).status_code == 400
    assert c.patch(f"/api/compositions/{cid}/layers/ly_nope", json={"visible": True}, headers=M).status_code == 404


def test_composition_export_zip_coherent_with_scene_json(comp, settings):
    import zipfile
    from PIL import Image
    c, s, b = comp
    d = _flow(c)
    c.patch(f"/api/compositions/{d['id']}/layers/{d['layers'][2]['id']}", json={"visible": False, "offset": [5, 7]}, headers=M)
    d = _comp(c, d["id"])
    r = c.get(f"/api/compositions/{d['id']}/export.zip", headers=H)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert r.headers["cache-control"] == "no-store" and r.headers["content-disposition"].startswith("attachment; filename=")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == ["background.png", "composite.png", "hermes.png", "index.html",
                                         "musa-scene.js", "scene.json", "tarro.png"]
        scene = json.loads(zf.read("scene.json"))
        assert scene["version"] == 1 and scene["name"] == "Escena" and (scene["width"], scene["height"]) == (800, 600)
        assert [x["name"] for x in scene["layers"]] == ["Fondo", "Hermes", "Tarro"]
        assert [x["role"] for x in scene["layers"]] == ["background", "piece", "piece"]
        for x, ly in zip(scene["layers"], d["layers"]):
            assert x["id"] == ly["id"] and x["visible"] == ly["visible"]
            with Image.open(io.BytesIO(zf.read(x["file"]))) as im:
                assert im.size == (x["w"], x["h"])
            if ly["role"] == "piece":
                assert (x["x"], x["y"]) == (ly["bbox"][0] + ly["offset"][0], ly["bbox"][1] + ly["offset"][1])
        assert scene["layers"][0]["file"] == "background.png" and (scene["layers"][0]["w"], scene["layers"][0]["h"]) == (800, 600)
        assert scene["layers"][2]["visible"] is False and scene["layers"][2]["x"] - d["layers"][2]["bbox"][0] == 5
        with Image.open(io.BytesIO(zf.read("composite.png"))) as im:
            assert im.size == (800, 600)
    assert not _tmp_zips(settings)


def test_composition_delete_keeps_images_and_collection(comp, settings):
    c, s, b = comp
    d = _flow(c)
    before = c.get("/api/generations", headers=H).json()["total"]
    assert (Path(settings.state_dir) / "compositions" / d["id"]).is_dir()
    r = c.delete(f"/api/compositions/{d['id']}", headers=M)
    assert r.status_code == 200 and r.json() == {"id": d["id"], "deleted": True}
    assert not (Path(settings.state_dir) / "compositions" / d["id"]).exists()
    assert c.get("/api/generations", headers=H).json()["total"] == before == 5
    assert c.app.state.ctx.library.get_collection(d["collection"]) is not None
    assert c.get("/api/compositions", headers=H).json() == {"items": []}
    r = c.get(f"/api/compositions/{d['id']}", headers=H)
    assert r.status_code == 404 and r.json()["code"] == "unknown_composition"
    assert c.delete(f"/api/compositions/{d['id']}", headers=M).status_code == 404


def test_composition_master_deleted_blocks_regenerate(comp):
    c, s, b = comp
    d = _flow(c)
    gen, i = d["master"]["image"].split("#")
    assert c.delete(f"/api/images/{gen}/{i}", headers=M).status_code == 200
    d2 = _comp(c, d["id"])
    assert d2["master_url"] is None and all(ly["image_url"] for ly in d2["layers"])
    lid = d["layers"][1]["id"]
    r = c.post(f"/api/compositions/{d['id']}/layers/{lid}/regenerate", json={}, headers=M)
    assert r.status_code == 400 and r.json()["code"] == "invalid_composition" and "se borró" in r.json()["message"]
    r = c.post(f"/api/compositions/{d['id']}/layers/{lid}/correct", json={"prompt": "x"}, headers=M)
    assert r.status_code == 400
    # the processed PNG survives
    assert c.get(d2["layers"][1]["image_url"].split("?")[0], headers=H).status_code == 200


def test_sse_changed_includes_compositions_mtime(comp):
    c, s, b = comp
    store = c.app.state.ctx.service.compositions
    data = _sse_changed(c, lambda: _create_comp(c))
    assert data["compositions"] > 0 and data["compositions"] <= store.mtime()


def test_composition_hook_installed_and_recovery_on_startup(comp):
    c, s, b = comp
    ctx = c.app.state.ctx
    assert ctx.jobs.on_finish == ctx.compositions.on_job_finished and ctx.service.compositions is ctx.compositions.store
    d = _flow(c)
    ctx.service.compositions.update(d["id"], lambda x: x["layers"][1].update(status="queued", job="j_gone"))
    create_app(settings=s, service=ctx.service, library=ctx.library, user_styles=ctx.user_styles, key="k3y", port=PORT)
    ly = ctx.service.compositions.get(d["id"])["layers"][1]
    assert ly["status"] == "done" and ly["error"]["code"] == "interrupted"


def test_put_animation_oversized_body_is_400(env):
    c, *_ = env
    r = c.put("/api/animations/g_00000000000a/0", content=b"{}",
              headers={**M, "content-length": str(1024 * 1024), "content-type": "application/json"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_animation"


def test_index_versions_scene_script(env):
    c, *_ = env
    html = c.get("/", headers=H).text
    assert '/static/musa-scene.js?v=' in html and '"/static/musa-scene.js"' not in html


# -- animated compositions (spec 3) --------------------------------------------------------------

def _lurl(d, i):
    return f"/api/compositions/{d['id']}/layers/{d['layers'][i]['id']}"


def _motion(**kw):
    return {"type": "motion", **kw}


def _set_anim(c, d, i, body):
    return c.patch(_lurl(d, i), json={"animation": body}, headers=M)


def _add_states(c, d, i, n, texts=None):
    out = d
    for k in range(1, n + 1):
        r = c.post(_lurl(d, i) + "/states", json={"prompt": (texts or ["más"] * n)[k - 1]}, headers=M)
        assert r.status_code == 202, r.text
        out = _wait_comp(c, d["id"], lambda x, k=k: len(x["layers"][i]["animation"].get("states") or []) == k
                         and x["layers"][i]["status"] == "done")
    return out


def test_composition_animation_patch_motion_and_validation(comp):
    c, s, b = comp
    d = _flow(c)
    an = lambda r: r.json()["layers"][1]["animation"]
    assert all(ly["animation"] == {"type": "static"} for ly in d["layers"])
    r = _set_anim(c, d, 1, _motion(preset="bob"))
    assert r.status_code == 200 and an(r) == {"type": "motion", "preset": "bob", "amplitude": 6, "period": 2.0, "phase": 0}
    assert r.json()["rev"] > d["rev"]
    assert an(_set_anim(c, d, 1, _motion(preset="fall"))) == {
        "type": "motion", "preset": "fall", "amplitude": None, "period": 1.2, "phase": 0, "copies": 3}
    assert an(_set_anim(c, d, 1, _motion(preset="fall", amplitude=120, copies=5, period=0.5, phase=0.25)))["copies"] == 5
    assert an(_set_anim(c, d, 1, _motion(preset="sway", amplitude=10, period=3, phase=0.5)))["amplitude"] == 10
    assert an(_set_anim(c, d, 1, _motion(preset="pulse")))["amplitude"] == 0.04
    bad = [{"type": "motion"}, _motion(preset="x"), _motion(preset="bob", amplitude=61), _motion(preset="bob", amplitude=0.5),
           _motion(preset="fall", amplitude=5), _motion(preset="sway", amplitude=31), _motion(preset="pulse", amplitude=0.5),
           _motion(preset="bob", period=0.2), _motion(preset="bob", period=11), _motion(preset="bob", phase=1.5),
           _motion(preset="bob", copies=2), _motion(preset="fall", copies=6), _motion(preset="fall", copies=0),
           _motion(preset="bob", pivot=[1, 2]), _motion(preset="bob", amplitude="6"), _motion(preset="bob", amplitude=True),
           {"type": "frames", "image": "g_x#0"}, {"type": "states", "hold": 1}, {"type": "static", "x": 1},
           {"type": "nope"}, "bob", None, {}]
    for body in bad:
        r = _set_anim(c, d, 1, body)
        assert r.status_code == 400 and r.json()["code"] == "invalid_composition" and set(r.json()) == ERR6, body
    assert _set_anim(c, d, 0, _motion(preset="bob")).status_code == 400   # the background only takes static
    assert _set_anim(c, d, 0, {"type": "static"}).status_code == 200
    assert an(_set_anim(c, d, 1, {"type": "static"})) == {"type": "static"}
    assert c.patch(_lurl(d, 1), json={"visible": False, "animation": _motion(preset="bob")}, headers=M).json()["layers"][1]["visible"] is False


def test_composition_frames_flow_scene_and_effective_rev(comp):
    from gemini_image_api import scene_math as sm
    c, s, b = comp
    d = _flow(c)
    cid = d["id"]
    b.sent.clear()
    r = c.post(_lurl(d, 1) + "/frames", json={"action": "parpadea", "grid": "4x2"}, headers=M)
    assert r.status_code == 202 and r.json()["job"].startswith("j_")
    d2 = _wait_comp(c, cid, lambda x: x["layers"][1]["animation"]["type"] == "frames" and x["layers"][1]["status"] == "done")
    kind, prompt, refs = b.sent[0]
    hermes = d["layers"][1]
    assert kind == "generate" and "Hermes from the reference image, parpadea. Keep the exact same character design" in prompt
    assert "sprite sheet" in prompt and refs == [f"{hermes['image'].split('#')[0]}_0.png"]
    an = d2["layers"][1]["animation"]
    gen, i = an["image"].split("#")
    assert (an["frames"], an["fps"], an["loop"]) == (8, 10, "loop") and an["frame_w"] > 0 and an["frame_h"] > 0
    assert an["sheet_url"].startswith(f"/api/animations/{gen}/{i}/sheet.png?rev=") and "sheet_rev" not in an
    assert c.get(an["sheet_url"], headers=H).status_code == 200
    ficha = c.app.state.ctx.service.animations.get(an["image"])
    assert (ficha["cols"], ficha["rows"], ficha["key_color"], ficha["mode"]) == (4, 2, "green", "sprite")
    prompts = {x["prompt"] for x in c.get("/api/generations", headers=H).json()["items"]}
    assert "Escena · Hermes · frames" in prompts
    # scene.json with Musa URLs
    sc = c.get(f"/api/compositions/{cid}/scene.json", headers=H).json()
    assert sc["version"] == 1 and (sc["width"], sc["height"]) == (800, 600)
    assert [x["id"] for x in sc["layers"]] == [ly["id"] for ly in d["layers"]]
    assert sc["layers"][0]["file"] == d2["layers"][0]["image_url"] and sc["layers"][0]["animation"] == {"type": "static"}
    fa = sc["layers"][1]["animation"]
    assert fa["type"] == "frames" and fa["file"] == an["sheet_url"] and fa["frames"] == 8
    L = [hermes["bbox"][0] + hermes["offset"][0], hermes["bbox"][1] + hermes["offset"][1],
         hermes["bbox"][2] + hermes["offset"][0], hermes["bbox"][3] + hermes["offset"][1]]
    x, y, w, h = sm.frames_placement(L, ficha["frame_w"], ficha["frame_h"], ficha["frame_boxes"])
    assert abs(fa["x"] - x) <= 1 and abs(fa["y"] - y) <= 1 and abs(fa["w"] - w) <= 1 and abs(fa["h"] - h) <= 1
    # the effective rev follows the ficha (preview and cover)
    item = c.get("/api/compositions", headers=H).json()["items"][0]
    assert item["rev"] == d2["rev"] == _comp(c, cid)["rev"]
    assert c.put(f"/api/animations/{gen}/{i}", json={"tolerance": 40}, headers=M).status_code == 200
    d3 = _comp(c, cid)
    item3 = c.get("/api/compositions", headers=H).json()["items"][0]
    assert d3["rev"] > d2["rev"] and item3["rev"] == d3["rev"] and item3["cover_url"].endswith(f"rev={d3['rev']}")
    assert c.get(item3["cover_url"], headers=H).status_code == 200
    # deleting the sheet image: broken, treated as static
    assert c.delete(f"/api/images/{gen}/{i}", headers=M).status_code == 200
    d4 = _comp(c, cid)
    assert d4["layers"][1]["animation"] == {"type": "static", "broken": True}
    sc4 = c.get(f"/api/compositions/{cid}/scene.json", headers=H).json()
    assert sc4["layers"][1]["animation"] == {"type": "static"}


def test_composition_frames_validation_and_failure(comp):
    c, s, b = comp
    d = _flow(c)
    url = _lurl(d, 1) + "/frames"
    for body in ({"action": "x", "grid": "9x9"}, {"action": "", "grid": "4x2"}, {"action": "x" * 501, "grid": "4x2"},
                 {"grid": "4x2"}, {"action": "x", "grid": "4x2", "extra": 1}, {"action": "x"}):
        r = c.post(url, json=body, headers=M)
        assert r.status_code == 400 and r.json()["code"] == "invalid_composition", body
    assert c.post(_lurl(d, 0) + "/frames", json={"action": "x", "grid": "4x2"}, headers=M).status_code == 400
    assert c.post(f"/api/compositions/{d['id']}/layers/ly_nope/frames", json={"action": "x", "grid": "4x2"},
                  headers=M).status_code == 404
    b.fail_prompts["from the reference image, salta"] = "content_blocked"
    assert c.post(url, json={"action": "salta", "grid": "2x2"}, headers=M).status_code == 202
    d2 = _wait_comp(c, d["id"], lambda x: x["layers"][1]["error"] is not None)
    ly = d2["layers"][1]
    assert ly["status"] == "done" and ly["error"]["code"] == "content_blocked" and ly["animation"] == {"type": "static"}


def test_composition_states_flow(comp, settings):
    c, s, b = comp
    d = _flow(c)
    cid, base = d["id"], _lurl(d, 2)
    b.sent.clear()
    d1 = _add_states(c, d, 2, 1, ["tarro lleno"])
    kind, prompt, refs = b.sent[0]
    assert kind == "edit" and "tarro lleno Keep the solid flat chroma green" in prompt and refs == []
    an = d1["layers"][2]["animation"]
    st = an["states"][0]
    assert (an["type"], an["hold"], an["loop"]) == ("states", 0.8, "loop")
    assert set(st) == {"image", "conversation_id", "bbox", "offset", "file", "rev"} and st["offset"] == [0, 0]
    assert all(abs(a - b_) <= 3 for a, b_ in zip(st["bbox"], d["layers"][2]["bbox"]))
    png = c.get(base + "/states/1.png", headers=H)
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert png.headers["cache-control"] == "private, max-age=3600"
    assert "Escena · Tarro · estado 1" in {x["prompt"] for x in c.get("/api/generations", headers=H).json()["items"]}
    # the layer conversation moved on elsewhere: the last state is no longer the head -> reference generation
    b.save("edit", d1["layers"][2]["conversation_id"], "x", {}, b.piece)
    b.sent.clear()
    assert c.post(base + "/states", json={"prompt": "dos"}, headers=M).status_code == 202
    d2 = _wait_comp(c, cid, lambda x: len(x["layers"][2]["animation"].get("states") or []) == 2
                    and x["layers"][2]["status"] == "done")
    kind, prompt, refs = b.sent[0]
    assert kind == "generate" and "Same image as the reference, with this change: dos." in prompt
    assert refs == [f"{st['image'].split('#')[0]}_0.png"]
    assert "Escena · Tarro · estado 2" in {x["prompt"] for x in c.get("/api/generations", headers=H).json()["items"]}
    # hold / loop adjustments only when already states
    rev = d2["rev"]
    r = _set_anim(c, d, 2, {"type": "states", "hold": 1.5, "loop": "pingpong"})
    got = r.json()["layers"][2]["animation"]
    assert r.status_code == 200 and (got["hold"], got["loop"]) == (1.5, "pingpong") and len(got["states"]) == 2
    assert r.json()["rev"] > rev
    for body in ({"type": "states", "hold": 0.1}, {"type": "states", "hold": 11}, {"type": "states", "loop": "x"},
                 {"type": "states", "extra": 1}, {"type": "states", "states": []}):
        assert _set_anim(c, d, 2, body).status_code == 400, body
    assert _set_anim(c, d, 1, {"type": "states", "hold": 1}).status_code == 400
    # remove states
    gone = d2["layers"][2]["animation"]["states"][0]["file"]
    r = c.delete(base + "/states/1", headers=M)
    assert r.status_code == 200 and len(r.json()["layers"][2]["animation"]["states"]) == 1
    assert not (Path(settings.state_dir) / "compositions" / cid / gone).exists()
    assert c.get(base + "/states/2.png", headers=H).status_code == 404
    r = c.delete(base + "/states/1", headers=M)
    assert r.json()["layers"][2]["animation"] == {"type": "static"}
    for k, code in (("1", 404), ("0", 400), ("abc", 400), ("-1", 400)):
        r = c.delete(base + f"/states/{k}", headers=M)
        assert r.status_code == code and r.json()["code"] == ("unknown_composition" if code == 404 else "invalid_composition")
    assert c.get(base + "/states/1.png", headers=H).status_code == 404


def test_composition_states_limits_exclusivity_and_resets(comp, settings):
    c, s, b = comp
    d = _flow(c)
    cid = d["id"]
    # exclusivity with frames
    assert c.post(_lurl(d, 1) + "/frames", json={"action": "parpadea", "grid": "2x2"}, headers=M).status_code == 202
    _wait_comp(c, cid, lambda x: x["layers"][1]["animation"]["type"] == "frames")
    assert c.post(_lurl(d, 1) + "/states", json={"prompt": "x"}, headers=M).status_code == 400
    # six states at most
    d6 = _add_states(c, d, 2, 6)
    r = c.post(_lurl(d, 2) + "/states", json={"prompt": "otro"}, headers=M)
    assert r.status_code == 400 and r.json()["code"] == "invalid_composition"
    assert c.post(_lurl(d, 2) + "/frames", json={"action": "x", "grid": "4x2"}, headers=M).status_code == 400
    for body in ({}, {"prompt": ""}, {"prompt": "x" * 2001}, {"prompt": "x", "extra": 1}):
        assert c.post(_lurl(d, 2) + "/states", json=body, headers=M).status_code == 400
    assert c.post(_lurl(d, 0) + "/states", json={"prompt": "x"}, headers=M).status_code == 400
    # a tolerance change re-cuts the states too
    old = d6["layers"][2]["animation"]["states"]
    r = c.patch(_lurl(d, 2), json={"tolerance": 40}, headers=M)
    new = r.json()["layers"][2]["animation"]["states"]
    assert r.status_code == 200 and len(new) == 6 and all(n["file"] != o["file"] and n["rev"] > o["rev"] for n, o in zip(new, old))
    root = Path(settings.state_dir) / "compositions" / cid
    assert all((root / n["file"]).is_file() for n in new) and not any((root / o["file"]).exists() for o in old)
    # regenerate returns states to static and drops their files; frames survive a regenerate
    r = c.post(_lurl(d, 2) + "/regenerate", json={}, headers=M)
    assert r.status_code == 202
    d7 = _wait_comp(c, cid, lambda x: x["layers"][2]["animation"]["type"] == "static" and x["layers"][2]["status"] == "done")
    assert not any((root / n["file"]).exists() for n in new)
    c.post(_lurl(d, 1) + "/regenerate", json={}, headers=M)
    _wait_comp(c, cid, lambda x: x["layers"][1]["rev"] != d7["layers"][1]["rev"] and x["layers"][1]["status"] == "done")
    assert _comp(c, cid)["layers"][1]["animation"]["type"] == "frames"
    # correct on a states layer also returns it to static
    _add_states(c, d7, 2, 1)
    c.post(_lurl(d, 2) + "/correct", json={"prompt": "más rojo"}, headers=M)
    _wait_comp(c, cid, lambda x: x["layers"][2]["animation"]["type"] == "static" and x["layers"][2]["status"] == "done")


def _index_scene(html: str) -> dict:
    start = html.index('id="scene-data">') + len('id="scene-data">')
    return json.loads(html[start:html.index("</script>", start)])


def test_composition_export_package_with_animation_and_escaping(comp, settings):
    import zipfile
    from gemini_image_api.styles import PACKAGE_ROOT
    c, s, b = comp
    name = "</script><b>x"
    d = _create_comp(c, name=name, pieces=["Hermes", "Tarro", "Luz"])
    d = _wait_comp(c, d["id"], lambda x: all(ly["status"] == "done" for ly in x["layers"]))
    assert c.patch(_lurl(d, 1), json={"animation": _motion(preset="sway")}, headers=M).status_code == 200
    _add_states(c, d, 2, 1)
    assert c.post(_lurl(d, 3) + "/frames", json={"action": "brilla", "grid": "4x2"}, headers=M).status_code == 202
    d = _wait_comp(c, d["id"], lambda x: x["layers"][3]["animation"]["type"] == "frames" and x["layers"][3]["status"] == "done")
    r = c.get(f"/api/compositions/{d['id']}/export.zip", headers=H)
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = sorted(zf.namelist())
        assert names == ["background.png", "composite.png", "hermes.png", "index.html", "luz-frames.png", "luz.png",
                         "musa-scene.js", "scene.json", "tarro-1.png", "tarro.png"]
        scene = json.loads(zf.read("scene.json"))
        assert zf.read("musa-scene.js") == (PACKAGE_ROOT / "web" / "musa-scene.js").read_bytes()
        page = zf.read("index.html").decode("utf-8")
        for ly in scene["layers"]:
            assert ly["file"] in names and ly["animation"]["type"] in ("static", "motion", "frames", "states")
        by = {x["name"]: x for x in scene["layers"]}
        assert by["Hermes"]["animation"]["type"] == "motion" and by["Hermes"]["animation"]["preset"] == "sway"
        piv = by["Hermes"]["animation"]["pivot"]
        assert piv == [by["Hermes"]["x"] + by["Hermes"]["w"] / 2, by["Hermes"]["y"] + by["Hermes"]["h"]]
        ts = by["Tarro"]["animation"]
        assert ts["type"] == "states" and [x["file"] for x in ts["states"]] == ["tarro-1.png"] and ts["hold"] == 0.8
        assert by["Luz"]["animation"]["type"] == "frames" and by["Luz"]["animation"]["file"] == "luz-frames.png"
        assert zf.getinfo("luz-frames.png").file_size > 0
    # the page: JSON inlined and escaped, name HTML-escaped in <title>
    assert "</script><b>" not in page and "\\u003c/script\\u003e\\u003cb\\u003ex" in page
    assert "<title>&lt;/script&gt;&lt;b&gt;x</title>" in page
    assert '<div id="scene" style="max-width:800px;margin:auto">' in page and '<script src="musa-scene.js"></script>' in page
    assert "&" not in page[page.index('id="scene-data">'):page.index('<script src="musa-scene.js">')]
    assert _index_scene(page) == scene and scene["name"] == name
    assert not _tmp_zips(settings)


def test_composition_scene_webp_is_animated_72_frames(comp, settings):
    from PIL import Image
    c, s, b = comp
    d = _flow(c)
    static = c.get(f"/api/compositions/{d['id']}/scene.webp", headers=H)
    assert static.status_code == 200
    with Image.open(io.BytesIO(static.content)) as im:
        assert im.n_frames == 1
    c.patch(_lurl(d, 1), json={"animation": _motion(preset="bob")}, headers=M)
    r = c.get(f"/api/compositions/{d['id']}/scene.webp", headers=H)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/webp"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-disposition"] == 'attachment; filename="scene-escena.webp"'
    with Image.open(io.BytesIO(r.content)) as im:
        assert 1 < im.n_frames <= 72 and im.width <= 800
        total = 0
        for k in range(im.n_frames):
            im.seek(k)
            im.load()
            total += im.info["duration"]
        assert total == 6000
    assert not _tmp_zips(settings)
    r = c.get("/api/compositions/cmp_00000000/scene.webp", headers=H)
    assert r.status_code == 404 and r.json()["code"] == "unknown_composition"
    assert c.get("/api/compositions/cmp_00000000/scene.json", headers=H).status_code == 404


def test_composition_scene_routes_before_ready_are_400(comp):
    c, s, b = comp
    b.gate.clear()
    d = _create_comp(c)
    for path in ("scene.json", "scene.webp", "export.zip"):
        r = c.get(f"/api/compositions/{d['id']}/{path}", headers=H)
        assert r.status_code == 400 and r.json()["code"] == "invalid_composition", path
    b.gate.set()


def test_composition_odd_ids_and_indexes_never_500(comp):
    c, s, b = comp
    d = _flow(c)
    cid, lid = d["id"], d["layers"][1]["id"]
    base = f"/api/compositions/{cid}/layers/{lid}"
    gets = ["/api/compositions/..%2f../layers/x.png", f"/api/compositions/{cid}/layers/..%2f..%2fx.png",
            f"/api/compositions/{cid}/layers/%2e%2e.png", "/api/compositions/%2e%2e/scene.json",
            "/api/compositions/..%2f..%2fetc%2fpasswd/preview.png", f"/api/compositions/{cid}/layers/..%2fx/states/1.png",
            base + "/states/99999999999999999999999.png", base + "/states/-1.png", base + "/states/abc.png",
            base + "/states/1e3.png", base + "/states/0.png", base + "/states/%2e%2e.png"]
    for p in gets:
        r = c.get(p, headers=H)
        assert r.status_code in (400, 404, 405), (p, r.status_code)
    for k, code in (("99999999999999999999999", 404), ("-1", 400), ("abc", 400), ("1e3", 400), ("0", 400), ("%2e%2e", 400)):
        r = c.delete(base + f"/states/{k}", headers=M)
        assert r.status_code == code, (k, r.status_code)
    for p in (f"/api/compositions/{cid}/layers/..%2fx", f"/api/compositions/{cid}/layers/%2e%2e"):
        assert c.patch(p, json={"visible": True}, headers=M).status_code in (400, 404, 405)
        assert c.post(p + "/regenerate", json={}, headers=M).status_code in (400, 404, 405)


def test_composition_animation_patch_while_busy_is_400(comp):
    c, s, b = comp
    d = _flow(c)
    b.gate.clear()
    assert c.post(_lurl(d, 1) + "/regenerate", json={}, headers=M).status_code == 202
    r = _set_anim(c, d, 1, _motion(preset="bob"))
    assert r.status_code == 400 and r.json()["code"] == "invalid_composition"
    assert c.patch(_lurl(d, 1), json={"visible": False}, headers=M).status_code == 200   # other fields stay allowed
    b.gate.set()
    d2 = _wait_comp(c, d["id"], lambda x: x["layers"][1]["status"] == "done" and x["layers"][1]["rev"] != d["layers"][1]["rev"])
    assert d2["layers"][1]["animation"] == {"type": "static"}


def test_composition_concurrent_regenerate_second_is_400(comp):
    import threading
    c, s, b = comp
    d = _flow(c)
    b.gate.clear()
    codes, barrier = [], threading.Barrier(2)

    def post():
        barrier.wait()
        codes.append(c.post(_lurl(d, 1) + "/regenerate", json={}, headers=M).status_code)
    ts = [threading.Thread(target=post) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    b.gate.set()
    assert sorted(codes) == [202, 400]
    _wait_comp(c, d["id"], lambda x: x["layers"][1]["status"] == "done" and x["layers"][1]["rev"] != d["layers"][1]["rev"])
    assert len([x for x in b.sent if "Isolate only Hermes" in x[1]]) == 2   # the initial one + exactly one regeneration


def test_composition_preview_needs_a_done_layer(comp):
    c, s, b = comp
    b.gate.clear()
    d = _create_comp(c)
    r = c.get(f"/api/compositions/{d['id']}/preview.png", headers=H)
    assert r.status_code == 404 and r.json()["code"] == "unknown_composition"
    b.gate.set()


def test_composition_piece_named_with_path_chars_is_slugified_in_zip(comp):
    import zipfile
    c, s, b = comp
    d = _create_comp(c, pieces=["../../x", "Tarro"])
    d = _wait_comp(c, d["id"], lambda x: all(ly["status"] == "done" for ly in x["layers"]))
    r = c.get(f"/api/compositions/{d['id']}/export.zip", headers=H)
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = zf.namelist()
        scene = json.loads(zf.read("scene.json"))
    assert "x.png" in names and all("/" not in n and ".." not in n and "\\" not in n for n in names)
    assert all("/" not in ly["file"] and ".." not in ly["file"] for ly in scene["layers"])


def test_composition_frames_ficha_without_rev_does_not_500(comp):
    import zipfile
    c, s, b = comp
    d = _flow(c)
    assert c.post(_lurl(d, 1) + "/frames", json={"action": "parpadea", "grid": "4x2"}, headers=M).status_code == 202
    d = _wait_comp(c, d["id"], lambda x: x["layers"][1]["animation"]["type"] == "frames")
    key = d["layers"][1]["animation"]["image"]
    store = c.app.state.ctx.service.animations
    data = json.loads(store.file.read_text())
    del data["animations"][key]["rev"]
    store.file.write_text(json.dumps(data))
    r = c.get(f"/api/compositions/{d['id']}/scene.json", headers=H)
    assert r.status_code == 200
    fa = r.json()["layers"][1]["animation"]
    assert fa["type"] == "frames" and "rev=" not in fa["file"]
    got = _comp(c, d["id"])["layers"][1]["animation"]
    assert got["type"] == "frames" and "rev=" not in got["sheet_url"] and "sheet_rev" not in got
    assert c.get("/api/compositions", headers=H).status_code == 200
    z = c.get(f"/api/compositions/{d['id']}/export.zip", headers=H)
    assert z.status_code == 200   # the sheet cannot be resolved without a rev: exported as a static layer
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        assert json.loads(zf.read("scene.json"))["layers"][1]["animation"] == {"type": "static"}
