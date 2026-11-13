import pytest, yaml
from pathlib import Path
from gemini_image_api.styles import StyleCatalog, build_prompt, ASPECT_HINTS, resolve_styles_path, load_catalog, PACKAGE_ROOT
from gemini_image_api.errors import GIAError

def test_styles_path_precedence(settings, monkeypatch, tmp_path):
    home = tmp_path / "home"; monkeypatch.setenv("HOME", str(home))
    settings.styles_file = None
    packaged = resolve_styles_path(settings)
    assert packaged.name == "styles.yaml" and "gemini_image_api" in str(packaged) and packaged.exists()
    user = home / ".config/gemini-image-api/styles.yaml"; user.parent.mkdir(parents=True); user.write_text("styles: {}")
    assert resolve_styles_path(settings) == user
    explicit = tmp_path / "x.yaml"; settings.styles_file = explicit
    assert resolve_styles_path(settings) == explicit

def test_packaged_catalog_has_examples(settings):
    settings.styles_file = None
    cat = StyleCatalog.load(resolve_styles_path(settings))
    assert cat.styles == {}  # sin estilos integrados; los estilos son siempre del usuario

@pytest.fixture
def catalog(tmp_path: Path) -> StyleCatalog:
    p = tmp_path / "styles.yaml"
    p.write_text(yaml.safe_dump({
        "styles": {"anime": {"description": "d", "append": "anime style, cel shading"}},
        "templates": {"anuncio": {"description": "d", "prompt": "foto de {producto}", "vars": ["producto"]}},
    }))
    return StyleCatalog.load(p)

def test_build_prompt_con_style(catalog):
    out = build_prompt(catalog, prompt="un gato", style="anime")
    assert "un gato" in out and "anime style, cel shading" in out

def test_build_prompt_style_desconocido_lista_validos(catalog):
    with pytest.raises(GIAError) as e:
        build_prompt(catalog, prompt="x", style="nope")
    assert e.value.code == "unknown_style" and e.value.details["valid"] == ["anime"]

def test_build_prompt_template_y_vars(catalog):
    out = build_prompt(catalog, prompt="", template="anuncio", template_vars={"producto": "zapato"})
    assert "foto de zapato" in out

def test_build_prompt_template_var_faltante(catalog):
    with pytest.raises(GIAError) as e:
        build_prompt(catalog, prompt="", template="anuncio", template_vars={})
    assert e.value.code == "template_missing_var" and "producto" in e.value.details["missing"]

@pytest.mark.parametrize("ratio", sorted(ASPECT_HINTS))
def test_aspect_hint_accepts_all_ten(catalog, ratio):
    assert ASPECT_HINTS[ratio] in build_prompt(catalog, prompt="x", aspect_hint=ratio)
    assert len(ASPECT_HINTS) == 10 and set(ASPECT_HINTS) == {"1:1", "4:5", "3:4", "2:3", "9:16", "5:4", "4:3", "3:2", "16:9", "21:9"}



@pytest.mark.parametrize("tpl,exc_missing", [("foto de {x}", ["x"]), ("foto {} raro", []), ("llave { suelta", [])])
def test_template_bad_placeholder_is_gia_error(tmp_path, tpl, exc_missing):
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump({"templates": {"t": {"description": "d", "prompt": tpl, "vars": ["producto"]}}}))
    cat = StyleCatalog.load(p)
    with pytest.raises(GIAError) as e:
        build_prompt(cat, prompt="", template="t", template_vars={"producto": "zapato"})
    assert e.value.code == "template_missing_var"
    assert e.value.details["missing"] == exc_missing and e.value.details["required"] == ["producto"]

def test_prompt_has_single_space_after_prefix(catalog):
    from gemini_image_api.styles import GENERATE_PREFIX
    assert build_prompt(catalog, prompt="un gato") == GENERATE_PREFIX + "un gato"
    assert "  " not in build_prompt(catalog, prompt="un gato")

