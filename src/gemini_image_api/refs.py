import asyncio, hashlib, ipaddress, re, socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse
import httpx
from .config import Settings
from .state import GenerationRecord
from .errors import GIAError, INVALID_REF, REF_NOT_ALLOWED, SSRF_BLOCKED, TIMEOUT

MAGIC = [(b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"), (b"GIF87a", "gif"), (b"GIF89a", "gif")]
MAX_REDIRECTS = 3
# Some hosts (e.g. Wikimedia) reject the default python-httpx User-Agent with 403.
USER_AGENT = "gemini-image-api/0.1 (local image reference fetcher)"

def sniff_image_format(data: bytes) -> str | None:
    head = data[:512].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "svg"
    for m in MAGIC:
        if data.startswith(m[0]):
            return m[1]
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


SVG_NOT_SUPPORTED = "SVG is not supported: Gemini cannot read SVG images; export it as PNG or JPEG first"

def svg_to_png(data: bytes, scale: float = 2.0) -> bytes:
    """Rasterize SVG to PNG bytes (svglib → PDF → pypdfium2). Raises ValueError if it cannot."""
    import io

    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPDF
    import pypdfium2 as pdfium

    drawing = svg2rlg(io.BytesIO(data))
    if drawing is None:
        raise ValueError("SVG could not be parsed")
    pdf_buf = io.BytesIO()
    renderPDF.drawToFile(drawing, pdf_buf)
    doc = pdfium.PdfDocument(pdf_buf.getvalue())
    try:
        if len(doc) == 0:
            raise ValueError("SVG produced an empty PDF")
        img = doc[0].render(scale=scale).to_pil().convert("RGB")
        out = io.BytesIO()
        img.save(out, "PNG")
        return out.getvalue()
    finally:
        doc.close()
    return out.getvalue()

def validate_local_ref(path: Path, settings: Settings) -> Path:
    p = Path(path).expanduser()
    if p.is_symlink() or not p.exists() or not p.is_file():
        raise GIAError(REF_NOT_ALLOWED, f"ref not accessible: {path}")
    rp = p.resolve()
    if settings.state_dir.resolve() in rp.parents:
        raise GIAError(REF_NOT_ALLOWED, "refs inside the state dir (cookies) are not allowed")
    if not any(rp.is_relative_to(d) for d in settings.allowed_ref_dirs_list()):
        raise GIAError(REF_NOT_ALLOWED, f"{rp} outside allowed_ref_dirs", allowed=settings.allowed_ref_dirs_list())
    size = rp.stat().st_size
    if size > settings.max_ref_bytes:
        raise GIAError(INVALID_REF, f"ref exceeds {settings.max_ref_bytes} bytes", path=str(rp))
    with rp.open("rb") as fh:
        head = fh.read(512)
    fmt = sniff_image_format(head)
    if fmt is None:
        raise GIAError(INVALID_REF, "file is not an image (png/jpeg/webp/gif; SVG not supported)", path=str(rp))
    if fmt == "svg":
        raise GIAError(INVALID_REF, SVG_NOT_SUPPORTED, path=str(rp))
    return rp

def validate_trusted_image(path: Path, root: Path, settings: Settings) -> Path:
    """Trusted image inside `root` (style refs, <state_dir>/uploads): no symlink, real file,
    resolved path stays inside root, size <= max_ref_bytes, and image magic bytes."""
    p = Path(path).expanduser()
    if p.is_symlink() or not p.is_file():
        raise GIAError(INVALID_REF, f"trusted ref not accessible: {path}")
    rp = p.resolve()
    root_r = Path(root).resolve()
    if not rp.is_relative_to(root_r):
        raise GIAError(INVALID_REF, f"{rp} outside trusted root {root_r}", path=str(rp))
    size = rp.stat().st_size
    if size > settings.max_ref_bytes:
        raise GIAError(INVALID_REF, f"ref exceeds {settings.max_ref_bytes} bytes", path=str(rp))
    with rp.open("rb") as fh:
        head = fh.read(512)
    fmt = sniff_image_format(head)
    if fmt is None:
        raise GIAError(INVALID_REF, "file is not an image (png/jpeg/webp/gif; SVG not supported)", path=str(rp))
    if fmt == "svg":
        raise GIAError(INVALID_REF, SVG_NOT_SUPPORTED, path=str(rp))
    return rp

GEN_REF_RE = re.compile(r"^gen:(g_[0-9a-f]{12})(?:#(\d+))?$")

def _resolve_gen_ref(ref: str, settings: Settings,
                     lookup: Callable[[str], GenerationRecord | None] | None,
                     is_deleted: Callable[[str], bool] | None = None) -> Path:
    m = GEN_REF_RE.match(ref)
    if not m:
        raise GIAError(INVALID_REF, f"bad gen ref format {ref!r}; expected gen:g_<12 hex>[#index]")
    if lookup is None:
        raise GIAError(INVALID_REF, "gen: refs need the generation history")
    rec = lookup(m.group(1))
    if rec is None:
        raise GIAError(INVALID_REF, f"unknown generation {m.group(1)}; see list_generations")
    idx = int(m.group(2) or 0)
    entry = next((e for e in rec.images if e.get("index") == idx), None)
    if entry is None:
        raise GIAError(INVALID_REF, f"generation {rec.id} has no image index {idx}")
    if is_deleted is not None and is_deleted(f"{rec.id}#{idx}"):
        raise GIAError(INVALID_REF, f"image {idx} of {rec.id} was deleted")
    if not entry.get("path"):
        raise GIAError(INVALID_REF, f"image {idx} of {rec.id} was not saved; call retry_download first")
    return validate_local_ref(Path(entry["path"]), settings)

def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not ip.is_global or ip.is_multicast

async def _resolve_public_ip(hostname: str, port: int) -> str:
    """Resolve hostname and return one validated public IP; block if any address is non-public."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise GIAError(INVALID_REF, f"DNS failed for {hostname}: {e}")
    ips = [ipaddress.ip_address(i[4][0]) for i in infos]
    if not ips:
        raise GIAError(INVALID_REF, f"DNS returned no addresses for {hostname}")
    if any(_blocked(ip) for ip in ips):
        raise GIAError(SSRF_BLOCKED, f"{hostname} resolves to a blocked IP", ips=[str(i) for i in ips])
    return str(ips[0])

def _parse_http_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise GIAError(INVALID_REF, f"invalid URL: {url}")
    return parsed

async def _download_bytes(url: str, settings: Settings) -> bytes:
    current = url
    async with httpx.AsyncClient(follow_redirects=False, timeout=settings.download_timeout_s) as client:
        for _ in range(MAX_REDIRECTS + 1):
            parsed = _parse_http_url(current)
            hostname = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            ip = await _resolve_public_ip(hostname, port)
            # Pin the connection to the validated IP to defeat DNS rebinding.
            ip_host = f"[{ip}]" if ":" in ip else ip
            pinned = parsed._replace(netloc=f"{ip_host}:{port}").geturl()
            host_header = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
            async with client.stream(
                "GET", pinned,
                headers={"Host": host_header, "User-Agent": USER_AGENT},
                extensions={"sni_hostname": hostname},
            ) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise GIAError(INVALID_REF, f"redirect from {current} without Location")
                    current = urljoin(current, location)
                    continue
                if resp.status_code != 200:
                    raise GIAError(INVALID_REF, f"download {current} returned {resp.status_code}")
                data = bytearray()
                async for chunk in resp.aiter_bytes(65536):
                    data.extend(chunk)
                    if len(data) > settings.max_ref_bytes:
                        raise GIAError(INVALID_REF, f"download of {url} exceeds max_ref_bytes")
                return bytes(data)
    raise GIAError(INVALID_REF, f"too many redirects for {url}")

async def download_ref_url(url: str, tmpdir: Path, settings: Settings) -> Path:
    _parse_http_url(url)
    tmpdir.mkdir(parents=True, exist_ok=True)
    try:
        async with asyncio.timeout(settings.download_timeout_s):
            data = await _download_bytes(url, settings)
    except TimeoutError:
        raise GIAError(TIMEOUT, f"download of {url} timed out after {settings.download_timeout_s}s")
    except httpx.TimeoutException:
        raise GIAError(TIMEOUT, f"download of {url} timed out")
    except httpx.HTTPError as e:
        raise GIAError(INVALID_REF, f"download of {url} failed: {e}")
    fmt = sniff_image_format(data)
    if fmt is None:
        raise GIAError(INVALID_REF, f"URL {url} did not return an image")
    ext = "jpg" if fmt == "jpeg" else fmt
    out = tmpdir / f"ref_{hashlib.sha256(data).hexdigest()[:16]}.{ext}"
    out.write_bytes(data)
    return out

@dataclass(frozen=True)
class ResolvedRef:
    path: Path; source: str; sha256: str

async def resolve_refs(refs: list[str], settings: Settings, tmpdir: Path,
                       lookup: Callable[[str], GenerationRecord | None] | None = None,
                       is_deleted: Callable[[str], bool] | None = None) -> list[ResolvedRef]:
    if len(refs) > settings.max_refs:
        raise GIAError(INVALID_REF, f"maximum {settings.max_refs} refs per call", max_refs=settings.max_refs, got=len(refs))
    out: list[ResolvedRef] = []
    for r in refs:
        if r.startswith("http://") or r.startswith("https://"):
            p = await download_ref_url(r, tmpdir, settings)
            source = r
        elif r.startswith("gen:"):
            p = _resolve_gen_ref(r, settings, lookup, is_deleted)
            source = r
        else:
            p = validate_local_ref(Path(r), settings)
            source = str(p)
        out.append(ResolvedRef(path=p, source=source, sha256=hashlib.sha256(p.read_bytes()).hexdigest()))
    return out
