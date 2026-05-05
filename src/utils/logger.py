"""
Centralized logging configuration for NanoPulse.

Call ``setup_logging()`` once at application startup (in ``main.py``).
Every module simply does::

    import logging
    log = logging.getLogger(__name__)

and the messages automatically go to **both** the console and a rotating
log file under ``data/logs/nanopulse.log``.

Two **dedicated** loggers are also configured:

* ``nano_tws`` → ``data/logs/nano_tws.log``
  Captures every IBKR ``ib_async`` API call (request / response / error)
  and every event pushed from TWS (account summary, portfolio, order
  status, PnL, pending tickers, errors, …).

* ``nano_order`` → ``data/logs/nano_order.log``
  Captures every order that NanoPulse submits to IBKR TWS together with
  all subsequent status changes, fills, and cancellations.

Usage in other modules::

    from src.utils.logger import tws_log, order_log

Both are plain ``logging.Logger`` instances with their own rotating file
handlers.  They do **not** propagate to the root logger, so their output
stays isolated in the dedicated files.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[2] / "data" / "logs"
LOG_FILE = LOG_DIR / "nanopulse.log"
TWS_LOG_FILE = LOG_DIR / "nano_tws.log"
ORDER_LOG_FILE = LOG_DIR / "nano_order.log"

_CONSOLE_FMT = "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s"
_FILE_FMT = "%(asctime)s  %(levelname)-5s  [%(name)s:%(lineno)d]  %(message)s"
_TWS_FMT = "%(asctime)s  %(levelname)-5s  %(message)s"
_ORDER_FMT = "%(asctime)s  %(levelname)-5s  %(message)s"
_DATE_FMT = "%H:%M:%S"

# Maximum 5 MB per file, keep 3 backups → ≤ 20 MB total
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3

# ── Dedicated loggers (module-level singletons) ──────────────────────────────
tws_log: logging.Logger = logging.getLogger("nano_tws")
order_log: logging.Logger = logging.getLogger("nano_order")


def _configure_dedicated_logger(
    logger: logging.Logger,
    log_file: Path,
    fmt: str,
    file_level: int = logging.DEBUG,
) -> None:
    """Attach a rotating-file handler to *logger* (idempotent).

    ``propagate`` is set to ``False`` so messages stay in the dedicated
    file and do not bubble up to the root / console handlers.

    *file_level* controls both the logger and handler threshold.
    Pass ``logging.INFO`` for high-frequency loggers (e.g. tws_log) to
    avoid disk I/O from DEBUG-level pnlSingle / accountSummary spam.
    Setting the logger level (not just the handler level) ensures Python
    skips LogRecord creation entirely for below-threshold calls.
    """
    if logger.handlers:
        return  # already configured
    logger.setLevel(file_level)
    logger.propagate = False

    fh = RotatingFileHandler(
        log_file,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(file_level)
    fh.setFormatter(logging.Formatter(fmt, datefmt=_DATE_FMT))
    logger.addHandler(fh)


def setup_logging(level: int = logging.INFO) -> None:
    """
    Configure the root logger with a console handler and a rotating file handler.

    Also sets up two dedicated loggers:
    * ``nano_tws``   → ``data/logs/nano_tws.log``
    * ``nano_order`` → ``data/logs/nano_order.log``

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

    # ── Dedicated loggers ─────────────────────────────────────────────────────
    # Both dedicated loggers run at INFO — the production setting.
    # pnlSingleEvent / accountSummaryEvent / pendingTickers each fire
    # many times per second, so dropping their DEBUG-level chatter keeps
    # the rotating 5 MB × 3 backups window from churning and avoids disk
    # I/O starving the worker thread.  Drop to DEBUG temporarily when
    # tracing wire-level issues.
    _configure_dedicated_logger(tws_log, TWS_LOG_FILE, _TWS_FMT,
                                file_level=logging.INFO)
    _configure_dedicated_logger(order_log, ORDER_LOG_FILE, _ORDER_FMT,
                                file_level=logging.INFO)

    # ── Pipe ib_async (TWS protocol) chatter into nano_tws.log ────────────
    # ib_async's loggers are the source of truth for every wire-level
    # request / response / event between Nano and TWS.  Attach the
    # nano_tws file handler to the root ``ib_async`` logger so all
    # children (``ib_async.wrapper``, ``ib_async.client``, …) inherit
    # it via the standard logger hierarchy.  ``propagate=False`` keeps
    # the messages out of the global ``nanopulse.log`` file and the
    # console — both would drown in the volume.
    shared_tws_handler = tws_log.handlers[0]
    ia = logging.getLogger("ib_async")
    ia.setLevel(logging.INFO)
    if shared_tws_handler not in ia.handlers:
        ia.addHandler(shared_tws_handler)
    ia.propagate = False

    logging.getLogger("NanoPulse").info(
        "Logging initialised  (console=INFO, file=%s level=%s, "
        "nano_tws=INFO, nano_order=INFO, ib_async→nano_tws.log)",
        LOG_FILE,
        logging.getLevelName(level),
    )
