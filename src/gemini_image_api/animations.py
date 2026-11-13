"""Animation records persisted in <state_dir>/animations.json."""
import copy
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

log = logging.getLogger(__name__)
_EMPTY = {"version": 1, "animations": {}, "revs": {}}
_SHEET_FIELDS = ("cols", "rows", "mode", "key_color", "tolerance", "cells")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AnimationStore:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "animations.json"
        self._lock = FileLock(str(self.dir / "animations.lock"))

    # -- io -------------------------------------------------------------
    def _read(self) -> dict:
        if not self.file.exists():
            return copy.deepcopy(_EMPTY)
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("animations"), dict):
                raise ValueError("invalid shape")
            if not isinstance(data.get("revs", {}), dict):
                raise ValueError("invalid revs")
        except (ValueError, UnicodeDecodeError) as e:
            bad = self.file.with_name(f"animations.json.corrupt-{int(time.time())}")
            os.replace(self.file, bad)
            log.warning("animations.json was unreadable (%s); moved to %s", e, bad.name)
            return copy.deepcopy(_EMPTY)
        data.setdefault("version", 1)
        data.setdefault("revs", {})
        return data

    def _write(self, data: dict) -> None:
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.file)

    # -- api ------------------------------------------------------------
    def get(self, key: str) -> dict | None:
        with self._lock:
            return self._read()["animations"].get(key)

    def all(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._read()["animations"])

    def mtime(self) -> float:
        try:
            return self.file.stat().st_mtime
        except OSError:
            return 0.0

    def put(self, key: str, ficha: dict) -> dict:
        with self._lock:
            data = self._read()
            old = data["animations"].get(key)
            new = dict(ficha)
            new["key"] = key
            now = _now_iso()
            new["created"] = old.get("created", now) if old else new.get("created", now)
            new["updated"] = now
            if old is not None and all(old.get(f) == new.get(f) for f in _SHEET_FIELDS) \
                    and old.get("rev") is not None:
                new["rev"] = old["rev"]
            else:
                high = max(int(data["revs"].get(key, 0)), int((old or {}).get("rev", 0) or 0))
                new["rev"] = max(time.time_ns() // 1_000_000, high + 1)
            data["revs"][key] = max(int(data["revs"].get(key, 0)), new["rev"])
            data["animations"][key] = new
            self._write(data)
            return new

    def remove(self, key: str) -> bool:
        with self._lock:
            data = self._read()
            if data["animations"].pop(key, None) is None:
                return False
            self._write(data)
            return True

    def prune(self, valid_keys) -> int:
        valid = set(valid_keys)
        with self._lock:
            data = self._read()
            dead = [k for k in data["animations"] if k not in valid]
            stale = [k for k in data["revs"] if k not in valid]
            for k in dead:
                del data["animations"][k]
            for k in stale:
                del data["revs"][k]
            if dead or stale:
                self._write(data)
            return len(dead)
