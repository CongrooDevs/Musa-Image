import os
from pathlib import Path
from musa.config import Settings, load_settings, usable_cwd


def test_defaults(settings: Settings):
    assert settings.gen_timeout_s == 300.0
    assert settings.download_timeout_s == 60.0
    assert settings.max_refs == 10
    assert settings.max_ref_bytes == 20 * 1024 * 1024
    assert settings.write_sidecar is False
    assert settings.default_model == "gemini-flash"
    assert settings.thumb_size == 512
    assert settings.state_dir.name == "state"

def test_output_root_default_is_absolute_and_cwd_independent(monkeypatch, tmp_path):
    # isolate from the real user: no GIA_* env and a yaml file that does not exist. The default itself is
    # evaluated at import (Path.home() then), so HOME can't be swapped here; the yaml_file override is what isolates.
    for k in [k for k in os.environ if k.startswith("GIA_")]:
        monkeypatch.delenv(k)
    monkeypatch.setitem(Settings.model_config, "yaml_file", str(tmp_path / "none.yaml"))
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.output_root == Path.home() / "Pictures" / "gemini-image-api"
    assert s.output_root.is_absolute()

def test_quota_stop_pct_default(settings):
    assert settings.quota_stop_pct == 90.0

def test_usable_cwd():
    assert usable_cwd(Path("/")) is False

def test_allowlist_excludes_root_cwd(settings, monkeypatch):
    monkeypatch.chdir("/")
    dirs = settings.allowed_ref_dirs_list()
    assert Path("/") not in dirs and settings.output_root.resolve() in dirs

def test_allowlist_excludes_readonly_cwd(settings, monkeypatch, tmp_path):
    ro = tmp_path / "ro"; ro.mkdir(); os.chmod(ro, 0o500)
    try:
        monkeypatch.chdir(ro)
        assert ro.resolve() not in settings.allowed_ref_dirs_list()
    finally:
        os.chmod(ro, 0o700)

def test_allowlist_includes_writable_cwd(settings, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert tmp_path.resolve() in settings.allowed_ref_dirs_list()

def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("GIA_GEN_TIMEOUT_S", "120")
    monkeypatch.setenv("GIA_ALLOWED_REF_DIRS", f"{tmp_path}/a:{tmp_path}/b")
    s = Settings()
    assert s.gen_timeout_s == 120.0
    assert s.allowed_ref_dirs_list() == [tmp_path / "a", tmp_path / "b"]

def test_ensure_dirs_permissions(settings: Settings):
    settings.ensure_dirs()
    assert (settings.state_dir / "cookies").is_dir()
    assert (settings.state_dir.stat().st_mode & 0o777) == 0o700

def test_load_settings_cached(monkeypatch, settings):
    load_settings.cache_clear()
    a, b = load_settings(), load_settings()
    assert a is b
    load_settings.cache_clear()
