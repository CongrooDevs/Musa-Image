"""Saved prompts: a small named-text store shared by the web UI, MCP and CLI."""
import json
import logging
import os
import time
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from .errors import INVALID_PROMPT, UNKNOWN_PROMPT, GIAError

log = logging.getLogger(__name__)
_ID_SHAPE = re.compile(r"^pr_[0-9a-f]{8}$")
MAX_PROMPTS = 200
MAX_NAME = 80
MAX_TEXT = 4000


def _empty() -> dict:
    return {"version": 1, "prompts": []}


class PromptStore:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "prompts.json"
        self._lock = FileLock(str(self.dir / "prompts.lock"))

    # -- io -------------------------------------------------------------
    @staticmethod
    def _shape_ok(data: dict) -> bool:
        items = data.get("prompts")
        if not isinstance(items, list):
            return False
        return all(
            isinstance(p, dict)
            and isinstance(p.get("id"), str)
            and isinstance(p.get("name"), str)
            and isinstance(p.get("text"), str)
            and isinstance(p.get("created"), str)
            and isinstance(p.get("uses"), int) and not isinstance(p.get("uses"), bool)
            for p in items
        )

    def _read(self) -> dict:
        if not self.file.exists():
            return _empty()
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            if not self._shape_ok(data):
                raise ValueError("invalid shape")
        except (ValueError, UnicodeDecodeError) as e:
            bad = self.file.with_name(f"prompts.json.corrupt-{time.time_ns()}")
            os.replace(self.file, bad)
            log.warning("prompts.json was unreadable (%s); moved to %s", e, bad.name)
            return _empty()
        data.setdefault("version", 1)
        return data

    def _write(self, data: dict) -> None:
        tmp = self.file.with_suffix(".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False, indent=1))
            os.replace(tmp, self.file)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _mutate(self, fn):
        with self._lock:
            data = self._read()
            result = fn(data)
            self._write(data)
            return result

    # -- validation -----------------------------------------------------
    @staticmethod
    def _clean_name(name) -> str:
        name = (name or "").strip() if isinstance(name, str) else ""
        if not name:
            raise GIAError(INVALID_PROMPT, "Prompt name is empty")
        if len(name) > MAX_NAME:
            raise GIAError(INVALID_PROMPT, f"Prompt name exceeds {MAX_NAME} characters", max=MAX_NAME)
        if _ID_SHAPE.match(name):
            raise GIAError(INVALID_PROMPT, f"Prompt name '{name}' looks like a prompt id; pick another name",
                           prompt=name)
        return name

    @staticmethod
    def _clean_text(text) -> str:
        text = (text or "").strip() if isinstance(text, str) else ""
        if not text:
            raise GIAError(INVALID_PROMPT, "Prompt text is empty")
        if len(text) > MAX_TEXT:
            raise GIAError(INVALID_PROMPT, f"Prompt text exceeds {MAX_TEXT} characters", max=MAX_TEXT)
        return text

    @staticmethod
    def _check_dup(items: list[dict], name: str, skip_id: str | None = None) -> None:
        folded = name.casefold()
        if any(p["name"].casefold() == folded and p["id"] != skip_id for p in items):
            raise GIAError(INVALID_PROMPT, f"A prompt named '{name}' already exists", prompt=name)

    @staticmethod
    def _find(data: dict, pid: str) -> dict:
        for p in data["prompts"]:
            if p["id"] == pid:
                return p
        raise GIAError(UNKNOWN_PROMPT, f"Unknown prompt: {pid}", prompt=pid)

    # -- api --------------------------------------------------------------
    def list(self) -> list[dict]:
        """Most used first, then most recently created."""
        with self._lock:
            items = self._read()["prompts"]
        ordered = sorted(enumerate(items), key=lambda t: (t[1]["uses"], t[1]["created"], t[0]), reverse=True)
        items = [p for _, p in ordered]
        return [dict(p) for p in items]

    def create(self, name: str, text: str) -> dict:
        name = self._clean_name(name)
        text = self._clean_text(text)

        def fn(data):
            if len(data["prompts"]) >= MAX_PROMPTS:
                raise GIAError(INVALID_PROMPT, f"Prompt limit reached ({MAX_PROMPTS})", max=MAX_PROMPTS)
            self._check_dup(data["prompts"], name)
            p = {"id": "pr_" + uuid.uuid4().hex[:8], "name": name, "text": text,
                 "created": datetime.now(timezone.utc).isoformat(), "uses": 0}
            data["prompts"].append(p)
            return dict(p)

        return self._mutate(fn)

    def update(self, pid: str, name: str | None = None, text: str | None = None) -> dict:
        new_name = self._clean_name(name) if name is not None else None
        new_text = self._clean_text(text) if text is not None else None

        if new_name is None and new_text is None:
            with self._lock:
                return dict(self._find(self._read(), pid))

        def fn(data):
            p = self._find(data, pid)
            if new_name is not None:
                self._check_dup(data["prompts"], new_name, skip_id=pid)
                p["name"] = new_name
            if new_text is not None:
                p["text"] = new_text
            return dict(p)

        return self._mutate(fn)

    def delete(self, pid: str) -> None:
        def fn(data):
            p = self._find(data, pid)
            data["prompts"].remove(p)

        self._mutate(fn)

    def use(self, pid: str) -> dict:
        def fn(data):
            p = self._find(data, pid)
            p["uses"] += 1
            return dict(p)

        return self._mutate(fn)

    def mtime(self) -> float:
        try:
            return self.file.stat().st_mtime
        except OSError:
            return 0.0
