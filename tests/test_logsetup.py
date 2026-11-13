import logging

import pytest
from loguru import logger

from musa.logsetup import redact, setup_logging


@pytest.fixture(autouse=True)
def _restore_logging():
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
            h.close()
    root.setLevel(level)
    logger.remove()


def test_redact_patterns():
    out = redact("a __Secure-1PSID=abc123 b SAPISID=zzz c cached_cookies_deadbeef d Cookie: a=b")
    for secret in ("abc123", "zzz", "deadbeef", "a=b"):
        assert secret not in out
    assert "REDACTED" in out


def test_logs_are_redacted_on_stderr_and_file(tmp_path, capsys):
    setup_logging(tmp_path)
    logging.getLogger("x").warning("token __Secure-1PSID=abc123 SAPISID=zzz Cookie: a=b")
    logger.warning("cached_cookies_deadbeef path")
    for h in logging.getLogger().handlers:
        h.flush()
    captured = capsys.readouterr()
    log_text = (tmp_path / "logs" / "gia.log").read_text()
    for text in (captured.err, log_text):
        assert "abc123" not in text and "deadbeef" not in text and "zzz" not in text
        assert "REDACTED" in text
    assert captured.out == ""


@pytest.mark.parametrize("psid", ["g.a000abc123XYZ", "dGhpc0lzQmFzZTY0UFNJRA==", "AbC-def_123"])
def test_redact_real_cache_filenames(psid):
    msg = f"Failed to read cached cookies: [Errno 13] '/tmp/gemini_webapi/.cached_cookies_{psid}.json'"
    out = redact(msg)
    assert psid not in out and "REDACTED" in out


async def test_upstream_file_sink_survives_set_log_level_and_redacts(tmp_path):
    from loguru import logger
    from musa.logsetup import setup_logging, add_upstream_file_sink
    setup_logging(tmp_path)
    logger.remove()                       # what upstream set_log_level does
    add_upstream_file_sink(tmp_path)
    logger.warning("rotate failed for cached_cookies_g.a000SECRET.json")
    await logger.complete()
    text = (tmp_path / "logs" / "upstream.log").read_text()
    assert "rotate failed" in text and "g.a000SECRET" not in text


async def test_upstream_file_sink_is_idempotent(tmp_path):
    from musa.logsetup import add_upstream_file_sink
    setup_logging(tmp_path)
    add_upstream_file_sink(tmp_path)
    add_upstream_file_sink(tmp_path)  # e.g. build_backend called twice
    logger.warning("only once please")
    await logger.complete()
    text = (tmp_path / "logs" / "upstream.log").read_text()
    assert text.count("only once please") == 1
