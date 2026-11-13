from functools import lru_cache
from pathlib import Path
import os
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GIA_",
        yaml_file=str(Path.home() / ".config/gemini-image-api/config.yaml"),
        extra="ignore",
    )
    state_dir: Path = Path.home() / ".gemini-image-api"
    output_root: Path = Path.home() / "Pictures" / "gemini-image-api"
    styles_file: Path | None = None
    quota_stop_pct: float = 90.0
    gen_timeout_s: float = 300.0
    download_timeout_s: float = 60.0
    max_refs: int = 10
    max_ref_bytes: int = 20 * 1024 * 1024
    allowed_ref_dirs: str = ""          # ":"-separated paths; empty = output_root + cwd if usable
    write_sidecar: bool = False
    min_delay_s: float = 2.0
    default_model: str = "gemini-flash"
    thumb_size: int = 512
    firefox_path: str | None = None     # real (non-automated) Firefox binary used by `musa auth`
    app_port: int = 8765                # Musa web app port (GIA_APP_PORT)
    app_with_mcp: bool = False          # musa-mcp also starts Musa (GIA_APP_WITH_MCP)
    temp_chats: bool = True              # generate in temporary chats: they never appear in the Gemini chat history (GIA_TEMP_CHATS)
    chrome_path: str | None = None       # Google Chrome binary for the ChatGPT (web) provider (GIA_CHROME_PATH)
    chatgpt_idle_close_s: float = 120.0  # close the headless Chrome after this many idle seconds (proposal, not measured)
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # env > user yaml; a missing yaml file is ignored.
        return (init_settings, env_settings, YamlConfigSettingsSource(settings_cls))

    @property
    def cookie_dir(self) -> Path: return self.state_dir / "cookies"
    @property
    def cookie_file(self) -> Path: return self.cookie_dir / "cookies.json"
    @property
    def generations_file(self) -> Path: return self.state_dir / "generations.jsonl"
    @property
    def lock_file(self) -> Path: return self.state_dir / "state.lock"
    @property
    def firefox_profile_dir(self) -> Path: return self.state_dir / "firefox-profile"
    @property
    def chatgpt_profile_dir(self) -> Path: return self.state_dir / "chatgpt-chrome-profile"
    @property
    def chatgpt_lock_file(self) -> Path: return self.state_dir / "chatgpt-profile.lock"

    def allowed_ref_dirs_list(self) -> list[Path]:
        if self.allowed_ref_dirs.strip():
            return [Path(p).expanduser().resolve() for p in self.allowed_ref_dirs.split(":") if p.strip()]
        dirs = [self.output_root.expanduser().resolve()]
        cwd = Path.cwd().resolve()
        if usable_cwd(cwd) and cwd not in dirs:
            dirs.append(cwd)
        return dirs

    @property
    def upstream_cache_dir(self) -> Path: return self.cookie_dir / "upstream_cache"

    def ensure_dirs(self) -> None:
        """Create the private dirs 0700 and tighten pre-existing ones (incl. upstream cookie cache files)."""
        for d in (self.state_dir, self.cookie_dir, self.upstream_cache_dir, self.state_dir / "logs",
                  self.chatgpt_profile_dir):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(d, 0o700)
        for f in self.upstream_cache_dir.iterdir():
            if f.is_file() and not f.is_symlink():
                os.chmod(f, 0o600)


def usable_cwd(p: Path) -> bool:
    """The process cwd is only trusted as a ref/copy root if it is a real, writable project dir.
    MCP hosts may start servers with cwd="/" (e.g. Claude Desktop on macOS)."""
    p = p.resolve()
    return p != Path("/") and os.access(p, os.W_OK)

@lru_cache(maxsize=1)
def load_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
