import asyncio
import logging
import base64
import json
import tempfile
from pathlib import Path
from typing import Annotated

import anyio
from fastmcp import FastMCP

from fastmcp.server.context import Context
from fastmcp.tools import ToolResult
from mcp.types import ImageContent, TextContent
from pydantic import Field

from .client import Backend, build_backend
from .config import Settings, load_settings
from .errors import AUTH_FAILED, INVALID_COMPOSITION, INVALID_REF, INVALID_STYLE, GIAError, error_payload
from .logsetup import setup_logging
from .service import ImageService, to_payload
from .storage import check_copy_dest, copy_file
from .compositions import CompositionManager, DirectRunner, public_view
from .library import LibraryStore
from .state import StateStore
from .style_store import UserStyleStore
from .refs import SVG_NOT_SUPPORTED, resolve_refs
from .styles import AspectHint, Style
from .applauncher import port_free, running_app, serve_app


INSTRUCTIONS = """\
Generate and edit images with the user's Gemini web session.
Flow: generate_image(prompt) -> returns conversation_id + saved image paths (+ a thumbnail image block).
Refine the SAME image with edit_image(conversation_id, prompt). Recover old ids with list_generations.
Styles/templates: call list_styles / list_templates, then pass style= / template= + template_vars=.
Style Gems: list_style_gems shows which styles have a persistent Gemini Gem (system instructions);
style_gem(style, enabled, instructions?) creates/updates/deletes it (enabled=false removes it).
Generations with a Gem style include "warning" in the payload when the Gem no longer exists
(code gem_missing; the image is still generated, without the Gem).
refs (max 10): local image paths (under output_root or the working dir), public https URLs, or
gen:<id>[#index] to reuse an image from list_generations.
aspect_hint: one of 1:1, 4:5, 3:4, 2:3, 9:16, 5:4, 4:3, 3:2, 16:9, 21:9 (a hint; Gemini usually follows it).
copy_to: optional path (dir or file) inside the working dir to also copy the result there.
Every response has quota {window_5h_pct, weekly_pct, resets_at_*}; at GIA_QUOTA_STOP_PCT the server
returns quota_guard instead of calling Gemini.
On error read code/retryable/next_action. Key codes: auth_expired -> auth_login; usage_limit/quota_guard ->
stop until reset; generated_not_saved -> retry_download (free); temporarily_blocked -> wait, do not loop.
Organise results with collection=<name> (created if missing); list_collections / add_to_collection manage them. Styles that have a reference image send it to Gemini as the first ref.
Collections: rename_collection, delete_collection (images stay), remove_from_collection(image, collection).
Images: get_image(image) reads one entry, favorite_image(image, favorite) stars it, delete_image(image) removes the
local file and thumbnail (Gemini's copy is untouched; use delete_conversation for that). image = gen:<id>[#index].
Batches: generate_batch(prompts[1..8], style?, collection?, ...) runs them sequentially; it stops on quota/auth errors.
Your own styles: create_style(name, instructions, description?, family?, image?), update_style(style, ...), delete_style(style)
(built-in styles are read-only); image = allowed local path, public https URL or gen:<id>[#index]. describe_style(style, prompt?)
shows a style and the exact prompt it would send, without calling Gemini.
Saved prompts (reusable texts): list_prompts, save_prompt(name, text), update_prompt(prompt, name?, text?),
delete_prompt(prompt); prompt = id (pr_xxxxxxxx) or name (case-insensitive). Read a saved text from list_prompts and pass it
as the prompt of generate_image/edit_image/generate_batch.
Animations: generate_animation(prompt, grid="4x2"|"3x3"|"2x2", mode="sprite"|"scene", key_color="green"|"magenta") asks Gemini for a
sprite sheet and detects its frames; set_animation(image, cols, rows, ...) converts any existing image or adjusts an animation (fps, loop,
tolerance, key color; no quota); export_animation(image, copy_to=<dir>) writes <name>.png (one-row sheet) + <name>.json and never overwrites.
Use key_color=magenta for green characters; edit_image on an animated image keeps the sheet layout automatically.
Compositions: generate_composition(prompt, pieces=[1-6 names], name?, refs? (max 3), style?, aspect_hint?, model?) makes the full scene, then one
image per layer (background + one per piece, each in its own image, background first) and returns {status: ok|partial, composition, quota};
it spends 2 + len(pieces) images and runs sequentially (a quota/auth stop leaves the rest in error: status partial). list_compositions /
get_composition(composition=<id or name>) read them; export_composition(composition, copy_to=<dir>) writes a zip (layer PNGs, composite.png,
scene.json, musa-scene.js, index.html) and never overwrites; webp=true also writes scene-<name>.webp (animated). Pieces can be animated without
spending quota: set_layer_animation(composition, layer=<id or name>, animation={type: static} | {type: motion, preset: bob|sway|pulse|fall,
amplitude?, period?, phase?, copies?}); frames and states (they spend images) are made from Musa. Pick pieces that do not touch each other.
"""

