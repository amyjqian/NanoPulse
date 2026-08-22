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
- `src/ui/main_window.py` is a plain `QWidget` (not `QMainWindow`, deliberately — avoids hidden menu/status/central chrome). It owns a `QStackedWidget` with pages: Connect, Trade (FlashTrade), StrategyEditor. The "Settings" tab opens `PreferencesDialog` as a modeless window instead of switching pages.
- `MainWindow` pre-warms `ReferenceData` (strategy tiers) before constructing child widgets — order matters because tier sections read the cache at construction. It also runs `instruments.migrate_active_config_from_db()` (idempotent legacy-DB enrichment) and owns the `InstrumentQualifier` (see below).

### IB connection model (`src/core/ib_worker.py`)
- `IBWorkerThread(QThread)` = one TWS connection. `MainWindow.active_threads: dict[str, IBWorkerThread]` is keyed by connection label.
- Each thread runs its own asyncio event loop with two concurrent coroutines: an **auto-reconnect loop** (`connectAsync` every 5 s, `reqAccountSummary` on re-connect) and a **throttler** (emits a de-duplicated batch every 250 ms).
- All data crossing into the UI crosses via Qt Signals on the worker: `summary_batch_updated`, `connection_status_changed`, `price_updated`, `bid_ask_updated`, `order_status_updated`, `portfolio_updated`, `account_pnl_updated`, `pnl_single_updated`, `accounts_detected`, plus `flatten_close_needed` / `reverse_close_needed` for order-management flows. Follow this pattern when adding new broker data — never touch Qt widgets from the worker thread.
- `orderRef` has a strict format: `nano_{YYMMDD}_{HHMMSS}_{4-letter-tag}` (see `_generate_order_ref` / `_parse_orderref_time`). Order-status and blotter code parses submission time back out of this string, so do not change the format. `_parse_orderref_time` also accepts the legacy `MMDD` date (dated to the current year) because those refs survive in open orders and in the fills snapshot IB replays after a reconnect. The Trades view groups fills into row-bands by the `{date}_{HHMMSS}` submission stamp plus instrument and side (`trades_model.order_group_key`), so one click across several accounts reads as one band.
- IB returns `DBL_MAX` (≈1.7e308) as a "no data" sentinel for finite floats — `math.isnan`/`math.isinf` won't catch it. Use `_safe_pnl` (threshold `_IB_DBL_MAX = 1.7e308`) whenever you touch PnL/price fields from ib_async.
- Shutdown path: `MainWindow.closeEvent` calls `worker.stop()` on every thread then `wait(3000)` each. Anything new that needs graceful teardown must cooperate with this 3-second budget.

