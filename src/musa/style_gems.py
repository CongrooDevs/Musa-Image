"""Per-style Gemini Gems persisted in <state_dir>/gems.json ({style_id: entry})."""
import json
import logging
import os
import time
from pathlib import Path

from filelock import FileLock

from .styles import Style

log = logging.getLogger(__name__)
_EMPTY = {"version": 1, "gems": {}}


def gem_name(style: Style) -> str:
    return f"Musa · {style.name}"[:60]


def default_instructions(style: Style) -> str:
    """Fixed English template (spec §3.1) filled with the style's data."""
    lines = [
        f'You are an image-generation assistant that always works in the "{style.name}" style.',
        f"Style description: {style.description}",
        f"Visual rules: {style.append}",
    ]
    if style.ref is not None:
        credit = f" ({style.credit})" if style.credit else ""
        lines.append(f"The first attached image is the style reference{credit}; "
                     "match its palette, brushwork and composition principles, never its subject.")
    lines.append("Always answer by generating an image. Do not search the web. "
                 "Keep any extra text to one short sentence.")
    lines.append("If the user attaches other images, keep their subject and layout and apply this style.")
    return "\n".join(lines)


class StyleGemStore:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "gems.json"
        self._lock = FileLock(str(self.dir / "gems.lock"))

    # -- io -------------------------------------------------------------
    def _read(self) -> dict:
        if not self.file.exists():
            return json.loads(json.dumps(_EMPTY))
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("gems"), dict):
                raise ValueError("invalid shape")
        except (ValueError, UnicodeDecodeError) as e:
            bad = self.file.with_name(f"gems.json.corrupt-{int(time.time())}")
            os.replace(self.file, bad)
            log.warning("gems.json was unreadable (%s); moved to %s", e, bad.name)
            return json.loads(json.dumps(_EMPTY))
        data.setdefault("version", 1)
        return data

    def _write(self, data: dict) -> None:
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.file)

    # -- api ------------------------------------------------------------
    def get(self, style_id: str) -> dict | None:
        with self._lock:
            return self._read()["gems"].get(style_id)

    def put(self, style_id: str, entry: dict) -> None:
        with self._lock:
            data = self._read()
            data["gems"][style_id] = entry
            self._write(data)

    def remove(self, style_id: str) -> None:
        with self._lock:
            data = self._read()
            if data["gems"].pop(style_id, None) is not None:
                self._write(data)

    def all(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._read()["gems"])

    def mtime(self) -> float:
        try:
            return self.file.stat().st_mtime
        except OSError:
            return 0.0
