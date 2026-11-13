import re
from pathlib import Path
import pytest
from gemini_image_api.config import Settings
from gemini_image_api.auth import read_cookie_jar

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

_SAPISID_HASH_RE = re.compile(r'SAPISIDHASH \d+_[0-9a-f]{40}')
_EMAIL_RE = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+')

def test_fixtures_contain_no_live_secrets():
    """Safety gate: asserts that no real cookies or tokens leaked into tests/fixtures/."""
    if not FIXTURES_DIR.exists():
        return

    fixture_files = list(FIXTURES_DIR.rglob("*"))
    files_to_check = [f for f in fixture_files if f.is_file() and not f.name.startswith(".")]
    if not files_to_check:
        return

    # 1. Load real user cookies if available on the workstation
    settings = Settings()
    real_cookies = []
    if settings.cookie_file.exists():
        jar = read_cookie_jar(settings.cookie_file)
        for c in jar:
            val = c.get("value", "")
            if len(val) >= 6:
                real_cookies.append((c.get("name", "cookie"), val))

    # 2. Check each fixture file
    for p in files_to_check:
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        # Check against real cookies
        for name, secret in real_cookies:
            assert secret not in content, (
                f"SECURITY FAILURE: Real secret for {name} found in fixture {p.name}!"
            )

        # Check against unredacted SAPISID hashes
        assert not _SAPISID_HASH_RE.search(content), (
            f"SECURITY FAILURE: Unredacted SAPISIDHASH found in fixture {p.name}!"
        )
        # Check against non-example.com email addresses
        emails = _EMAIL_RE.findall(content)
        for em in emails:
            assert em.endswith("@example.com"), (
                f"SECURITY FAILURE: Real email {em} found in fixture {p.name}!"
            )
