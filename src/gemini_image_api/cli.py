import asyncio
import json
import shutil
import tomllib
import webbrowser
from pathlib import Path

import typer

from .applauncher import port_free, running_app, serve_app
from .client import Backend, build_backend
from .config import Settings, load_settings
from .errors import INVALID_STYLE, GIAError, error_payload
from .logsetup import setup_logging
from .service import ImageService, to_payload
from .library import LibraryStore
from .state import StateStore
from .style_store import UserStyleStore

app = typer.Typer(help="gia: generate/edit images with your Gemini web session (no API key)")
_backend: Backend | None = None
_owned = False  # True when _backend was built lazily by _bs() (as opposed to injected via set_backend)
_settings: Settings | None = None
_service: ImageService | None = None


def set_backend(backend: Backend | None, settings: Settings | None) -> None:
    global _backend, _settings, _service, _owned
    _backend, _settings, _owned = backend, settings, False
    _service = (ImageService(backend, settings, StateStore(settings.state_dir),
                             LibraryStore(settings.state_dir), UserStyleStore(settings.state_dir))
                if backend is not None and settings is not None else None)


def _bs() -> tuple[Backend, Settings]:
    global _backend, _settings, _service, _owned
    if _service is None or _backend is None:  # first use, or the backend was closed by _run
        _settings = _settings or load_settings()
        if _backend is None:
            _backend, _owned = build_backend(_settings), True
        _service = ImageService(_backend, _settings, StateStore(_settings.state_dir),
                                LibraryStore(_settings.state_dir), UserStyleStore(_settings.state_dir))
    return _backend, _settings


def _run(coro):
    async def wrapped():
        global _backend
        try:
            return await coro
        finally:
            if _backend is not None:
                await _backend.aclose()
                if _owned:  # built by _bs(): forget it; the next _bs() recreates it on demand.
                    _backend = None  # (_service stays valid for the rest of the current command)
    return asyncio.run(wrapped())


