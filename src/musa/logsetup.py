"""Logging setup with secret redaction (session cookies must never reach stderr or log files)."""
import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from loguru import logger as _loguru  # loguru is a dependency of the internal client, not part of it

REDACT_PATTERNS = [r"__Secure-\S+", r"cached_cookies_[^\s/'\"]+", r"SAPISID\S*", r"Cookie:.*"]
_REDACTED = "[REDACTED]"
_COMPILED = [re.compile(p) for p in REDACT_PATTERNS]
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_OWN = "_gia_handler"
_upstream_sink: int | None = None  # loguru handler id of the upstream.log sink


def redact(text: str) -> str:
    for rx in _COMPILED:
        text = rx.sub(_REDACTED, text)
    return text


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info:  # render the traceback now so it can be redacted too
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def setup_logging(state_dir: Path) -> None:
    """Route stdlib logging and loguru (used by the internal client) to stderr + a rotating file, redacted.

    Never writes to stdout: the MCP stdio transport owns it.
    """
    log_dir = Path(state_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, _OWN, False)]:
        root.removeHandler(h)
        h.close()
    fmt = logging.Formatter(_FORMAT)
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    fileh = RotatingFileHandler(log_dir / "gia.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fileh.setLevel(logging.INFO)
    for h in (err, fileh):
        h.setFormatter(fmt)
        h.addFilter(_RedactFilter())
        setattr(h, _OWN, True)
        root.addHandler(h)
    root.setLevel(logging.INFO)

    _loguru.remove()
    global _upstream_sink
    _upstream_sink = None
    _loguru.configure(patcher=lambda record: record.update(message=redact(record["message"])))
    _loguru.add(sys.stderr, level="WARNING", format="{time:HH:mm:ss} {level} {name}: {message}")


def add_upstream_file_sink(state_dir: Path) -> None:
    """the internal client's set_log_level() drops every loguru sink; re-add a redacted file sink after it.

    Idempotent: a sink added by a previous call is replaced, never duplicated.
    """
    global _upstream_sink
    log_dir = Path(state_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if _upstream_sink is not None:
        try:
            _loguru.remove(_upstream_sink)
        except ValueError:  # already dropped (e.g. by set_log_level / logger.remove())
            pass
        _upstream_sink = None
    _loguru.configure(patcher=lambda record: record.update(message=redact(record["message"])))
    _upstream_sink = _loguru.add(log_dir / "upstream.log", level="WARNING", rotation="1 MB", retention=3,
                                 format="{time:YYYY-MM-DD HH:mm:ss} {level} {name}: {message}")