### Authentication & user management (`src/auth/` + `src/ui/auth/`)
- `main.py` no longer builds `MainWindow` directly — an `AppController` shows the auth flow first and only constructs `MainWindow(current_user, auth_service)` after sign-in. On `MainWindow.sign_out_requested` it swaps back to a fresh login window (shows the new window *before* closing the old one so the event loop never sees zero windows).
- `src/auth/service.py` — `AuthService` facade over `data/users.db` (SQLite, gitignored — holds password hashes). Passwords hashed with PBKDF2-HMAC-SHA256 via `src/auth/hashing.py` (stdlib; `argon2`/`bcrypt` aren't installed — the stored format `pbkdf2_sha256$iters$salt$hash` is self-describing so the algo can be upgraded later). Failed-attempt/lockout state is **in-memory** keyed by lowercased username (cleared on restart). Exceptions: `InvalidCredentials` (carries `attempts_remaining`), `AccountLocked` (carries `seconds_remaining`), `AccountDisabled`.
- Two roles (`src/auth/models.py`): `admin` (manages users, **cannot trade**) and `trader` (trades, no user mgmt). The single admin is created only at first-run setup; there is no UI path to a second admin. New users are always traders, created with a generated temp password + `must_change_pw=1`.
- `src/ui/auth/auth_window.py` — `AuthWindow` hosts Setup/Login/Locked/ChangePw pages in a `QStackedWidget` and emits `authenticated(User)`. "Remember me" persists **only the username** to `QSettings("nanoPulse","nanoPulse")`.
- Role gating is enforced in two places: `MainWindow._apply_role_gating` hides the Nano/StrategyEditor tabs + Account pill for admins, and `FlashTradeView._on_order_requested` hard-blocks orders when `set_trading_allowed(False)`.
- User Management lives inside `PreferencesDialog`: pass `auth_context={"user","service","on_sign_out"}`; admins get a "Users" sidebar page (`src/ui/auth/users_page.py`) and everyone gets a user chip + Change-password + Sign-out footer. `PreferencesDialog._check_should_close` skips its focus-loss auto-close while any modal (`QApplication.activeModalWidget()`) is up.

### Backup & Restore (`src/backup/` + `src/ui/widgets/backup_page.py`)
- `src/backup/service.py` — `ConfigBackup` writes/reads a `.npbak` file, which is a **ZIP archive** (so it can carry the binary SQLite DBs): `manifest.json` + `config.json` (the signed-in user's `app_config-<username>.json`, which carries the chip/hot-button contract metadata) + `db/users.db` + `db/strategy_tiers.db`. Legacy archives may also carry `db/contracts.db`; restore still writes it and re-runs the instrument migration. Passwords travel only as the `pw_hash` already in `users.db` — never plaintext.
- `export()` writes atomically (`*.tmp` → `os.replace`). `restore()` validates → snapshots the current config+DBs to a temp dir → replaces files → **rolls back from the snapshot on any error**. After a successful restore the Settings dialog emits `reload_requested`, which the host treats exactly like a sign-out (returns to Login so the new accounts/config/DBs reload cleanly).
- **Automatic backups**: `maybe_auto_backup()` is called on login and hourly (`AppController._start_auto_backup` + a `QTimer`). It's a schedule *check* (catch-up-on-launch), backs up to `QStandardPaths.AppDataLocation/backups/` when the last auto-backup is ≥24 h old, and prunes to the last 5. The on/off flag + timestamps live in the user's config under `preferences.backup`.
- **UI** is a "Backup & Restore" page in `PreferencesDialog`, available to all signed-in users (both Create backup and Restore). The archived `config.json` carries everything in the per-user config — including **connections** (and `next_client_id`, aliases, layout, hot buttons, account groups). Manual backups go to a user-chosen path via `QFileDialog`.

### Shared in-memory caches
- `src/core/instruments.py` — the contract resolver. **There is no contract database**: each symbol chip (`order_entry_symbols` / `book_trader_symbols`), hot button, and cash-sweep symbol (`preferences.cash_management.sweep_contracts`) carries its own contract metadata inline in the per-user config (`conid`, `sec_type`, `exchange`, `expiry`, `local_symbol`, `multiplier`, `min_tick`, …), captured at add-time from the live symbol search. `instruments` is the single read path (Qt-free + lock/mtime-cached, so the worker thread may call it); `config_manager.upsert_instrument` is the single fan-out writer. Deleting a chip deletes its metadata — there is no cleanup step.
- `src/core/instrument_qualifier.py` — on every worker connect, entries missing a conid (or holding an expired FUT) are re-qualified via `request_browse` and healed through `upsert_instrument` (serialized browses; paused while the symbol-search popup is open).
- `src/core/reference_data.py` — `ReferenceData` singleton accessed via `get_instance()`. Caches strategy tiers only. Tier lookup falls back in this order: exact symbol → asset-class catch-all (`"FUT"` for futures, `"STK"` for stocks/options) → `None` (caller uses preferences). Emits `strategy_tiers_changed` on reload.
- `src/core/trading_prefs.py` — `trading_prefs` module-level singleton for Futures/Stocks preferences. Thread-safe (internal `threading.Lock`) because IBWorkerThread coroutines read prefs while the UI thread writes them from `PreferencesDialog`.

### Persistence layout (split across JSON + two SQLite DBs)
- `app_config.json` at project root (gitignored) — connections, next client id, account aliases, hidden/unchecked accounts, layout (window geometry + splitter sizes), order-entry symbols, active symbol, and the `preferences` dict. Written atomically via tempfile + `os.replace`. Every save helper merges into existing data — never write the full file from scratch.
- `data/contracts.db` — **legacy, read-only**. Kept on disk solely so `instruments.migrate_active_config_from_db()` can enrich bare (string) chip entries from it on first login per user. Never written; new installs work without it.
- `data/strategy_tiers.db` — per-symbol strategy tier offsets/values (take_profit, stop_loss, stop, trailing). Read by `ReferenceData`; written by `StrategyEditorView`.

### Logging (`src/utils/logger.py`)
Three loggers, all rotating (5 MB × 3 backups) under `data/logs/`:
- Root logger → `nanopulse.log` at **INFO** (file + console).
- `nano_tws` → `nano_tws.log` at **INFO** — `pnlSingleEvent` / `accountSummaryEvent` fire thousands of times per minute and DEBUG saturates the rotating handler's lock.
- `nano_order` → `nano_order.log` at **INFO** — order audit trail. Every line is
  stamped with the signed-in NanoPulse username by a handler-side filter
  (`set_log_user`, called from `AppController` on sign-in/sign-out), so no
  `order_log` call site passes it. The stamp names the *session* that wrote the
  line: for the app's own actions that is who triggered them, but `ORDER-SNAPSHOT`
  / `ORDER-STATUS` lines are TWS replays that include other clients' orders — the
  order id settles origin (`nano_`-prefixed ref = ours, `{account}_p{permId}` = not).
- `ib_async` → piped into `nano_tws.log` at **INFO**.

Drop any of these to DEBUG temporarily when tracing wire-level / state issues.

Both dedicated loggers set `propagate = False` so their output stays out of the console. `ib_async.*` loggers are explicitly pinned to WARNING+ for the console but DEBUG still reaches the file. Import the dedicated loggers as `from src.utils.logger import tws_log, order_log`.

### Connection loss hides its accounts (`src/ui/widgets/offline_connections.py`)
- While a connection is down, nothing refreshes the accounts it serves, so **its accounts are hidden from all four views** — Positions, Positions Summary, Trades, Trades Summary. Everything left on screen is being actively refreshed.
- `FlashTradeView._on_connection_status_for_visibility` tracks the down labels and fans the set out via `AccountPositionPanel.set_offline(labels, accounts)` and `TradesPanel.set_offline_labels(labels)`. Every position dict and fill dict already carries `label` (`_do_portfolio_refresh` / `_flatten_fill`), so the views filter on it directly.
- Account **header bands** render from `_acct_data`, which carries no connection label, so `AccountPositionModel` cannot filter them the way it filters positions. `rebuild()` subtracts `hidden_accounts()` — the caller's explicit list UNION every account whose positions came from an offline connection — so a band never survives with frozen Net/Excess/BP and no rows beneath it. Do not narrow that back to `_offline_accounts` alone.
- The `accounts` argument exists for the flat Positions view only: an account **band** renders for any in-scope account with summary data even when it holds nothing, so filtering positions alone would leave a frozen Net/Excess/BP header behind. The label→accounts map is captured from `accounts_detected` and held on `FlashTradeView` — an explicit Disconnect calls `AccountsTableModel.remove_rows_by_label`, destroying that mapping exactly when it is needed. That map is empty at startup though (`accounts_detected` fires on connect, before this view wires to the worker), so `_publish_offline_scope` also unions in `AccountsTableModel.known_accounts()`, the session registry that survives a disconnect.
- **Nothing is deleted — the views filter.** Position caches and `TradesSummaryModel._fills` keep every entry, so a reconnect restores each view exactly. This matters most for fills: IB replays only a limited window, so a discarded fill is unrecoverable.
- **Known cost, accepted deliberately:** a hidden position still exists at the broker, and a shorter Positions tab can read as "I am flat". Every hide therefore emits a `transient_status_message` naming the connection and the account count (`hidden_accounts_notice`). Do not remove that notice.
- Reconnect needs no special handling beyond clearing the flag: `_do_portfolio_refresh` rebuilds from `ib.positions()` and emits zero-qty entries for cached positions that no longer appear, so a position closed elsewhere during the outage collapses to flat on its own.

### Accounts panel on disconnect (`src/ui/widgets/account_list_panel.py`)
- The Accounts panel is the **selector**, so unlike Positions/Trades it does NOT hide a disconnected connection's accounts — removing cards would break group membership and hide who an order fan-out is about to skip. They stay, dimmed, `⚠`-prefixed, with balance and cash blanked to `–` (a balance nothing refreshes reads exactly like a live one).
- `AccountsTableModel.remove_rows_by_label` must drop `_acct_to_label` entries and `_connected_labels[label]` **before** emitting its row removals. `endRemoveRows` runs receivers synchronously and `AccountListPanel._on_rows_changed` re-resolves every row through `connection_for_account` / `is_label_connected`; with the reverse index rebuilt only afterwards, the panel read removed accounts as still live and undid the offline styling `_sync_rows` had just applied. Symptom: cards kept their balances until the user switched account groups and back.
- `is_label_connected` deliberately defaults to **True** for unknown labels (so a fresh connection isn't dimmed before its first signal). `_is_account_connected` therefore checks `connection_for_account(...) is None` FIRST — that None is what makes a removed account read as offline.
- Offline members are rendered on **every** view: a user group diffs its own membership list (`group_members - model_accounts`), while All / None / no-group diff `AccountsTableModel.known_accounts()` — every account seen this session, which survives a disconnect. Both halves honour `_hidden` so an ×-hidden account doesn't reappear by going offline.
- **Disconnect vs delete**: `remove_rows_by_label` drops the rows but KEEPS `_known_accounts`, so the accounts stay listed-but-offline. `forget_label(label)` is deletion — it purges `_known_accounts` first, then removes rows, then emits `known_accounts_changed` (needed because `remove_rows_by_label` emits nothing when the rows are already gone, which is the usual disconnect-then-delete order). `ConnectionsView._on_delete_row` calls it; nothing else should.

### Trades date filter (`src/ui/widgets/trades_date_filter.py`)
- Every entry in the Trades control-bar date dropdown resolves to one `TradeWindow` — a half-open ET interval `[start, end)`, either bound `None` for unbounded. Session presets (Evening 18:00 / Noon 12:00 / Morning 09:30) and Today set only `start`; a named day ("Thu, Aug 20") sets both, so it means that day alone; Last N Days and Week to Date set `start`.
- **Windows count CALENDAR days — weekends included.** The old `trading_days_cutoff` (Mon–Fri) is gone: "Last 2 Days" on a Monday is Sunday + Monday, and weekend dates appear in the named-day list (the Sunday futures session is real trading). The one surviving weekend skip is `trades_summary_model.session_day`, which rolls a Friday-evening futures print to Monday — that picks a FIFO book, not a filter frame.
- One predicate, three consumers: `TradesFilterProxy.filterAcceptsRow`, `TradesSummaryModel._passes_time_filter` and `pnl_totals` all call `TradeWindow.contains_fill`. Don't reintroduce a private copy — they used to have one each and drifted.
- A datetime typed into the search box becomes its own window and replaces the combo's, except when the combo holds a session preset. That precedence lives in `effectiveWindow` / `effective_window` only.
- Named-day entries are literal dates, so they go stale at ET midnight. `TradesFilterProxy.anchorDateChanged` → `TradesPanel._on_anchor_changed` rebuilds the combo and re-resolves the selection; a day that rolled off the list falls back to Today.

### UI composition notes
- The FlashTrade page (`src/ui/widgets/flash_trade_view.py`) wires the `OrderEntryPanel` + `OrderStatusPanel` + `AccountListPanel` + `AccountPositionPanel` around nested `QSplitter`s. Splitter sizes persist under three distinct keys (`flash_main_splitter`, `flash_right_v_splitter`, `flash_top_right_splitter`) via `config_manager.load_layout/save_layout`.
- New instruments are added ONLY through the symbol-search popup (`symbol_search_popup.py`, the "+" buttons / "/" shortcut); a pick returns qualified contract metadata that the host stores on the new chip entry via `_add_symbol(sym, meta)`.
- Status bar counts (connections / accounts / selected) update on a 1 s `QTimer` in `MainWindow._update_status`; "selected" = checked accounts in `AccountListPanel`.

## Conventions worth knowing
- `from __future__ import annotations` is used across the codebase — annotations are strings at runtime, so forward references in type hints are fine.
- `ib_async` is imported under a `try/except ImportError` guard in `ib_worker.py` so UI-only development works without TWS/ib_async installed.
- All times shown to the user are US/Eastern (`_ET = ZoneInfo("America/New_York")`). TWS timestamps are parsed via `_parse_ib_time` — they arrive in several separator styles, so reuse that helper rather than calling `strptime` directly.
- Chip-list entries in the config may be bare strings (legacy) or dicts with contract metadata — always read/write them through `config_manager`'s entry helpers (`load_*_entries`, `save_*_symbols` merge shims, `upsert_instrument`), never raw JSON, or metadata will be silently dropped.