def _dump(obj) -> None:
    typer.echo(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _vars(var: list[str]) -> dict | None:
    tv = {}
    for kv in var:
        if "=" not in kv:
            raise typer.BadParameter(f"expected k=v, got {kv!r}", param_hint="--var")
        k, v = kv.split("=", 1)
        tv[k] = v
    return tv or None


def _fail(e: GIAError) -> None:
    _dump(error_payload(e))
    raise typer.Exit(1)


def _finish(sr) -> None:
    p = to_payload(sr)
    _dump(p)
    if p["status"] == "error":
        raise typer.Exit(1)


@app.command()
def gen(prompt: str,
        refs: list[str] = typer.Option([], "--ref", "-r", help="Local path, http(s) URL, or gen:<id>[#index] (repeatable)"),
        style: str | None = typer.Option(None, "--style"),
        style_text: str | None = typer.Option(None, "--style-text"),
        template: str | None = typer.Option(None, "--template"),
        var: list[str] = typer.Option([], "--var", "-v", help="Template variable k=v (repeatable)"),
        aspect: str | None = typer.Option(None, "--aspect", help="Aspect ratio hint: 1:1, 4:5, 3:4, 2:3, 9:16, 5:4, 4:3, 3:2, 16:9, 21:9"),
        project: str | None = typer.Option(None, "--project"),
        subdir: str | None = typer.Option(None, "--subdir"),
        copy_to: str | None = typer.Option(None, "--copy-to", help="Also copy the result here"),
        model: str | None = typer.Option(None, "--model"),
        collection: str | None = typer.Option(None, "--collection", help="File the result under this collection (created if missing)")):
    """Generate a new image (fresh chat)."""
    _bs()
    try:
        sr = _run(_service.generate(prompt=prompt, refs=refs, style=style, style_text=style_text,
                                    template=template, template_vars=_vars(var), aspect_hint=aspect,
                                    model=model, project=project, subdir=subdir, copy_to=copy_to,
                                    collection=collection))
    except GIAError as e:
        _fail(e)
    _finish(sr)


@app.command()
def edit(conversation_id: str, prompt: str,
         refs: list[str] = typer.Option([], "--ref", "-r", help="Local path, http(s) URL, or gen:<id>[#index] (repeatable)"),
         style: str | None = typer.Option(None, "--style"),
         style_text: str | None = typer.Option(None, "--style-text"),
         template: str | None = typer.Option(None, "--template"),
         var: list[str] = typer.Option([], "--var", "-v", help="Template variable k=v (repeatable)"),
         aspect: str | None = typer.Option(None, "--aspect", help="Aspect ratio hint: 1:1, 4:5, 3:4, 2:3, 9:16, 5:4, 4:3, 3:2, 16:9, 21:9"),
         project: str | None = typer.Option(None, "--project"),
         subdir: str | None = typer.Option(None, "--subdir"),
         copy_to: str | None = typer.Option(None, "--copy-to", help="Also copy the result here"),
         model: str | None = typer.Option(None, "--model"),
         collection: str | None = typer.Option(None, "--collection", help="File the result under this collection (created if missing)")):
    """Edit the image of an existing conversation."""
    _bs()
    try:
        sr = _run(_service.edit(conversation_id, prompt=prompt, refs=refs, style=style, style_text=style_text,
                                template=template, template_vars=_vars(var), aspect_hint=aspect,
                                model=model, project=project, subdir=subdir, copy_to=copy_to,
                                collection=collection))
    except GIAError as e:
        _fail(e)
    _finish(sr)


@app.command()
def auth(status: bool = typer.Option(False, "--status", help="Show session status instead of logging in")):
    """Guided login (Firefox) or session check."""
    b, s = _bs()
    if status:
        try:
            _dump(_run(b.status()))
        except GIAError as e:
            _fail(e)
        return
    from .auth import login_guided
    try:
        jar = login_guided(s)
    except GIAError as e:
        _fail(e)
    typer.echo(f"OK: {len(jar)} cookies saved to {s.cookie_file}")


styles_app = typer.Typer(help="List styles/templates; add|edit|rm|show your own styles; manage style Gems", invoke_without_command=True)
app.add_typer(styles_app, name="styles")


@styles_app.callback()
def styles_list(ctx: typer.Context):
    """List styles and templates."""
    if ctx.invoked_subcommand is not None:
        return
    _bs()
    try:
        c = _service.catalog()
    except (OSError, ValueError) as e:
        typer.echo(f"cannot load styles: {e}", err=True)
        raise typer.Exit(1)
    _dump({"styles": {n: x.description for n, x in c.styles.items()},
           "templates": {n: {"vars": t.vars, "description": t.description} for n, t in c.templates.items()}})


@styles_app.command("gems")
def styles_gems():
    """List style Gems (persistent Gemini instructions per style)."""
    _bs()
    _dump(_service.list_style_gems())


@styles_app.command("gem")
def styles_gem(style: str, action: str,
               instructions: str | None = typer.Option(None, "--instructions",
                   help="Custom Gem instructions; omit for the default template (empty string restores it)")):
    """Enable (on) or disable (off) the Gem of a style."""
    if action not in ("on", "off"):
        raise typer.BadParameter(f"expected on|off, got {action!r}")
    _bs()
    try:
        out = _run(_service.set_style_gem(style, action == "on", instructions=instructions))
    except GIAError as e:
        _fail(e)
    _dump(out)


@styles_app.command("show")
def styles_show(style: str, prompt: str | None = typer.Option(None, "--prompt", help="Sample prompt to preview")):
    """Show a style and the exact prompt it would send (offline)."""
    _bs()
    try:
        _dump(_service.describe_style(style, prompt))
    except GIAError as e:
        _fail(e)


@styles_app.command("add")
def styles_add(name: str,
               instructions: str = typer.Option(..., "--instructions", help="Text appended to prompts that use the style"),
               description: str = typer.Option("", "--description"),
               family: str = typer.Option("otros", "--family"),
               image: str | None = typer.Option(None, "--image", help="Reference image: allowed path, https URL or gen:<id>\\[#index]")):
    """Create your own style."""
    from .mcp_server import resolve_style_image, style_dict
    _bs()
    try:
        data = _run(resolve_style_image(_service, image)) if image else None
        style = _service.styles.create(name=name, description=description, append=instructions, family=family,
                                       image=data, taken_ids=set(_service.catalog().styles))
    except GIAError as e:
        _fail(e)
    _dump(style_dict(style))


@styles_app.command("edit")
def styles_edit(style: str,
                name: str | None = typer.Option(None, "--name"),
                instructions: str | None = typer.Option(None, "--instructions"),
                description: str | None = typer.Option(None, "--description"),
                family: str | None = typer.Option(None, "--family"),
                image: str | None = typer.Option(None, "--image", help="New reference image: allowed path, https URL or gen:<id>\\[#index]"),
                remove_image: bool = typer.Option(False, "--remove-image")):
    """Edit one of your own styles (omitted fields are kept)."""
    from .mcp_server import require_user_style, resolve_style_image, style_dict
    _bs()

    async def go():
        require_user_style(_service, style, "edited")  # before any download
        fields: dict = {k: v for k, v in (("name", name), ("append", instructions),
                                          ("description", description), ("family", family)) if v is not None}
        if image:
            fields["image"] = await resolve_style_image(_service, image)
        if remove_image:
            fields["remove_image"] = True
        return await _service.update_user_style(style, **fields)
    try:
        updated = _run(go())
    except GIAError as e:
        _fail(e)
    _dump(style_dict(updated))


@styles_app.command("rm")
def styles_rm(style: str):
    """Delete one of your own styles (and its Gem)."""
    from .mcp_server import require_user_style
    _bs()

    async def go():
        require_user_style(_service, style, "deleted")
        await _service.delete_user_style(style)
    try:
        _run(go())
    except GIAError as e:
        _fail(e)
    _dump({"deleted": style})


@app.command()
def history(query: str | None = typer.Option(None, "--query"),
            project: str | None = typer.Option(None, "--project"),
            since: str | None = typer.Option(None, "--since"),
            status: str | None = typer.Option(None, "--status"),
            collection: str | None = typer.Option(None, "--collection", help="Filter: only images in this collection (name or id; must already exist)"),
            model: str | None = typer.Option(None, "--model", help="Filter: only generations made with this exact model id"),
            limit: int = typer.Option(20, "--limit"),
            offset: int = typer.Option(0, "--offset")):
    """List past generations (newest first); ids work as refs: gen:<id>[#index]."""
    _bs()
    try:
        _dump(_service.list_generations(query=query, project=project, since=since, status=status,
                                        collection=collection, model=model, limit=limit, offset=offset))
    except GIAError as e:
        _fail(e)

collections_app = typer.Typer(help="List collections; add|rename|rm|remove collections and their images", invoke_without_command=True)
app.add_typer(collections_app, name="collections")


@collections_app.callback()
def collections_list(ctx: typer.Context):
    """List collections (tags) with member counts."""
    if ctx.invoked_subcommand is None:
        _bs()
        _dump(_service.list_collections())


@collections_app.command("add")
def collections_add(image: str, collection: str):
    """Add an image (gen:<id>\\[#index]) to a collection (created if missing)."""
    _bs()
    try:
        _dump(_service.add_to_collection(image, collection))
    except GIAError as e:
        _fail(e)


@collections_app.command("rename")
def collections_rename(collection: str, name: str,
                       description: str | None = typer.Option(None, "--description")):
    """Rename a collection (name or id)."""
    _bs()
    try:
        _dump(_service.rename_collection(collection, name, description))
    except GIAError as e:
        _fail(e)


@collections_app.command("rm")
def collections_rm(collection: str):
    """Delete a collection; its images are kept."""
    _bs()
    try:
        _dump(_service.delete_collection(collection))
    except GIAError as e:
        _fail(e)


@collections_app.command("remove")
def collections_remove(image: str, collection: str):
    """Remove an image (gen:<id>\\[#index]) from a collection."""
    _bs()
    try:
        _dump(_service.remove_from_collection(image, collection))
    except GIAError as e:
        _fail(e)


prompts_app = typer.Typer(help="List saved prompts; add|edit|rm|use reusable prompt texts", invoke_without_command=True)
app.add_typer(prompts_app, name="prompts")


@prompts_app.callback()
def prompts_list(ctx: typer.Context):
    """List saved prompts (most used first)."""
    if ctx.invoked_subcommand is None:
        _bs()
        _dump(_service.list_prompts())


@prompts_app.command("add")
def prompts_add(name: str, text: str):
    """Save a prompt under a unique name (case-insensitive).

    A text starting with '-' needs '--' first: gia prompts add -- name "-v text"."""
    _bs()
    try:
        _dump(_service.save_prompt(name, text))
    except GIAError as e:
        _fail(e)


@prompts_app.command("edit")
def prompts_edit(prompt: str = typer.Argument(..., help="Prompt id (pr_xxxxxxxx) or name"),
                 name: str | None = typer.Option(None, "--name", help="New name"),
                 text: str | None = typer.Option(None, "--text", help="New text")):
    """Rename and/or change the text of a saved prompt (omitted fields are kept).

    A --text starting with '-' works as --text=-v text; positional ids never start with '-'."""
    _bs()
    try:
        _dump(_service.update_prompt(prompt, name=name, text=text))
    except GIAError as e:
        _fail(e)


@prompts_app.command("rm")
def prompts_rm(prompt: str = typer.Argument(..., help="Prompt id (pr_xxxxxxxx) or name")):
    """Delete a saved prompt."""
    _bs()
    try:
        _dump(_service.delete_prompt(prompt))
    except GIAError as e:
        _fail(e)


@prompts_app.command("use")
def prompts_use(prompt: str = typer.Argument(..., help="Prompt id (pr_xxxxxxxx) or name")):
    """Print a saved prompt's text (counts as one use), e.g. p=$(gia prompts use cat) && gia gen "$p".

    Errors go to stderr as JSON (exit 1), so stdout never carries an error."""
    _bs()
    try:
        typer.echo(_service.use_prompt(prompt)["text"])
    except GIAError as e:
        typer.echo(json.dumps(error_payload(e), ensure_ascii=False, indent=2, default=str), err=True)
        raise typer.Exit(1)


@app.command()
def fav(image: str, off: bool = typer.Option(False, "--off", help="Unmark instead of marking")):
    """Mark an image (gen:<id>\\[#index]) as favorite."""
    _bs()
    try:
        _dump(_service.set_favorite(image, not off))
    except GIAError as e:
        _fail(e)


@app.command()
def rm(image: str):
    """Delete the local file and thumbnail of an image (gen:<id>\\[#index]); Gemini's copy is untouched."""
    _bs()
    try:
        _dump(_service.delete_image(image))
    except GIAError as e:
        _fail(e)


@app.command()
def models():
    """Chat models available on your account."""
    b, _ = _bs()
    try:
        _dump(_run(b.list_models()))
    except GIAError as e:
        _fail(e)


@app.command(name="retry-download")
def retry_download(conversation_id: str, image_index: int):
    """Retry only the download of an already generated image."""
    b, _ = _bs()
    try:
        img = _run(b.retry_download(conversation_id, image_index))
    except GIAError as e:
        _fail(e)
    _dump({"path": str(img.path), "full_size": img.full_size})


@app.command()
def delete(conversation_id: str):
    """Delete the conversation on Gemini and in the local history."""
    b, _ = _bs()
    try:
        _run(b.delete_conversation(conversation_id))
    except GIAError as e:
        _fail(e)
    typer.echo(f"deleted {conversation_id}")


def mcp_config_dict(repo_root: Path | None) -> dict:
    if repo_root is not None:
        server = {"command": shutil.which("uv") or "uv",
                  "args": ["run", "--project", str(repo_root), "gia-mcp"]}
    else:
        server = {"command": shutil.which("gia-mcp") or "gia-mcp"}
    return {"mcpServers": {"gemini-image-api": server}}


def _checkout_root(module_file: str | Path = __file__) -> Path | None:
    """Repo root when running from a source checkout (<root>/src/gemini_image_api/cli.py), else None.

    Path components and the parsed [project].name are compared exactly, never substrings.
    """
    pkg = Path(module_file).resolve().parent
    if pkg.parent.name != "src":
        return None
    root = pkg.parent.parent
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    proj = data.get("project")
    return root if isinstance(proj, dict) and proj.get("name") == "gemini-image-api" else None


@app.command(name="mcp-config")
def mcp_config():
    """Print the JSON to register this MCP server in your agent (omp, Claude Code, Claude Desktop, Cursor)."""
    typer.echo(json.dumps(mcp_config_dict(_checkout_root()), indent=2))


@app.command()
def serve():
    """Run the MCP server over stdio."""
    from .mcp_server import main as serve_main
    serve_main()


@app.command(name="app")
def app_cmd(port: int | None = typer.Option(None, "--port"),
            no_open: bool = typer.Option(False, "--no-open")):
    """Open Musa, the local gallery (styles, collections)."""
    _, s = _bs()
    port = port or s.app_port
    info = running_app(s)
    if info:
        typer.echo(f"Musa is already running: {info['url']}", err=True)
        if not no_open:
            webbrowser.open(info["url"])
        return
    if not port_free(port):
        typer.echo(json.dumps({"status": "error", "code": "port_busy",
                                "message": f"port {port} is busy", "next_action": "use --port"},
                               ensure_ascii=False))
        raise typer.Exit(1)
    asyncio.run(serve_app(s, _service, _service.library, _service.styles,
                          port=port, open_browser=not no_open))

@app.command()
def update(check: bool = typer.Option(False, "--check", help="Solo comprobar si hay actualizaciones disponibles")):
    """Actualiza gia y Musa a la última versión desde GitHub."""
    from .updates import check_update, run_update
    _, s = _bs()
    if check:
        res = check_update(s.state_dir, force=True)
        if res.get("has_update"):
            typer.echo(f"💡 Nueva versión disponible: {res.get('message')} ({res.get('latest_sha', '')[:8]})")
            typer.echo("Ejecuta 'gia update' para instalarla.")
        else:
            typer.echo("✅ gia está actualizado a la última versión.")
        return

    typer.echo("Actualizando gia desde GitHub...")
    try:
        code = run_update()
        if code == 0:
            typer.echo("✅ gia se ha actualizado correctamente.")
        else:
            typer.echo("❌ Falló la actualización de gia.", err=True)
            raise typer.Exit(code)
    except Exception as e:
        typer.echo(f"❌ Error al actualizar: {e}", err=True)
        raise typer.Exit(1)




def main() -> None:
    setup_logging(load_settings().state_dir)
    app()


if __name__ == "__main__":
    main()
