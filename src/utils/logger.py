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
  all subsequent status changes, fills, and cancellations.  Each line is
  stamped with the signed-in NanoPulse username — see ``set_log_user``.

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
# ``user`` is injected by ``_SessionUserFilter`` — see ``set_log_user``.
_ORDER_FMT = "%(asctime)s  %(levelname)-5s  %(user)-12s  %(message)s"
_DATE_FMT = "%H:%M:%S"

# Placeholder written when no one is signed in (startup before the auth
# gate, and the window between sign-out and the next sign-in).
NO_USER = "-"

# Maximum 5 MB per file, keep 3 backups → ≤ 20 MB total
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3

# ── Dedicated loggers (module-level singletons) ──────────────────────────────
tws_log: logging.Logger = logging.getLogger("nano_tws")
order_log: logging.Logger = logging.getLogger("nano_order")

# The signed-in NanoPulse username, stamped onto every nano_order line.
# One process hosts exactly one session (``AppController`` tears the
# MainWindow down on sign-out before the next user appears), so a plain
# module global is enough — and it has to be readable without a lock:
# most order_log calls come from an ``IBWorkerThread`` coroutine, and a
# logging filter must never block the thread it runs on.  Rebinding a
# module global is atomic under the GIL, so the worker either sees the
# old name or the new one, never a torn value.
_session_user: str = NO_USER


def set_log_user(username: str | None) -> None:
    """Set the username stamped onto every ``nano_order`` line.

    Call it right after authentication and again with ``None`` on sign-out
    (``AppController`` does both).  The stamp answers "which NanoPulse
    session wrote this line", which for the app's own actions
    (ORDER-SUBMIT / ORDER-PLACE-* / ORDER-CANCEL / ORDER-FLATTEN …) is
    the person who triggered them.

    It is NOT proof of who originated an order.  ORDER-SNAPSHOT and
    ORDER-STATUS lines are replays of what TWS pushed — ``reqAllOpenOrders``
    returns orders from every client on the connection, including ones
    placed in TWS itself or by another API session.  Those lines carry
    the username of whoever happened to be watching.  The order id still
    settles origin: a ``nano_``-prefixed orderRef is ours, an
    ``{account}_p{permId}`` id came from somewhere else (see
    ``IBWorkerThread._order_key``).
    """
    global _session_user
    _session_user = (username or NO_USER).strip() or NO_USER


def get_log_user() -> str:
    """The username currently being stamped (``"-"`` when signed out)."""
    return _session_user


class _SessionUserFilter(logging.Filter):
    """Injects ``record.user`` so ``_ORDER_FMT`` can render it.

    A filter rather than 48 changed call sites: every nano_order line —
    including ones added later — is stamped without the callers knowing
    the field exists.  Never returns False; it only decorates.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.user = _session_user
        return True


def _configure_dedicated_logger(
    logger: logging.Logger,
    log_file: Path,
    fmt: str,
    file_level: int = logging.DEBUG,
    filters: tuple[logging.Filter, ...] = (),
) -> None:
    """Attach a rotating-file handler to *logger* (idempotent).

    ``propagate`` is set to ``False`` so messages stay in the dedicated
    file and do not bubble up to the root / console handlers.

    *file_level* controls both the logger and handler threshold.
    Pass ``logging.INFO`` for high-frequency loggers (e.g. tws_log) to
    avoid disk I/O from DEBUG-level pnlSingle / accountSummary spam.
    Setting the logger level (not just the handler level) ensures Python
    skips LogRecord creation entirely for below-threshold calls.

    *filters* are attached to the HANDLER, not the logger: when *fmt*
    names a custom field, only a handler-side filter guarantees every
    record that reaches the formatter carries it — a logger-side filter
    is skipped for records that arrive from a child logger, and the
    formatter would raise on the missing attribute.
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
    for filt in filters:
        fh.addFilter(filt)
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
    # Gate at the logger, not just the handlers: with the root pinned to
    # DEBUG, every log.debug() in the hot paths (price ticks, portfolio
    # batches, order events) built a LogRecord and ran findCaller's
    # stack walk only for both handlers to reject it.  Pass
    # ``setup_logging(logging.DEBUG)`` to capture debug in the file
    # handler again — the level flows through to it below.
    root.setLevel(level)

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
                                file_level=logging.INFO,
                                filters=(_SessionUserFilter(),))

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
