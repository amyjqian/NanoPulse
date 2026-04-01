# Plan: Refactor Order Tracking to Use `orderRef` as Canonical Key

## Background

This is the exact architectural standard used by institutional trading firms. In professional trading infrastructure (like the FIX Protocol), this concept is called the **Client Order ID (ClOrdID)**. By using our custom `orderRef` as the ultimate source of truth, we decouple NanoPulse from the quirks and delays of Interactive Brokers' internal database.

### Why This Architecture Wins

1. **Zero-Latency State Tracking**: The millisecond you click "Buy," NanoPulse generates the `orderRef` (e.g., `nano_0331_093000_abcd`). The UI can instantly populate the Order Status table and show "Pending" without waiting for a round-trip network ping to get an ID from TWS.

2. **Immunity to TWS Restarts**: Every time you restart TWS, it resets its internal `orderId` counter back to 1. If you rely on `orderId` to link things, your app will break across reboots. The `orderRef` is universally unique and immune to TWS resets.

3. **Clean Isolation**: TWS broadcasts events for all activity on your account, including manual trades. By filtering events through `orderRef`, NanoPulse cleanly ignores manual trades and only updates data for the orders it actually owns.

### Current System (Before Refactor)

- Orders keyed by `{label}_{orderId}` initially, then **migrated** to `{label}_p{permId}` once TWS assigns a permanent ID.
- A `supersedes` mechanism tells the UI to remove the stale `orderId`-based row when the `permId`-based row arrives.
- A mutable `key_ref = [order_id]` closure container tracks the current key and swaps it on migration.
- This is complex, fragile, and introduces a race window where the UI shows duplicate rows.

### New System (After Refactor)

- **Internal (NanoPulse) orders**: keyed by `{label}_{orderRef}` — known instantly at order creation, never changes, no migration needed.
- **External orders** (TWS manual / other clients): still keyed by `{label}_p{permId}` (or `{label}_{orderId}` as fallback).
- The `supersedes` mechanism is retained only for external order edge cases but will never fire for NanoPulse orders.

---

## Files to Change

### 1. `src/core/ib_worker.py`

#### A. Refactor `_build_trade_dict()` key logic (~line 938–971)

**Current** (lines 963–971):
```python
# ── Unique key ────────────────────────────────────────────────
# Prefer permId (globally unique, survives restarts).
# Fall back to orderId for orders that haven't received a
# permId yet (e.g. PendingSubmit).
perm_id = getattr(order, "permId", 0) or getattr(trade.orderStatus, "permId", 0)
if perm_id:
    order_id = f"{self._label}_p{perm_id}"
else:
    order_id = f"{self._label}_{order.orderId}"
```

**New**:
```python
# ── Unique key ────────────────────────────────────────────────
order_ref = getattr(order, "orderRef", "") or ""
perm_id = getattr(order, "permId", 0) or getattr(trade.orderStatus, "permId", 0)

if order_ref.startswith("nano_"):
    # Internal NanoPulse order — orderRef is the immutable key.
    # No orderId → permId migration ever needed.
    order_id = f"{self._label}_{order_ref}"
elif perm_id:
    order_id = f"{self._label}_p{perm_id}"
else:
    order_id = f"{self._label}_{order.orderId}"
```

Also update the docstring to describe the new key strategy.

#### B. Simplify `_do_place_order()` closure (~lines 596–644)

**Current** (lines 600–644):
```python
# Use _build_trade_dict for consistent order_id generation.
# At this point permId may still be 0, so it falls back to orderId.
d = self._build_trade_dict(trade)
if d:
    order_id = d["order_id"]
else:
    order_id = f"{self._label}_{trade.order.orderId}"

self._trades[order_id] = trade

log.info("Worker %s: placed %s %s %d %s for account %s → %s",
         self._label, action, order_type, qty,
         params.get("symbol", "?"), account, order_id)

# ── Emit initial status to UI ─────────────────────────────────
status = trade.orderStatus.status if trade.orderStatus.status else "Submitted"
self._emit_order_status(trade, order_id, params, status)

# ── Bind trade.statusEvent for live updates ───────────────────
frozen_params = dict(params)
# Mutable container so the closure can update the tracked key
# when migrating from orderId → permId.
key_ref = [order_id]

def _on_status(t, _kr=key_ref, _p=frozen_params):
    # Once TWS assigns a permId, re-register under the canonical key
    new_d = self._build_trade_dict(t)
    if new_d:
        canonical = new_d["order_id"]
        old_key = None
        if canonical != _kr[0]:
            # Migrate from orderId-based key to permId-based key
            old_key = _kr[0]
            self._trades.pop(old_key, None)
            self._trades[canonical] = t
            _kr[0] = canonical
        self._emit_order_status(t, canonical, _p,
                                t.orderStatus.status,
                                supersedes=old_key)
    else:
        self._emit_order_status(t, _kr[0], _p, t.orderStatus.status)

trade.statusEvent += _on_status
```