Prompt = Annotated[str, Field(description="What to draw (any language). For edit_image: the change to apply.")]
Refs = Annotated[list[str] | None, Field(description="Reference images (max 10): local paths under output_root/working dir, public https URLs, or gen:<id>[#index] from list_generations")]
StyleArg = Annotated[str | None, Field(description="Preset name from list_styles")]
StyleText = Annotated[str | None, Field(description="Free-form style words appended to the prompt")]
TemplateArg = Annotated[str | None, Field(description="Template name from list_templates (replaces prompt)")]
TemplateVars = Annotated[dict[str, str] | None, Field(description="Values for every var the template lists")]
Aspect = Annotated[AspectHint | None, Field(description="Aspect ratio hint")]
ModelArg = Annotated[str | None, Field(description="Chat model from list_models; default gemini-flash")]
Project = Annotated[str | None, Field(description="Subfolder of output_root to save into")]
Subdir = Annotated[str | None, Field(description="Extra subfolder under project")]
CopyTo = Annotated[str | None, Field(description="Also copy the result here (dir or file path inside the working dir)")]
ReturnImage = Annotated[bool, Field(description="Attach a 512px JPEG thumbnail image block")]
CollectionArg = Annotated[str | None, Field(description="Collection name or id to file the result under (created if missing)")]
CollectionFilter = Annotated[str | None, Field(description="Only images in this collection (name or id; must already exist)")]
StyleId = Annotated[str, Field(description="Style id from list_styles")]
ImageRef = Annotated[str, Field(description="Image: gen:<id>[#index] from list_generations")]
StyleImage = Annotated[str | None, Field(description="Reference image: allowed local path, public https URL or gen:<id>[#index] (SVG is converted to PNG)")]


async def resolve_style_image(svc: ImageService, image: str) -> tuple[bytes, str]:
    """Fetch a style reference image (allowed path, https URL or gen: ref) as (bytes, name)."""
    with tempfile.TemporaryDirectory() as td:
        try:
            (ref,) = await resolve_refs([image], svc.settings, Path(td), lookup=svc.state.generation,
                                        is_deleted=svc.library.is_deleted)
            path = ref.path
        except GIAError as e:
            # validate_local_ref rejects SVG only after the allow-list/size checks passed, so the
            # path is trusted; styles convert SVG to PNG like web uploads.
            if e.code == INVALID_REF and str(e) == SVG_NOT_SUPPORTED and e.details.get("path"):
                path = Path(e.details["path"])
            else:
                raise
        return path.read_bytes(), path.name


def style_dict(style: Style) -> dict:
    return {"id": style.id, "name": style.name, "family": style.family, "description": style.description,
            "instructions": style.append, "source": style.source, "has_image": style.ref is not None}


def require_user_style(svc: ImageService, style_id: str, verb: str) -> Style:
    """unknown_style if the id does not exist, invalid_style if it is built-in; else the user style."""
    style = svc._style_or_raise(style_id)
    if style.source != "user":
        raise GIAError(INVALID_STYLE, f"style {style_id!r} is built-in and cannot be {verb}")
    return style


def _gen_error(e: GIAError) -> dict:
    return {**error_payload(e), "conversation_id": None, "images": [], "gemini_text": "", "elapsed_s": 0.0}


