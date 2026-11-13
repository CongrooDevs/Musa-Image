"""Update checking and installation routines for Musa."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

REPO_URL = "git+https://github.com/CongrooDevs/Musa-Image.git"
GITHUB_COMMITS_URL = "https://api.github.com/repos/CongrooDevs/Musa-Image/commits/main"
CHECK_INTERVAL_S = 86400.0  # 24 hours

def get_local_sha() -> str | None:
    """Attempts to retrieve the installed git commit SHA via PEP 610 or local repo root."""
    try:
        import importlib.metadata
        dist = importlib.metadata.distribution("gemini-image-api")
        direct_url_text = dist.read_text("direct_url.json")
        if direct_url_text:
            data = json.loads(direct_url_text)
            commit_id = data.get("vcs_info", {}).get("commit_id")
            if commit_id:
                return str(commit_id)
    except Exception:
        pass

    try:
        repo_root = Path(__file__).resolve().parent.parent.parent
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=1.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return None

def check_update(state_dir: Path, force: bool = False) -> dict[str, Any]:
    """Checks GitHub for new commits, using a local 24h cache."""
    cache_path = Path(state_dir) / "update_check.json"
    now = time.time()

    if not force and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and (now - cached.get("last_checked", 0) < CHECK_INTERVAL_S):
                return cached
        except Exception:
            pass

    import httpx
    try:
        resp = httpx.get(
            GITHUB_COMMITS_URL,
            headers={"User-Agent": "musa-update-checker"},
            timeout=3.0,
        )
        if resp.status_code != 200:
            return {"has_update": False, "error": f"HTTP {resp.status_code}"}

        data = resp.json()
        remote_sha = data.get("sha", "")
        commit_msg = (data.get("commit", {}).get("message") or "").splitlines()[0]
        local_sha = get_local_sha()

        has_update = bool(local_sha and remote_sha and not local_sha.startswith(remote_sha) and not remote_sha.startswith(local_sha))

        result = {
            "last_checked": now,
            "has_update": has_update,
            "latest_sha": remote_sha,
            "current_sha": local_sha or "unknown",
            "message": commit_msg,
        }

        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

        return result
    except Exception as e:
        return {"has_update": False, "error": str(e)}

def run_update() -> int:
    """Invokes uv tool install --force to update Musa."""
    uv_path = shutil.which("uv")
    if not uv_path:
        raise RuntimeError("No se encontró el ejecutable 'uv' en el PATH del sistema.")

    cmd = [uv_path, "tool", "install", "--force", REPO_URL]
    res = subprocess.run(cmd)
    return res.returncode
