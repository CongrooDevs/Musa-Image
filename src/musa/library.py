"""Organisation data for the gallery: collections (tags), favourites and deleted images."""
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from .errors import INVALID_COLLECTION, UNKNOWN_COLLECTION, GIAError

log = logging.getLogger(__name__)
_EMPTY = {"version": 1, "collections": [], "membership": {}, "favorites": [], "deleted": [], "activity": []}
ACTIVITY_CAP = 500


@dataclass(frozen=True)
class Collection:
    id: str
    name: str
    description: str
    created: str


class LibraryStore:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "library.json"
        self._lock = FileLock(str(self.dir / "library.lock"))

    # -- io -------------------------------------------------------------
    def _read(self) -> dict:
        if not self.file.exists():
            return json.loads(json.dumps(_EMPTY))
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            if not self._shape_ok(data):
                raise ValueError("invalid shape")
        except (ValueError, UnicodeDecodeError) as e:
            bad = self.file.with_name(f"library.json.corrupt-{int(time.time())}")
            os.replace(self.file, bad)
            log.warning("library.json was unreadable (%s); moved to %s", e, bad.name)
            return json.loads(json.dumps(_EMPTY))
        for k, v in _EMPTY.items():
            data.setdefault(k, json.loads(json.dumps(v)))
        return data

    @staticmethod
    def _shape_ok(data: dict) -> bool:
        colls = data.get("collections")
        if not isinstance(colls, list) or any(
            not isinstance(c, dict) or "id" not in c or "name" not in c for c in colls
        ):
            return False
        if not isinstance(data.get("membership"), dict):
            return False
        act = data.get("activity")
        if act is not None and not isinstance(act, list):
            return False
        return isinstance(data.get("favorites"), list) and isinstance(data.get("deleted"), list)

    def _write(self, data: dict) -> None:
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.file)

    def _mutate(self, fn):
        with self._lock:
            data = self._read()
            result = fn(data)
            self._write(data)
            return result

    @staticmethod
    def _log_activity(data: dict, collection: str, kind: str, *, key: str | None = None,
                      name: str | None = None) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "collection": collection, "kind": kind}
        if key is not None:
            entry["key"] = key
        if name is not None:
            entry["name"] = name
        data["activity"].append(entry)
        data["activity"] = data["activity"][-ACTIVITY_CAP:]

    def activity(self, coll_id: str | None = None, limit: int = 50) -> list[dict]:
        """Activity entries newest first, optionally filtered by collection."""
        acts = self._view()["activity"]
        if coll_id is not None:
            acts = [a for a in acts if a.get("collection") == coll_id]
        return list(reversed(acts))[: max(1, int(limit))]

    def _view(self) -> dict:
        with self._lock:
            return self._read()

    # -- collections -------------------------------------------------------
    @staticmethod
    def _coll(d: dict) -> Collection:
        return Collection(d["id"], d["name"], d.get("description", ""), d.get("created", ""))

    def collections(self) -> list[Collection]:
        return [self._coll(c) for c in self._view()["collections"]]

    def get_collection(self, id_or_name: str) -> Collection | None:
        needle = (id_or_name or "").strip()
        for c in self.collections():
            if c.id == needle or c.name.casefold() == needle.casefold():
                return c
        return None

    def _require(self, data: dict, coll_id: str) -> dict:
        found = next((c for c in data["collections"] if c["id"] == coll_id), None)
        if found is None:
            raise GIAError(UNKNOWN_COLLECTION, f"collection {coll_id!r} does not exist",
                           valid=[c["id"] for c in data["collections"]])
        return found

    def _create_in(self, data: dict, name: str, description: str = "") -> Collection:
        name = (name or "").strip()
        if not name:
            raise GIAError(INVALID_COLLECTION, "collection name is empty")
        if any(c["name"].casefold() == name.casefold() for c in data["collections"]):
            raise GIAError(INVALID_COLLECTION, f"collection {name!r} already exists")
        c = {"id": f"col_{uuid.uuid4().hex[:8]}", "name": name, "description": description or "",
             "created": datetime.now(timezone.utc).isoformat()}
        data["collections"].append(c)
        self._log_activity(data, c["id"], "created", name=name)
        return self._coll(c)

    def create_collection(self, name: str, description: str = "") -> Collection:
        return self._mutate(lambda data: self._create_in(data, name, description))

    def resolve_collection(self, id_or_name: str, *, create: bool) -> Collection:
        def fn(data):
            needle = (id_or_name or "").strip()
            found = next((c for c in data["collections"]
                          if c["id"] == needle or c["name"].casefold() == needle.casefold()), None)
            if found is not None:
                return self._coll(found)
            if not create:
                raise GIAError(UNKNOWN_COLLECTION, f"collection {id_or_name!r} does not exist",
                               valid=[c["name"] for c in data["collections"]])
            return self._create_in(data, id_or_name)
        return self._mutate(fn)

    def update_collection(self, coll_id, *, name=None, description=None) -> Collection:
        def fn(data):
            c = self._require(data, coll_id)
            if name is not None:
                new = name.strip()
                if not new or any(o["id"] != coll_id and o["name"].casefold() == new.casefold()
                                  for o in data["collections"]):
                    raise GIAError(INVALID_COLLECTION, f"invalid or duplicate collection name {name!r}")
                if new != c["name"]:
                    c["name"] = new
                    self._log_activity(data, coll_id, "renamed", name=new)
            if description is not None:
                c["description"] = description
            return self._coll(c)
        return self._mutate(fn)

    def delete_collection(self, coll_id: str) -> None:
        def fn(data):
            self._require(data, coll_id)
            data["collections"] = [c for c in data["collections"] if c["id"] != coll_id]
            data["membership"] = {k: [x for x in v if x != coll_id] for k, v in data["membership"].items()}
            data["membership"] = {k: v for k, v in data["membership"].items() if v}
            data["activity"] = [a for a in data["activity"] if a.get("collection") != coll_id]
        self._mutate(fn)

    # -- membership ----------------------------------------------------------
    def add(self, key: str, coll_id: str) -> list[str]:
        def fn(data):
            self._require(data, coll_id)
            cur = data["membership"].setdefault(key, [])
            if coll_id not in cur:
                cur.append(coll_id)
                self._log_activity(data, coll_id, "added", key=key)
            return list(cur)
        return self._mutate(fn)

    def remove(self, key: str, coll_id: str) -> list[str]:
        def fn(data):
            cur = data["membership"].get(key, [])
            if coll_id in cur:
                self._log_activity(data, coll_id, "removed", key=key)
            cur = [x for x in cur if x != coll_id]
            if cur:
                data["membership"][key] = cur
            else:
                data["membership"].pop(key, None)
            return cur
        return self._mutate(fn)

    def collections_of(self, key: str) -> list[str]:
        return list(self._view()["membership"].get(key, []))

    def members(self, coll_id: str) -> list[str]:
        return [k for k, v in self._view()["membership"].items() if coll_id in v]

    # -- favourites / deleted --------------------------------------------------
    def set_favorite(self, key: str, value: bool) -> bool:
        def fn(data):
            favs = [k for k in data["favorites"] if k != key]
            if value:
                favs.append(key)
            data["favorites"] = favs
            return value
        return self._mutate(fn)

    def is_favorite(self, key: str) -> bool:
        return key in self._view()["favorites"]

    def favorites(self) -> set[str]:
        return set(self._view()["favorites"])

    def mark_deleted(self, key: str) -> None:
        def fn(data):
            if key not in data["deleted"]:
                data["deleted"].append(key)
            data["favorites"] = [k for k in data["favorites"] if k != key]
            data["membership"].pop(key, None)
        self._mutate(fn)

    def is_deleted(self, key: str) -> bool:
        return key in self._view()["deleted"]

    def deleted(self) -> set[str]:
        return set(self._view()["deleted"])

    def mtime(self) -> float:
        return self.file.stat().st_mtime if self.file.exists() else 0.0

    def snapshot(self) -> dict:
        """Single read for request handlers that need several fields."""
        return self._view()