def _gen_result(payload: dict, thumbnails: list[bytes] = ()) -> ToolResult:
    """Structured payload, the same JSON as text (clients without structured output), and one image
    block per thumbnail: multimodal agents see the image instead of reading base64 as text."""
    content = [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]
    content += [ImageContent(type="image", data=base64.b64encode(t).decode(), mime_type="image/jpeg")
                for t in thumbnails]
    return ToolResult(content=content, structured_content=payload)


def _progress_cb(ctx: Context | None):
    """Adapt backend progress(stage, fraction) to MCP progress notifications."""
    if ctx is None:
        return None

    async def cb(stage: str, fraction: float) -> None:
        await ctx.report_progress(fraction, 1.0, stage)

    return cb


def create_server(backend: Backend, settings: Settings) -> FastMCP:
    mcp = FastMCP("gemini-image-api", instructions=INSTRUCTIONS)
    svc = ImageService(backend, settings, StateStore(settings.state_dir),
                       LibraryStore(settings.state_dir), UserStyleStore(settings.state_dir))
    compositions = CompositionManager(svc, svc.compositions, DirectRunner(svc))
    RO = {"readOnlyHint": True, "openWorldHint": False}

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
    async def auth_status() -> dict:
        """Session status: validity, models, account status, cookie age, Google quota (5h/weekly %); app_url = Musa URL when running."""
        status = await backend.status()
        info = running_app(settings)
        status["app_url"] = info["url"] if info else None
        return status

    @mcp.tool(annotations={"openWorldHint": True})
    async def auth_login() -> dict:
        """Open the user's Firefox (dedicated profile) to sign in to Gemini; closes itself once logged in."""
        from .auth import login_guided
        try:
            jar = await asyncio.to_thread(login_guided, settings)
        except GIAError as e:
            return error_payload(e)
        except Exception as e:
            return error_payload(GIAError(AUTH_FAILED, str(e)))
        return {"status": "ok", "cookies": len(jar)}

    @mcp.tool(annotations=RO)
    async def list_styles() -> dict:
        """Style presets {name: description}; pass the name as style= to generate_image/edit_image."""
        return {n: s.description for n, s in svc.catalog().styles.items()}

    @mcp.tool(annotations=RO)
    async def list_templates() -> dict:
        """Prompt templates {name: {description, vars}}; pass template= and template_vars= with every var."""
        return {n: {"description": t.description, "vars": t.vars} for n, t in svc.catalog().templates.items()}

    @mcp.tool(annotations=RO)
    async def list_style_gems() -> dict:
        """Style Gems (persistent Gemini instructions per style): [{style, gem_id, name, custom}]."""
        return {"status": "ok", "gems": svc.list_style_gems()}

    @mcp.tool(annotations={"openWorldHint": True})
    async def style_gem(style: Annotated[str, Field(description="Style name from list_styles")],
                        enabled: Annotated[bool, Field(description="true: create/update the Gem; false: delete it")],
                        instructions: Annotated[str | None, Field(description="Custom Gem instructions; omit for the style's default template, empty string to restore it")] = None) -> dict:
        """Enable or disable a style's Gem; generations with that style then use it automatically."""
        try:
            return {"status": "ok", **await svc.set_style_gem(style, enabled, instructions=instructions)}
        except GIAError as e:
            return error_payload(e)


    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
    async def list_models() -> dict:
        """Chat models available on the account (the chat model determines the image variant)."""
        try:
            models = await backend.list_models()
            requested = settings.default_model
            try:
                resolved = await backend.resolve_model_name(requested)
            except Exception:
                resolved = requested
            return {"status": "ok", "code": None, "models": models,
                    "resolved_default": {"requested": requested, "resolved": resolved or requested}}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations=RO)
    async def list_generations(
        query: Annotated[str | None, Field(description="Case-insensitive text to find in the prompt")] = None,
        project: Annotated[str | None, Field(description="Only this output project")] = None,
        since: Annotated[str | None, Field(description="ISO date/time lower bound, e.g. 2026-11-09")] = None,
        status: Annotated[str | None, Field(description="ok | error | generated_not_saved")] = None,
        collection: CollectionFilter = None,
        model: Annotated[str | None, Field(description="Only generations made with this exact model id (see list_models)")] = None,
        limit: Annotated[int, Field(description="1-100, newest first")] = 20,
        offset: Annotated[int, Field(description="Skip this many results")] = 0,
    ) -> dict:
        """Past generations (newest first) with ids usable as refs (gen:<id>) and conversation_ids for edit_image."""
        try:
            return {"status": "ok", "generations": svc.list_generations(
                query=query, project=project, since=since, status=status, collection=collection,
                model=model, limit=limit, offset=offset)}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations=RO)
    async def list_collections() -> dict:
        """Collections (tags) to organise images: [{id, name, description, count}]."""
        return {"status": "ok", "collections": svc.list_collections()}

    @mcp.tool(annotations={"idempotentHint": True, "openWorldHint": False})
    async def add_to_collection(
        image: Annotated[str, Field(description="Image to file: gen:<id>[#index] from list_generations")],
        collection: Annotated[str, Field(description="Collection name or id (created if missing)")],
    ) -> dict:
        """Add an existing image to a collection (idempotent)."""
        try:
            return {"status": "ok", **svc.add_to_collection(image, collection)}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"openWorldHint": True})
    async def generate_image(prompt: Prompt, refs: Refs = None, style: StyleArg = None,
                             style_text: StyleText = None, template: TemplateArg = None,
                             template_vars: TemplateVars = None, aspect_hint: Aspect = None,
                             model: ModelArg = None, project: Project = None, subdir: Subdir = None,
                             copy_to: CopyTo = None, collection: CollectionArg = None,
                             return_image: ReturnImage = True,
                             ctx: Context | None = None) -> ToolResult:
        """Generate a new image in a fresh Gemini chat; saves it under output_root and returns paths + conversation_id."""
        try:
            sr = await svc.generate(prompt=prompt, refs=refs, style=style, style_text=style_text,
                                    template=template, template_vars=template_vars, aspect_hint=aspect_hint,
                                    model=model, project=project, subdir=subdir, copy_to=copy_to,
                                    collection=collection, progress=_progress_cb(ctx))
            return _gen_result(to_payload(sr), [s.thumbnail for s in sr.result.images if return_image and s.thumbnail])
        except GIAError as e:
            return _gen_result(_gen_error(e))

    @mcp.tool(annotations={"openWorldHint": True})
    async def edit_image(conversation_id: Annotated[str, Field(description="From generate_image or list_generations")],
                         prompt: Prompt, refs: Refs = None, style: StyleArg = None,
                         style_text: StyleText = None, template: TemplateArg = None,
                         template_vars: TemplateVars = None, aspect_hint: Aspect = None,
                         model: ModelArg = None, project: Project = None, subdir: Subdir = None,
                         copy_to: CopyTo = None, collection: CollectionArg = None,
                         return_image: ReturnImage = True,
                         ctx: Context | None = None) -> ToolResult:
        """Edit the image of an existing conversation (keeps the same scene); returns the new image."""
        try:
            sr = await svc.edit(conversation_id, prompt=prompt, refs=refs, style=style, style_text=style_text,
                                template=template, template_vars=template_vars, aspect_hint=aspect_hint,
                                model=model, project=project, subdir=subdir, copy_to=copy_to,
                                collection=collection, progress=_progress_cb(ctx))
            return _gen_result(to_payload(sr), [s.thumbnail for s in sr.result.images if return_image and s.thumbnail])
        except GIAError as e:
            return _gen_result(_gen_error(e))

    @mcp.tool(annotations={"openWorldHint": True})
    async def generate_animation(
        prompt: Annotated[str, Field(description="Subject and action, e.g. 'astronaut cat walking to the right'")],
        grid: Annotated[str, Field(description="Sheet grid: 4x2 (8 frames, 16:9), 3x3 (9 frames, 1:1) or 2x2 (4 frames, 1:1)")] = "4x2",
        mode: Annotated[str, Field(description="sprite: background removed, frames centred; scene: opaque")] = "sprite",
        key_color: Annotated[str, Field(description="sprite only: green (default) or magenta (for green characters)")] = "green",
        refs: Refs = None, style: StyleArg = None, style_text: StyleText = None, model: ModelArg = None,
        project: Project = None, subdir: Subdir = None, copy_to: CopyTo = None,
        collection: CollectionArg = None, return_image: ReturnImage = True,
        ctx: Context | None = None) -> ToolResult:
        """Generate a looping sprite-sheet animation (one image); returns the generate_image payload plus animations: [{image, frames, status, frame_w, frame_h}]."""
        try:
            sr = await svc.generate(prompt=prompt, refs=refs, style=style, style_text=style_text,
                                    model=model, project=project, subdir=subdir, copy_to=copy_to,
                                    collection=collection, progress=_progress_cb(ctx),
                                    animation={"grid": grid, "mode": mode, "key_color": key_color})
            payload = to_payload(sr)
            animations = []
            for key in svc.result_keys(sr.result):
                ficha = svc.animations.get(key)
                if ficha is not None:
                    animations.append({"image": f"gen:{key}", "frames": ficha["frames"],
                                       "status": ficha["status"], "frame_w": ficha["frame_w"],
                                       "frame_h": ficha["frame_h"]})
            payload["animations"] = animations
            return _gen_result(payload, [s.thumbnail for s in sr.result.images if return_image and s.thumbnail])
        except GIAError as e:
            return _gen_result(_gen_error(e))

    @mcp.tool(annotations={"idempotentHint": True, "openWorldHint": False})
    async def set_animation(
        image: ImageRef,
        cols: Annotated[int | None, Field(description="Grid columns (1-8); required with rows when converting an image")] = None,
        rows: Annotated[int | None, Field(description="Grid rows (1-8); cols x rows between 2 and 36")] = None,
        mode: Annotated[str | None, Field(description="sprite or scene")] = None,
        key_color: Annotated[str | None, Field(description="green or magenta (sprite only)")] = None,
        tolerance: Annotated[int | None, Field(description="Key tolerance 5-80 (default 25)")] = None,
        fps: Annotated[int | None, Field(description="Frames per second 2-30 (default 10)")] = None,
        loop: Annotated[str | None, Field(description="loop or pingpong")] = None,
        redetect: Annotated[bool, Field(description="Detect the cells again")] = False,
    ) -> dict:
        """Turn an existing image into an animation (cols and rows required) or adjust its animation; no quota is spent."""
        try:
            ficha = await asyncio.to_thread(svc.set_animation, image, cols=cols, rows=rows, mode=mode,
                                            key_color=key_color, tolerance=tolerance, fps=fps, loop=loop,
                                            redetect=redetect)
            return {"status": "ok", "animation": ficha}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"openWorldHint": False})
    async def export_animation(
        image: ImageRef,
        copy_to: Annotated[str, Field(description="Directory (inside the allowed dirs) that receives <name>.png and <name>.json; existing files are never overwritten")],
    ) -> dict:
        """Export the animation as a normalized one-row PNG sheet plus a JSON descriptor."""
        try:
            target = check_copy_dest(copy_to, settings)
            png, meta, base = await asyncio.to_thread(svc.animation_export, image)
            png_out = await asyncio.to_thread(copy_file, png, target / f"{base}.png")
            meta["image"] = png_out.name
            with tempfile.TemporaryDirectory() as td:
                src = Path(td) / f"{base}.json"
                src.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                json_out = await asyncio.to_thread(copy_file, src, png_out.with_suffix(".json"))
            return {"status": "ok", "png": str(png_out), "json": str(json_out),
                    "animation": svc.get_animation(image)}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"openWorldHint": True})
    async def generate_composition(
        prompt: Annotated[str, Field(description="The whole scene, e.g. 'Hermes the robot in a street of shops filling a coin jar'")],
        pieces: Annotated[list[str], Field(min_length=1, max_length=6, description="1-6 unique element names (max 80 chars each) that get their own layer; the background is made automatically")],
        name: Annotated[str | None, Field(description="Composition name (max 80 chars); default: the first 40 chars of the prompt")] = None,
        refs: Annotated[list[str] | None, Field(description="Up to 3 reference images for the full scene: allowed paths, https URLs or gen:<id>[#index]")] = None,
        style: StyleArg = None, aspect_hint: Aspect = None, model: ModelArg = None) -> dict:
        """Generate a layered composition: full scene, background and one image per piece. Spends 2 + len(pieces) images; returns {status: ok|partial, composition, quota}."""
        try:
            out = await compositions.run_direct(prompt=prompt, pieces=pieces, name=name, refs=refs, style=style,
                                                ratio=aspect_hint, model=model)
        except GIAError as e:
            return error_payload(e)
        try:
            quota = await backend.quota()
        except Exception:
            quota = None
        return {"status": out["status"], "composition": public_view(out["composition"]), "quota": quota}

    @mcp.tool(annotations=RO)
    async def list_compositions() -> dict:
        """Compositions, newest first: id, name, progress {done,total,errors} and layers [{id, name, role, status, animation}]."""
        return {"status": "ok", "compositions": await asyncio.to_thread(compositions.summaries)}

    @mcp.tool(annotations=RO)
    async def get_composition(composition: Annotated[str, Field(description="Composition id (cmp_…) or exact name from list_compositions")]) -> dict:
        """One composition: master image, size and every layer with its status, file, bbox, offset and animation."""
        try:
            return {"status": "ok", "composition": public_view(await asyncio.to_thread(
                compositions.detail, compositions.resolve(composition)))}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"openWorldHint": False})
    async def export_composition(
        composition: Annotated[str, Field(description="Composition id (cmp_…) or exact name from list_compositions")],
        copy_to: Annotated[str, Field(description="Directory (inside the allowed dirs) that receives the files; existing files are never overwritten")],
        webp: Annotated[bool, Field(description="Also write the animated flattened scene-<name>.webp (takes a few seconds)")] = False,
    ) -> dict:
        """Write the composition zip (layers, composite.png, scene.json, musa-scene.js, index.html) into copy_to; webp=true also writes the animated WebP."""
        try:
            target = check_copy_dest(copy_to, settings)
            cid = compositions.resolve(composition)
            out = {"status": "ok"}
            jobs = [("zip", compositions.export_zip)] + ([("webp", compositions.export_webp)] if webp else [])
            for key, fn in jobs:
                path, filename = await asyncio.to_thread(fn, cid)
                try:
                    out[key] = str(await asyncio.to_thread(copy_file, path, target / filename))
                finally:
                    path.unlink(missing_ok=True)
            return out
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"idempotentHint": True, "openWorldHint": False})
    async def set_layer_animation(
        composition: Annotated[str, Field(description="Composition id (cmp_…) or exact name from list_compositions")],
        layer: Annotated[str, Field(description="Layer id (ly_…) or exact piece name from get_composition")],
        animation: Annotated[dict, Field(description="{type: static} removes any animation; {type: motion, preset: bob|sway|pulse|fall, amplitude?, period?, phase?, copies?} (copies only for fall; fall amplitude null = auto). No quota is spent.")],
    ) -> dict:
        """Set a piece's animation to static or motion (no quota). Frames and states are generated from Musa."""
        try:
            cid = compositions.resolve(composition)
            if not isinstance(animation, dict) or animation.get("type") not in ("static", "motion"):
                raise GIAError(INVALID_COMPOSITION, "solo se admite animation.type static o motion; frames y estados se crean en Musa")
            lid = compositions.resolve_layer(cid, layer)
            await asyncio.to_thread(lambda: compositions.patch_layer(cid, lid, animation=animation))
            return {"status": "ok", "composition": public_view(await asyncio.to_thread(compositions.detail, cid))}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True})
    async def retry_download(conversation_id: str, image_index: int) -> dict:
        """Retry ONLY the download of an already generated image (no regeneration, no quota)."""
        try:
            img = await backend.retry_download(conversation_id, image_index)
            return {"status": "ok", "code": None, "image": {
                "path": str(img.path), "width": img.width, "height": img.height,
                "bytes": img.nbytes, "format": img.format, "full_size": img.full_size}}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "openWorldHint": True})
    async def delete_conversation(conversation_id: str) -> dict:
        """Delete the conversation on Gemini and mark it in the local history."""
        try:
            await backend.delete_conversation(conversation_id)
            return {"status": "ok", "code": None, "conversation_id": conversation_id}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"openWorldHint": True})
    async def create_style(
        name: Annotated[str, Field(description="Display name; the style id is derived from it")],
        instructions: Annotated[str, Field(description="Text appended to prompts that use this style")],
        description: Annotated[str, Field(description="Short description shown in list_styles")] = "",
        family: Annotated[str, Field(description="Group label, e.g. otros")] = "otros",
        image: StyleImage = None,
    ) -> dict:
        """Create your own style (optionally with a reference image sent to Gemini as the first ref)."""
        try:
            data = await resolve_style_image(svc, image) if image else None
            style = svc.styles.create(name=name, description=description, append=instructions, family=family,
                                      image=data, taken_ids=set(svc.catalog().styles))
            return {"status": "ok", "style": style_dict(style)}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True})
    async def update_style(
        style: StyleId,
        name: Annotated[str | None, Field(description="New display name (the id never changes)")] = None,
        instructions: Annotated[str | None, Field(description="New text appended to prompts")] = None,
        description: Annotated[str | None, Field(description="New short description")] = None,
        family: Annotated[str | None, Field(description="New group label")] = None,
        image: StyleImage = None,
        remove_image: Annotated[bool, Field(description="Drop the reference image")] = False,
    ) -> dict:
        """Edit one of your own styles; omitted fields are kept. Built-in styles are rejected (invalid_style). Syncs its Gem if enabled."""
        try:
            fields: dict = {k: v for k, v in (("name", name), ("append", instructions),
                                              ("description", description), ("family", family)) if v is not None}
            require_user_style(svc, style, "edited")  # before any download
            if image:
                fields["image"] = await resolve_style_image(svc, image)
            if remove_image:
                fields["remove_image"] = True
            return {"status": "ok", "style": style_dict(await svc.update_user_style(style, **fields))}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "openWorldHint": True})
    async def delete_style(style: StyleId) -> dict:
        """Delete one of your own styles and its Gem, if any. Built-in styles are rejected (invalid_style)."""
        try:
            require_user_style(svc, style, "deleted")
            await svc.delete_user_style(style)
            return {"status": "ok", "deleted": style}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations=RO)
    async def describe_style(
        style: StyleId,
        prompt: Annotated[str | None, Field(description="Sample prompt to preview; default a placeholder")] = None,
    ) -> dict:
        """A style's details plus the exact prompt that would be sent, without calling Gemini."""
        try:
            return {"status": "ok", **svc.describe_style(style, prompt)}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"idempotentHint": True, "openWorldHint": False})
    async def rename_collection(
        collection: Annotated[str, Field(description="Existing collection name or id")],
        name: Annotated[str, Field(description="New name")],
        description: Annotated[str | None, Field(description="New description; omit to keep it")] = None,
    ) -> dict:
        """Rename a collection (and optionally change its description)."""
        try:
            return {"status": "ok", **svc.rename_collection(collection, name, description)}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
    async def delete_collection(
        collection: Annotated[str, Field(description="Existing collection name or id")],
    ) -> dict:
        """Delete a collection; its images are kept."""
        try:
            return {"status": "ok", **svc.delete_collection(collection)}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"idempotentHint": True, "openWorldHint": False})
    async def remove_from_collection(
        image: ImageRef,
        collection: Annotated[str, Field(description="Existing collection name or id")],
    ) -> dict:
        """Remove an image from a collection (the image itself is kept)."""
        try:
            return {"status": "ok", **svc.remove_from_collection(image, collection)}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"idempotentHint": True, "openWorldHint": False})
    async def favorite_image(
        image: ImageRef,
        favorite: Annotated[bool, Field(description="true: mark as favorite; false: unmark")] = True,
    ) -> dict:
        """Mark or unmark an image as favorite."""
        try:
            return {"status": "ok", **svc.set_favorite(image, favorite)}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations=RO)
    async def get_image(image: ImageRef) -> dict:
        """One image's path, prompt, style, model, size, favorite flag, collections and conversation_id."""
        try:
            return {"status": "ok", **svc.get_image(image)}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
    async def delete_image(image: ImageRef) -> dict:
        """Delete the local image file and thumbnail and hide it from history; Gemini's copy is untouched."""
        try:
            return {"status": "ok", **svc.delete_image(image)}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"openWorldHint": True})
    async def generate_batch(
        prompts: Annotated[list[str], Field(min_length=1, max_length=8, description="1-8 prompts, each generated in its own fresh chat, one after another")],
        style: StyleArg = None, collection: CollectionArg = None, aspect_hint: Aspect = None,
        model: ModelArg = None, project: Project = None, subdir: Subdir = None,
    ) -> dict:
        """Generate several images with shared options. Returns {status ok|partial|stopped, results, stopped_at}; stops on quota/auth errors."""
        try:
            return await svc.generate_batch(prompts, style=style, collection=collection,
                                            aspect_hint=aspect_hint, model=model, project=project, subdir=subdir)
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations=RO)
    async def list_prompts() -> dict:
        """Saved prompts, most used first: [{id, name, text, created, uses}]."""
        return {"status": "ok", "prompts": svc.list_prompts()}

    @mcp.tool(annotations={"openWorldHint": False})
    async def save_prompt(
        name: Annotated[str, Field(description="Short unique name (case-insensitive, max 80 chars)")],
        text: Annotated[str, Field(description="Prompt text (max 4000 chars)")],
    ) -> dict:
        """Save a reusable prompt. Not idempotent: a duplicate name fails with invalid_prompt (use update_prompt to change one)."""
        try:
            return {"status": "ok", "prompt": svc.save_prompt(name, text)}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
    async def update_prompt(
        prompt: Annotated[str, Field(description="Saved prompt id (pr_xxxxxxxx) or name, from list_prompts")],
        name: Annotated[str | None, Field(description="New name; omit to keep it")] = None,
        text: Annotated[str | None, Field(description="New text; omit to keep it")] = None,
    ) -> dict:
        """Rename and/or change the text of a saved prompt (omitted fields are kept)."""
        try:
            return {"status": "ok", "prompt": svc.update_prompt(prompt, name=name, text=text)}
        except GIAError as e:
            return error_payload(e)

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
    async def delete_prompt(
        prompt: Annotated[str, Field(description="Saved prompt id (pr_xxxxxxxx) or name, from list_prompts")],
    ) -> dict:
        """Delete a saved prompt."""
        try:
            return {"status": "ok", **svc.delete_prompt(prompt)}
        except GIAError as e:
            return error_payload(e)

    return mcp