**New**:
```python
# orderRef is already set on the order object — use it as the
# canonical, immutable key.  No migration will ever be needed.
order_id = f"{self._label}_{order.orderRef}"
self._trades[order_id] = trade

log.info("Worker %s: placed %s %s %d %s for account %s → %s",
         self._label, action, order_type, qty,
         params.get("symbol", "?"), account, order_id)

# ── Emit initial status to UI ─────────────────────────────────
status = trade.orderStatus.status if trade.orderStatus.status else "Submitted"
self._emit_order_status(trade, order_id, params, status)

# ── Bind trade.statusEvent for live updates ───────────────────
# The key never changes, so the closure is trivially simple.
frozen_params = dict(params)
frozen_oid = order_id

def _on_status(t, _oid=frozen_oid, _p=frozen_params):
    self._emit_order_status(t, _oid, _p, t.orderStatus.status)

trade.statusEvent += _on_status
```

**Key improvements**:
- `order_id` is computed from `order.orderRef` which was set two lines above — always available, never 0.
- No `key_ref` mutable container.
- No `_build_trade_dict()` call (which could return `None`).
- No `supersedes` parameter.
- No `self._trades.pop()` / re-register dance.
- Closure is a trivial one-liner.

#### C. `_on_global_order_status()` (~line 1078) — No code change needed

This method calls `_build_trade_dict(trade)` which now returns `{label}_{orderRef}` for internal orders. Since we stored the trade under this exact key in `_trades`, the check `if order_id in self._trades: return` correctly skips internal orders (the per-trade closure handles them). External orders continue to flow through and get emitted.

#### D. `_emit_order_status()` — Keep `supersedes` parameter

The `supersedes` parameter remains in the signature for external order edge cases but will never be set for NanoPulse orders. No code change needed.

#### E. `_snapshot_existing_orders()` (~line 1100) — No code change needed

This calls `_build_trade_dict()` which now correctly keys internal orders by `orderRef`. Snapshots after restart will produce the same stable key.

### 2. `src/ui/widgets/flash_trade_view.py`

#### A. Wire `cancel_all_requested` signal (~line 155)

Add after the `order_requested` connection:

```python
# ── Wire order entry → order placement ───────────────────────────────
self._order_entry.order_requested.connect(self._on_order_requested)
self._order_entry.cancel_all_requested.connect(self._on_cancel_all_for_symbol)
```

#### B. Add `_on_cancel_all_for_symbol` handler (after `_on_cancel_requested`, ~line 495)

```python
def _on_cancel_all_for_symbol(self, symbol: str) -> None:
    """Cancel button clicked in OrderEntryPanel → cancel all open orders
    for *symbol* across every connected worker."""
    count = 0
    for label, worker in self._active_threads.items():
        if worker.isRunning() and worker.is_ib_connected:
            self._ensure_worker_connected(worker)
            worker.cancel_all_for_symbol(symbol)
            count += 1
    log.info("FlashTrade: cancel_all_for_symbol '%s' → sent to %d worker(s)",
             symbol, count)
```

### 3. `src/ui/widgets/order_status_panel.py` — No changes needed

- Already treats `order_id` as an opaque string key.
- Already uses `order_ref` for internal/external visual distinction.
- Cancel routing in `flash_trade_view._on_cancel_requested()` still works: `split("_", 1)` on `"L-paper-ib_nano_0331_093000_abcd"` correctly yields label `"L-paper-ib"` (labels use hyphens, not underscores).

### 4. `src/ui/widgets/order_entry_panel.py` — No changes needed

- Already has `cancel_all_requested = Signal(str)` and `_on_cancel_all()` handler.
- Already emits `order_requested` with the payload dict consumed by `flash_trade_view`.

---

## Verification Checklist

After implementation, verify:

1. **Place an order via NanoPulse** — log should show `order_id = L-paper-ib_nano_0331_...` (not `_p{permId}` or `_{orderId}`).
2. **Order status updates** — no `supersedes` key in the emitted dict, no duplicate rows in the UI.
3. **Cancel a single order** via the Order Status panel Cancel button — routing extracts the correct label.
4. **Cancel all for symbol** via the Order Entry Cancel button — all open orders for that symbol are cancelled.
5. **Place an order in TWS manually** — should appear in NanoPulse with `{label}_p{permId}` key and muted external background.
6. **Restart the app** — snapshot should restore NanoPulse orders under the same `{label}_{orderRef}` keys (no duplicates).
7. **Disconnect/reconnect** — re-snapshot should not create duplicate rows.

---

## Summary

| Aspect | Before | After |
|--------|--------|-------|
| Internal order key | `{label}_{orderId}` → migrates to `{label}_p{permId}` | `{label}_{orderRef}` — immutable from birth |
| Key available at | After `placeOrder()` returns (orderId), or after TWS assigns permId | Immediately at `_generate_order_ref()` — before `placeOrder()` |
| Migration mechanism | `supersedes` + `key_ref` mutable closure | None needed |
| TWS restart impact | orderId resets → stale keys | orderRef persists → zero impact |
| Closure complexity | 15 lines with mutable container | 2 lines, trivial |
| External order key | `{label}_p{permId}` | `{label}_p{permId}` (unchanged) |