def test_invalid_aspect_has_own_code(catalog):
    with pytest.raises(GIAError) as e:
        build_prompt(catalog, prompt="x", aspect_hint="7:5")
    assert e.value.code == "invalid_aspect"
    assert e.value.details["valid"] == ["16:9", "1:1", "21:9", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16"]

def test_template_subscript_placeholder_is_template_missing_var(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump({"templates": {"t": {"prompt": "{x[y]}", "vars": ["x"]}}}))
    with pytest.raises(GIAError) as e:
        build_prompt(StyleCatalog.load(p), prompt="", template="t", template_vars={"x": "abc"})
    assert e.value.code == "template_missing_var"

def test_packaged_templates(settings):
    settings.styles_file = None
    cat = load_catalog(settings)
    t = cat.templates
    assert {"anuncio_producto", "post_redes", "banner_web", "ficha_producto"} <= set(t)
    assert set(t["post_redes"].vars) == {"tema", "estilo"}
    assert "1:1" in t["post_redes"].prompt and "16:9" in t["banner_web"].prompt

def test_legacy_yaml_still_loads(settings, tmp_path):
    p = tmp_path / "s.yaml"; p.write_text("styles:\n  x:\n    description: d\n    append: a\n")
    settings.styles_file = p
    s = load_catalog(settings).styles["x"]
    assert (s.id, s.name, s.family, s.append, s.ref) == ("x", "x", "otros", "a", None)

def test_user_styles_are_merged(settings):
    from gemini_image_api.style_store import UserStyleStore
    settings.styles_file = None
    UserStyleStore(settings.state_dir).create(name="Mi acuarela", description="suave", append="soft watercolor")
    cat = load_catalog(settings)
    assert cat.styles["mi-acuarela"].source == "user" and cat.styles["mi-acuarela"].append == "soft watercolor"

def test_escaping_ref_is_dropped_with_warning(settings, caplog):
    settings.styles_file = None
    udir = settings.state_dir / "styles"; udir.mkdir(parents=True)
    (udir / "styles.yaml").write_text('styles:\n  evil:\n    description: d\n    ref: "../../etc/passwd"\n')
    with caplog.at_level("WARNING", logger="gemini_image_api.styles"):
        cat = load_catalog(settings)
    assert cat.styles["evil"].ref is None and cat.styles["evil"].source == "user"
    assert "escapes ref_base" in caplog.text


def test_preview_prompt_style_template_aspect(catalog):
    from gemini_image_api.styles import preview_prompt
    out = preview_prompt(catalog, prompt="", style="anime", template="anuncio",
                         template_vars={"producto": "zapato"}, aspect_hint="16:9")
    assert "foto de zapato" in out["final_prompt"] and "anime style, cel shading" in out["final_prompt"]
    assert ASPECT_HINTS["16:9"] in out["final_prompt"]
    assert out["style_ref"] is None and out["aspect_hint"] == "16:9"


def test_preview_prompt_style_ref_and_errors(settings):
    from gemini_image_api.style_store import UserStyleStore
    from gemini_image_api.styles import preview_prompt
    settings.styles_file = None
    s = UserStyleStore(settings.state_dir).create(
        name="Ref", append="x", image=(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "a.png"))
    cat = load_catalog(settings)
    assert preview_prompt(cat, prompt="p", style=s.id)["style_ref"] == str(s.ref)
    with pytest.raises(GIAError) as e:
        preview_prompt(cat, prompt="p", style="nope")
    assert e.value.code == "unknown_style"


def test_preview_prompt_propagates_template_and_aspect_errors(catalog):
    from gemini_image_api.styles import preview_prompt
    with pytest.raises(GIAError) as e:
        preview_prompt(catalog, prompt="", template="anuncio", template_vars={})
    assert e.value.code == "template_missing_var"
    with pytest.raises(GIAError) as e:
        preview_prompt(catalog, prompt="x", aspect_hint="7:5")
    assert e.value.code == "invalid_aspect"
