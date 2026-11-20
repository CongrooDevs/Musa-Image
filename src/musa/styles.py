from importlib import resources
import logging

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from .errors import GIAError, INVALID_ASPECT, UNKNOWN_STYLE, TEMPLATE_MISSING_VAR

log = logging.getLogger(__name__)

PACKAGE_ROOT = Path(str(resources.files("musa")))

ASPECT_HINTS = {
    "1:1": "square 1:1 aspect ratio",
    "4:5": "4:5 portrait aspect ratio",
    "3:4": "3:4 portrait aspect ratio",
    "2:3": "2:3 portrait aspect ratio",
    "9:16": "tall portrait 9:16 aspect ratio",
    "5:4": "5:4 aspect ratio",
    "4:3": "4:3 aspect ratio",
    "3:2": "3:2 landscape aspect ratio",
    "16:9": "wide landscape 16:9 aspect ratio",
    "21:9": "ultra-wide panoramic 21:9 aspect ratio",
}
AspectHint = Literal["1:1", "4:5", "3:4", "2:3", "9:16", "5:4", "4:3", "3:2", "16:9", "21:9"]
GENERATE_PREFIX = "Generate an image (do not search the web): "


def resolve_styles_path(settings) -> Path:
    if settings.styles_file:
        return Path(settings.styles_file).expanduser()
    user = Path.home() / ".config" / "gemini-image-api" / "styles.yaml"
    if user.exists():
        return user
    return Path(str(resources.files("musa") / "styles.yaml"))


@dataclass(frozen=True)
class Style:
    id: str
    name: str
    family: str
    description: str
    append: str
    ref: Path | None
    credit: str
    source: str


@dataclass(frozen=True)
class Template:
    name: str
    description: str
    prompt: str
    vars: list[str]


@dataclass
class StyleCatalog:
    styles: dict[str, Style] = field(default_factory=dict)
    templates: dict[str, Template] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path, *, ref_base: Path | None = None, source: str = "builtin") -> "StyleCatalog":
        path = Path(path)
        base = Path(ref_base) if ref_base is not None else path.parent
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        styles = {}
        base_resolved = base.resolve()
        for sid, s in (data.get("styles") or {}).items():
            ref = None
            if s.get("ref"):
                candidate = (base / s["ref"]).resolve()
                if candidate.is_relative_to(base_resolved):
                    ref = candidate
                else:
                    log.warning("style %r ref %r escapes ref_base %s; ignoring it", sid, s["ref"], base_resolved)
            styles[sid] = Style(id=sid, name=s.get("name", sid), family=s.get("family", "otros"),
                                description=s.get("description", ""),
                                append=s.get("append") or s.get("description", ""),
                                ref=ref, credit=s.get("credit", ""), source=source)
        templates = {n: Template(n, t.get("description", ""), t.get("prompt", ""), list(t.get("vars") or []))
                     for n, t in (data.get("templates") or {}).items()}
        return cls(styles=styles, templates=templates)

    def merge(self, other: "StyleCatalog") -> "StyleCatalog":
        """Merge other into self; self wins on id clashes for styles and templates."""
        return StyleCatalog(styles={**other.styles, **self.styles},
                            templates={**other.templates, **self.templates})


def load_catalog(settings) -> StyleCatalog:
    """Built-in (or configured) styles file plus the user's own styles, if present."""
    base_path = resolve_styles_path(settings)
    packaged = Path(str(resources.files("musa") / "styles.yaml"))
    ref_base = PACKAGE_ROOT if base_path == packaged else base_path.parent
    catalog = StyleCatalog.load(base_path, ref_base=ref_base, source="builtin")
    user = Path(settings.state_dir) / "styles" / "styles.yaml"
    if user.exists():
        catalog = catalog.merge(StyleCatalog.load(user, source="user"))
    return catalog


def build_prompt(catalog: StyleCatalog, *, prompt: str, style: str | None = None,
                 style_text: str | None = None, template: str | None = None,
                 template_vars: dict | None = None, aspect_hint: str | None = None) -> str:
    parts: list[str] = []
    if template:
        t = catalog.templates.get(template)
        if t is None:
            raise GIAError(UNKNOWN_STYLE, f"template {template!r} does not exist", valid=sorted(catalog.templates))
        missing = [v for v in t.vars if not (template_vars or {}).get(v)]
        if missing:
            raise GIAError(TEMPLATE_MISSING_VAR, f"missing vars {missing}", missing=missing, required=t.vars)
        try:
            prompt = t.prompt.format(**{k: str(v) for k, v in (template_vars or {}).items()})
        except (KeyError, IndexError, ValueError, AttributeError, TypeError) as e:
            used = [str(e.args[0])] if isinstance(e, KeyError) and e.args else []
            raise GIAError(TEMPLATE_MISSING_VAR,
                           f"template {template!r} cannot be formatted ({type(e).__name__}: {e}); "
                           "every {placeholder} must be a name listed in vars",
                           missing=used, required=t.vars) from e
    parts.append(prompt.strip())
    if style:
        s = catalog.styles.get(style)
        if s is None:
            raise GIAError(UNKNOWN_STYLE, f"style {style!r} does not exist", valid=sorted(catalog.styles))
        parts.append(s.append)
    if style_text:
        parts.append(style_text.strip())
    if aspect_hint:
        if aspect_hint not in ASPECT_HINTS:
            raise GIAError(INVALID_ASPECT, f"invalid aspect_hint {aspect_hint!r}", valid=sorted(ASPECT_HINTS))
        parts.append(ASPECT_HINTS[aspect_hint])
    return GENERATE_PREFIX + " ".join(p for p in parts if p)


def preview_prompt(catalog: StyleCatalog, *, prompt: str, style: str | None = None,
                   style_text: str | None = None, template: str | None = None,
                   template_vars: dict | None = None, aspect_hint: str | None = None) -> dict:
    """Offline preview of what generate() would send: final prompt plus the style's reference image."""
    final = build_prompt(catalog, prompt=prompt, style=style, style_text=style_text, template=template,
                         template_vars=template_vars, aspect_hint=aspect_hint)
    s = catalog.styles.get(style) if style else None
    return {"final_prompt": final, "style_ref": str(s.ref) if s and s.ref else None,
            "aspect_hint": aspect_hint}
