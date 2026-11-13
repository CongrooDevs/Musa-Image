import json
import sys
import uuid
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from filelock import FileLock

from .errors import INVALID_QUERY, GIAError


@dataclass
class GenerationRecord:
    id: str
    ts: str
    type: str
    conversation_id: str
    chat_metadata: list | None
    prompt: str
    style: str | None
    style_text: str | None
    aspect_hint: str | None
    model: str | None
    project: str | None
    subdir: str | None
    refs: list[dict]
    images: list[dict]
    status: str
    code: str | None
    gemini_text: str
    elapsed_s: float
    parent_id: str | None
    user_prompt: str | None = None
    gem_id: str | None = None

    def to_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_line(cls, line: str) -> "GenerationRecord":
        return cls(**json.loads(line))

def display_prompt(rec: "GenerationRecord") -> str:
    if rec.user_prompt:
        return rec.user_prompt
    from .styles import GENERATE_PREFIX  # lazy import to avoid a potential import cycle
    return (rec.prompt or "").removeprefix(GENERATE_PREFIX)


class StateStore:
    LIST_MAX = 100

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.state_dir / "generations.jsonl"
        self.lock_path = self.state_dir / "state.lock"

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def append(self, record: GenerationRecord) -> None:
        with FileLock(str(self.lock_path)):
            with self.file.open("a", encoding="utf-8") as f:
                f.write(record.to_line() + "\n")
                f.flush()

    def load_all(self) -> list[GenerationRecord]:
        if not self.file.exists():
            return []
        out = []
        with FileLock(str(self.lock_path)):
            for n, line in enumerate(self.file.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    out.append(GenerationRecord.from_line(line))
                except (json.JSONDecodeError, TypeError, KeyError) as e:
                    print(f"warning: skipping corrupt line {n} in {self.file}: {type(e).__name__}",
                          file=sys.stderr)
        return out

    def conversation(self, cid: str) -> GenerationRecord | None:
        last = None
        for r in self.load_all():
            if r.conversation_id != cid:
                continue
            if r.type == "delete":
                last = None
            elif r.chat_metadata:
                last = r
        return last

    def generation(self, gen_id: str) -> GenerationRecord | None:
        return next((r for r in self.load_all() if r.id == gen_id), None)

    def visible_records(self) -> list[GenerationRecord]:
        """Non-delete records newer than the last delete of their conversation, ordered by ts ascending. No query/limit."""
        records = self.load_all()
        last_delete: dict[str, tuple] = {}
        for r in records:
            if r.type == "delete":
                prev = last_delete.get(r.conversation_id)
                if prev is None or self._ts_key(r.ts) > prev:
                    last_delete[r.conversation_id] = self._ts_key(r.ts)
        out = [r for r in records
               if r.type != "delete"
               and (r.conversation_id not in last_delete or self._ts_key(r.ts) > last_delete[r.conversation_id])]
        out.sort(key=lambda r: self._ts_key(r.ts))
        return out

    @staticmethod
    def _parse_ts(value: str) -> datetime:
        """ISO text -> aware datetime; naive values (e.g. a bare date) are taken as UTC."""
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    @classmethod
    def _ts_key(cls, ts: str) -> tuple:
        """Sort key: parsed datetime (minimum if unparsable), then the raw text as tie-break."""
        try:
            return (cls._parse_ts(ts), ts)
        except (ValueError, TypeError):
            return (datetime.min.replace(tzinfo=timezone.utc), str(ts))

    def list(self, *, query: str | None = None, project: str | None = None, since: str | None = None,
             status: str | None = None, model: str | None = None, where=None, limit: int = 20,
             offset: int = 0) -> list[GenerationRecord]:
        """Newest first. `since` is compared as a datetime (mixed zones ok); `model` is an exact match;
        `where` is an extra record predicate. All filters apply before offset/limit; limit <= 0 returns []."""
        limit = min(int(limit), self.LIST_MAX)
        if limit <= 0:
            return []
        since_dt = None
        if since is not None:
            try:
                since_dt = self._parse_ts(since)
            except ValueError:
                raise GIAError(INVALID_QUERY, f"invalid since {since!r}: expected an ISO date/time")
        q = query.lower() if query else None

        def at_or_after(r: GenerationRecord) -> bool:
            try:
                return self._parse_ts(r.ts) >= since_dt
            except (ValueError, TypeError):
                return False

        out = [r for r in self.visible_records()
               if (q is None or q in display_prompt(r).lower())
               and (project is None or r.project == project)
               and (status is None or r.status == status)
               and (model is None or r.model == model)
               and (where is None or where(r))
               and (since_dt is None or at_or_after(r))]
        out.sort(key=lambda r: self._ts_key(r.ts), reverse=True)
        off = max(0, int(offset))
        return out[off: off + limit]

    def images_per_day(self, days: int = 7, *, today: date | None = None) -> "list[dict]":
        """Saved images per local day, oldest→newest: [{"date": "YYYY-MM-DD", "images": int}].

        Counts each image key "<origin>#<index>" once, on the local day of the first
        record that saved it (retry_download keys share their origin's key). Only
        entries with a path count, whatever the record status. Deleted images still
        count: this measures consumption, not what the gallery shows.
        """
        days = max(1, int(days))
        end = today or datetime.now().date()
        start = end - timedelta(days=days - 1)
        counts = {start + timedelta(days=i): 0 for i in range(days)}
        seen: set[str] = set()
        for r in sorted(self.load_all(), key=lambda r: r.ts):
            origin = r.parent_id if r.type == "retry_download" and r.parent_id else r.id
            day = datetime.fromisoformat(r.ts).astimezone().date()
            for entry in r.images:
                if not entry.get("path"):
                    continue
                key = f"{origin}#{entry.get('index', 0)}"
                if key in seen:
                    continue
                seen.add(key)
                if day in counts:
                    counts[day] += 1
        return [{"date": d.isoformat(), "images": counts[d]} for d in sorted(counts)]

    def count_images_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        return sum(len(r.images) for r in self.load_all()
                   if r.status == "ok" and r.ts.startswith(today))

    def count_images_since(self, since_iso: str) -> int:
        """Saved images (path set) in records at/after `since_iso`, deduped by origin#index."""
        try:
            since = datetime.fromisoformat(since_iso)
        except ValueError:
            return 0
        seen = set()
        for r in sorted(self.load_all(), key=lambda r: r.ts):
            try:
                if datetime.fromisoformat(r.ts) < since:
                    continue
            except ValueError:
                continue
            origin = r.parent_id if r.type == "retry_download" and r.parent_id else r.id
            for e in r.images:
                if e.get("path"):
                    seen.add(f"{origin}#{e.get('index', 0)}")
        return len(seen)
