"""
Centralized logging configuration for NanoPulse.

Call ``setup_logging()`` once at application startup (in ``main.py``).
Every module simply does::

    import logging
    log = logging.getLogger(__name__)

and the messages automatically go to **both** the console and a rotating
log file under ``data/logs/nanopulse.log``.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[2] / "data" / "logs"
LOG_FILE = LOG_DIR / "nanopulse.log"

_CONSOLE_FMT = "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s"
_FILE_FMT = "%(asctime)s  %(levelname)-5s  [%(name)s:%(lineno)d]  %(message)s"
_DATE_FMT = "%H:%M:%S"

# Maximum 5 MB per file, keep 3 backups → ≤ 20 MB total
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3


def setup_logging(level: int = logging.DEBUG) -> None:
    """
    Configure the root logger with a console handler and a rotating file handler.

    Parameters
    ----------
    level : int
        Minimum severity for the **file** handler (console is always INFO).
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # capture everything; handlers filter

    # ── Prevent duplicate handlers on repeated calls ──────────────────────────
    if root.handlers:
        return

    # ── Console handler (INFO+) ───────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt=_DATE_FMT))
    root.addHandler(console)

    # ── Rotating file handler (DEBUG+) ────────────────────────────────────────
    file_h = RotatingFileHandler(
        LOG_FILE,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_h.setLevel(level)
    file_h.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_DATE_FMT))
    root.addHandler(file_h)

    logging.getLogger("NanoPulse").info(
        "Logging initialised  (console=INFO, file=%s level=%s)",
        LOG_FILE,
        logging.getLevelName(level),
    )

    # ── Quiet noisy third-party loggers on the console ────────────────────────
    # ib_async fires dozens of INFO lines per tick (position, wrapper, client).
    # Keep them in the log file at DEBUG, but only show WARNING+ on screen.
    for name in ("ib_async", "ib_async.wrapper", "ib_async.client", "ib_async.ib"):
        logging.getLogger(name).setLevel(logging.WARNING)


