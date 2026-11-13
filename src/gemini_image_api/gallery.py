"""Flattened gallery index: every visible, saved, non-deleted image of the history."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .library import LibraryStore
from .state import GenerationRecord, StateStore, display_prompt

_IMAGE_TYPES = {"generate", "edit", "retry_download"}
FILTERS = ("all", "in_collection", "no_collection", "favorites")
SORTS = ("newest", "oldest", "favorites")
MAX_LIMIT = 200
_MIN_TS = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ImageEntry:
    key: str
    record: GenerationRecord
    index: int
    path: Path
    width: int | None
    height: int | None


def image_index(state: StateStore, library: LibraryStore,
                output_root: Path) -> dict[str, ImageEntry]:
    """Map "<record_id>#<index>" -> ImageEntry over visible records (ts ascending).

    retry_download entries merge into their origin record (parent_id): the latest
    record wins path/width/height, but the entry keeps the origin's prompt/style.
    Deleted keys, missing files and paths outside output_root are dropped.
    """
    deleted = library.deleted()
    root = Path(output_root).resolve()
    origins: dict[str, GenerationRecord] = {}
    out: dict[str, ImageEntry] = {}
    for r in state.visible_records():
        if r.type not in _IMAGE_TYPES:
            continue
        origin = r.parent_id if r.type == "retry_download" and r.parent_id else r.id
        if r.type != "retry_download":
            origins.setdefault(origin, r)
        rec = origins.get(origin, r)
        for entry in r.images:
            raw = entry.get("path")
            if not raw:
                continue
            idx = entry.get("index", 0)
            key = f"{origin}#{idx}"
            if key in deleted:
                continue
            p = Path(raw)
            if not p.is_file():
                continue
            if not p.resolve().is_relative_to(root):
                continue
            out[key] = ImageEntry(key=key, record=rec, index=int(idx), path=p,
                                  width=entry.get("width"), height=entry.get("height"))
    return out


def _parse_bound(value: str, name: str, *, end: bool) -> datetime:
    """ISO date/datetime -> aware datetime (naive = UTC). A date-only `end` bound is the
    exclusive start of the next day, so the whole day is included."""
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"invalid {name} date {value!r}; expected ISO 8601") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end:
        # exclusive upper bound: next day for a date-only value, +1us otherwise (inclusive instant)
        try:
            dt += timedelta(days=1) if "T" not in s and " " not in s and ":" not in s \
                else timedelta(microseconds=1)
        except OverflowError:
            raise ValueError(f"invalid {name} date {value!r}; out of range") from None
    return dt


def _record_ts(e: ImageEntry) -> datetime | None:
    try:
        dt = datetime.fromisoformat(e.record.ts)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def query_images(entries: dict[str, ImageEntry], library: LibraryStore, *, query=None, collection=None,
                 filter="all", style=None, model=None, sort="newest", since=None, until=None,
                 style_names: dict[str, str] | None = None,
                 limit=60, offset=0) -> tuple[list[ImageEntry], int]:
    """Filter/sort/page the index.

    `query` matches (case-insensitive) display_prompt, the record's style id and, when
    `style_names` (id -> friendly name) is given, the style name. `model` is an exact match.
    `since`/`until` are ISO dates/datetimes (naive = UTC); a date-only `until` includes that whole day.
    sort: newest (real ts desc, key desc), oldest (real ts asc, key asc), favorites (favorites first,
    then newest). Unparseable record ts sorts as the oldest.
    """
    if filter not in FILTERS:
        raise ValueError(f"unknown filter {filter!r}; expected one of {FILTERS}")
    if sort not in SORTS:
        raise ValueError(f"unknown sort {sort!r}; expected one of {SORTS}")
    try:
        if int(limit) < 1:
            raise ValueError("limit must be >= 1")
        limit = int(limit)
        offset = int(offset)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid limit/offset: {exc}") from None
    lo = _parse_bound(since, "since", end=False) if since else None
    hi = _parse_bound(until, "until", end=True) if until else None
    if lo is not None and hi is not None and lo >= hi:
        raise ValueError("since must not be after until")
    q = query.lower() if query else None
    favs = library.favorites()
    members = set(library.members(collection)) if collection is not None else None
    out = []
    for e in entries.values():
        if q is not None and q not in display_prompt(e.record).lower() \
                and q not in (e.record.style or "").lower() \
                and q not in ((style_names or {}).get(e.record.style) or "").lower():
            continue
        if style is not None and e.record.style != style:
            continue
        if model is not None and e.record.model != model:
            continue
        if lo is not None or hi is not None:
            ts = _record_ts(e)
            if ts is None or (lo is not None and ts < lo) or (hi is not None and ts >= hi):
                continue
        colls = library.collections_of(e.key)
        if members is not None and e.key not in members:
            continue
        if filter == "in_collection" and not colls:
            continue
        if filter == "no_collection" and colls:
            continue
        if filter == "favorites" and e.key not in favs:
            continue
        out.append(e)
    # one real-time key per entry (record ts text is not comparable across zones)
    tkey = {e.key: (_record_ts(e) or _MIN_TS, e.key) for e in out}
    if sort == "oldest":
        out.sort(key=lambda e: tkey[e.key])
    elif sort == "favorites":
        out.sort(key=lambda e: (e.key in favs, tkey[e.key]), reverse=True)
    else:
        out.sort(key=lambda e: tkey[e.key], reverse=True)
    total = len(out)
    limit = min(limit, MAX_LIMIT)
    offset = max(0, offset)
    return out[offset:offset + limit], total
