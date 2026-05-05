# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the desktop app
python main.py

# Run the full pytest suite (pytest + pytest-qt)
pytest tests/

# Run a single test / test class / test method
pytest tests/test_trading_prefs.py
pytest tests/test_trading_prefs.py::TestRoundToTick
pytest tests/test_trading_prefs.py::TestRoundToTick::test_nq_quarter_tick

# test_strategy_editor_model.py is a script-style smoke test — run directly
python tests/test_strategy_editor_model.py

# Install deps
pip install -r requirements.txt
```

There is no lint/format tooling configured.

## Architecture

NanoPulse is a PySide6 (Qt) desktop trading front-end for Interactive Brokers, using the `ib_async` library against TWS / Gateway. The design revolves around **one isolated QThread per broker connection** bridged to the UI via Qt Signals.

### Entry point and navigation
- `main.py` installs a global `sys.excepthook`, calls `setup_logging()`, then shows `MainWindow`.
- `src/ui/main_window.py` is a plain `QWidget` (not `QMainWindow`, deliberately — avoids hidden menu/status/central chrome). It owns a `QStackedWidget` with 5 pages: Connect, Contracts, Trade (FlashTrade), StrategyEditor, Executions. The "Settings" tab opens `PreferencesDialog` as a modeless window instead of switching pages.
- `MainWindow` pre-warms `ReferenceData` (contracts + strategy tiers) before constructing child widgets — order matters because `OrderEntryPanel`, `StrategyEditorView`, etc. read from that cache at construction.

### IB connection model (`src/core/ib_worker.py`)
- `IBWorkerThread(QThread)` = one TWS connection. `MainWindow.active_threads: dict[str, IBWorkerThread]` is keyed by connection label.
- Each thread runs its own asyncio event loop with two concurrent coroutines: an **auto-reconnect loop** (`connectAsync` every 5 s, `reqAccountSummary` on re-connect) and a **throttler** (emits a de-duplicated batch every 250 ms).
- All data crossing into the UI crosses via Qt Signals on the worker: `summary_batch_updated`, `connection_status_changed`, `price_updated`, `bid_ask_updated`, `order_status_updated`, `portfolio_updated`, `account_pnl_updated`, `pnl_single_updated`, `accounts_detected`, plus `flatten_close_needed` / `reverse_close_needed` for order-management flows. Follow this pattern when adding new broker data — never touch Qt widgets from the worker thread.
- `orderRef` has a strict format: `nano_{MMDD}_{HHMMSS}_{4-letter-tag}` (see `_generate_order_ref` / `_parse_orderref_time`). Order-status and blotter code parses submission time back out of this string, so do not change the format.
- IB returns `DBL_MAX` (≈1.7e308) as a "no data" sentinel for finite floats — `math.isnan`/`math.isinf` won't catch it. Use `_safe_pnl` (threshold `_IB_DBL_MAX = 1.7e308`) whenever you touch PnL/price fields from ib_async.
- Shutdown path: `MainWindow.closeEvent` calls `worker.stop()` on every thread then `wait(3000)` each. Anything new that needs graceful teardown must cooperate with this 3-second budget.

### Shared in-memory caches
- `src/core/reference_data.py` — `ReferenceData` singleton accessed via `get_instance()`. Caches contracts (SQLite-backed) and strategy tiers. Tier lookup falls back in this order: exact symbol → asset-class catch-all (`"FUT"` for futures, `"STK"` for stocks/options) → `None` (caller uses preferences). Emits `contracts_changed` / `strategy_tiers_changed` on reload.
- `src/core/trading_prefs.py` — `trading_prefs` module-level singleton for Futures/Stocks preferences. Thread-safe (internal `threading.Lock`) because IBWorkerThread coroutines read prefs while the UI thread writes them from `PreferencesDialog`.

### Persistence layout (split across JSON + two SQLite DBs)
- `app_config.json` at project root (gitignored) — connections, next client id, account aliases, hidden/unchecked accounts, layout (window geometry + splitter sizes), order-entry symbols, active symbol, and the `preferences` dict. Written atomically via tempfile + `os.replace`. Every save helper merges into existing data — never write the full file from scratch.
- `data/contracts.db` — the contract universe. `config_manager.load_contracts`/`save_contracts` delegate to `src/core/contracts_db.py`. On first load, any legacy `"contracts"` key still present in `app_config.json` is migrated into SQLite; after migration the JSON file is left intact but `_read_json` strips the `"contracts"` key on every read so it can never be re-persisted there.
- `data/strategy_tiers.db` — per-symbol strategy tier offsets/values (take_profit, stop_loss, stop, trailing). Read by `ReferenceData`; written by `StrategyEditorView`.

### Logging (`src/utils/logger.py`)
Three loggers, all rotating (5 MB × 3 backups) under `data/logs/`:
- Root logger → `nanopulse.log` at **INFO** (file + console).
- `nano_tws` → `nano_tws.log` at **INFO** — `pnlSingleEvent` / `accountSummaryEvent` fire thousands of times per minute and DEBUG saturates the rotating handler's lock.
- `nano_order` → `nano_order.log` at **INFO** — order audit trail.
- `ib_async` → piped into `nano_tws.log` at **INFO**.

Drop any of these to DEBUG temporarily when tracing wire-level / state issues.

Both dedicated loggers set `propagate = False` so their output stays out of the console. `ib_async.*` loggers are explicitly pinned to WARNING+ for the console but DEBUG still reaches the file. Import the dedicated loggers as `from src.utils.logger import tws_log, order_log`.

### UI composition notes
- The FlashTrade page (`src/ui/widgets/flash_trade_view.py`) wires the `OrderEntryPanel` + `OrderStatusPanel` + `AccountListPanel` + `AccountPositionPanel` around nested `QSplitter`s. Splitter sizes persist under three distinct keys (`flash_main_splitter`, `flash_right_v_splitter`, `flash_top_right_splitter`) via `config_manager.load_layout/save_layout`.
- `ContractsView.contracts_changed` is wired in `MainWindow._setup_ui` to `OrderEntryPanel._refresh_combo_items` — when adding new contract-dependent widgets, connect to that signal rather than polling `ReferenceData`.
- Status bar counts (connections / accounts / selected) update on a 1 s `QTimer` in `MainWindow._update_status`; "selected" = checked accounts in `AccountListPanel`.

## Conventions worth knowing
- `from __future__ import annotations` is used across the codebase — annotations are strings at runtime, so forward references in type hints are fine.
- `ib_async` is imported under a `try/except ImportError` guard in `ib_worker.py` so UI-only development works without TWS/ib_async installed.
- All times shown to the user are US/Eastern (`_ET = ZoneInfo("America/New_York")`). TWS timestamps are parsed via `_parse_ib_time` — they arrive in several separator styles, so reuse that helper rather than calling `strptime` directly.
- Do not store contracts back into `app_config.json`; contracts live in SQLite only (see `_read_json` in `config_manager.py`).
