import pytest

from musa.config import Settings


@pytest.fixture
def png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setitem(Settings.model_config, "yaml_file", str(tmp_path / "none.yaml"))  # isolate from user yaml
    monkeypatch.setenv("GIA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GIA_OUTPUT_ROOT", str(tmp_path / "generated"))
    monkeypatch.setenv("GIA_STYLES_FILE", str(tmp_path / "styles.yaml"))
    return Settings()