def main() -> None:
    settings = load_settings()
    setup_logging(settings.state_dir)
    backend = build_backend(settings, auto_close=True)
    mcp = create_server(backend, settings)
    if settings.app_with_mcp:
        asyncio.run(_run_with_app(settings, backend, mcp))
    else:
        mcp.run(show_banner=False)  # stdio


async def _run_with_app(settings: Settings, backend: Backend, mcp: FastMCP) -> None:
    """MCP over stdio plus Musa in the same process; stdout stays pure JSON-RPC."""
    log = logging.getLogger(__name__)
    svc = ImageService(backend, settings, StateStore(settings.state_dir),
                       LibraryStore(settings.state_dir), UserStyleStore(settings.state_dir))
    info = running_app(settings)
    if info is not None or not port_free(settings.app_port):
        if info is not None:
            log.info(f"Musa already running at {info['url']}; not starting another one")
        else:
            log.warning(f"port {settings.app_port} is busy; MCP continues without Musa")
        await mcp.run_async(transport="stdio", show_banner=False)
        return
    async with anyio.create_task_group() as tg:
        async def _serve() -> None:
            try:
                await serve_app(settings, svc, svc.library, svc.styles,
                                port=settings.app_port, open_browser=False)
            except Exception:
                log.warning("Musa app failed to start; MCP continues without it", exc_info=True)
        tg.start_soon(_serve)
        await mcp.run_async(transport="stdio", show_banner=False)  # returns -> task group cancels the app


if __name__ == "__main__":
    main()
