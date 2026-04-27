# Hot Buttons — Implementation Spec

A panel of customizable one-click order buttons. Each button fires a fully-defined order (side, qty, type, anchor, offset, optional bracket attachments) against the **currently focused symbol**. Users add/edit/delete buttons via a modal.

Reference design: `NanoPulse Redesign.html` — search for `hb-grid`, `hbm-` (modal), `applyFeedState`, `evalButton`, `renderHotButtons`.

---

## 1. Scope

### In scope
- Grid of hot buttons (2-col), grouped by user-defined "groups" (e.g. `Live · Futures`, `Paper · Sandbox`, plus a special `__current__` group that follows the active account).
- "Add" tile + per-button edit pencil + edit-mode wiggle/delete.
- Modal to create/edit buttons.
- Live disabled/stale state driven by market-data feed health.
- Persistence of button definitions.

### Out of scope (this spec)
- Actual broker order routing (assume an `OrderService.submit(order_dict)` API exists).
- Group management UI (groups can be hardcoded or come from the existing account/group store).
- Themeing — match existing app theme.

---

## 2. Data Model

### `HotButton` dataclass

```python
@dataclass
class HotButton:
    id: int
    side: Literal['BUY', 'SELL']
    group: str                      # group name OR '__current__' sentinel
    sym: str                        # '' means "use focused symbol at fire time"
    qty: str                        # '' means "use default qty"; supports '100', '25%', '1'
    ord_type: Literal['LMT', 'MKT']
    anchor: Literal['BID', 'ASK', 'MID']   # ignored when ord_type == 'MKT'
    offset: int                     # ticks, ± from anchor; ignored when MKT
    label: str                      # '' = auto-generate from other fields
    tp: BracketLeg                  # take-profit attached order
    sl: BracketLeg                  # stop-loss attached order

@dataclass
class BracketLeg:
    on: bool
    v: float                        # numeric value
    u: Literal['ticks', 'pct', 'abs']  # 'abs' = absolute price
```

**No `tif` field** — time-in-force is fixed (DAY) at submit time. **No `LAST` anchor** — only BID / ASK / MID.

### Auto label

If `label == ''`, render: `{Side} {qty} @{anchor}{±offset}` for LMT, or `{Side} {qty} MKT` for MKT. Examples:
- `Buy 100 @Bid +2`
- `Sell 25% @Mid`
- `Buy 1 MKT`

---

## 3. Hot Button Grid

### Layout
- 2-column CSS grid (PySide: `QGridLayout` with 2 columns).
- Each cell: 44px tall, rounded 6px, full-color (green for BUY, red for SELL).
- Add tile: dashed border, plus icon, last cell in the section.

### Button content (top → bottom inside cell)
1. **Label row** — bold 11.5px, ellipsis on overflow.
2. **Meta row** — small 9px monospace tags:
   - `@{anchor} {±offset}t` (LMT only)
   - Bracket tag `TP {v}t · SL {v}t` if either leg is on (slightly darker pill)

### Edit pencil
- Top-right corner, 14×14, only visible on hover (or always in edit mode).
- Click → opens modal in edit mode for that button.

### States

| State | Trigger | Visual |
|---|---|---|
| Normal | feed live | Full color |
| Stale | feed quote age > N seconds AND button needs a quote | Greyed out (`var(--surface-3)`), `⚠ {reason}` warn pill replaces meta. **Disabled.** |
| Down | feed disconnected | Same as stale. MKT buttons also disabled (broker route impossible). |
| Editing | user toggled edit mode | All buttons wiggle (alternating delay). Red ✕ delete badge appears top-left. |
| Fresh-pulse | feed just recovered | Brief green box-shadow pulse for buttons that were stale and are now live. |

### Eligibility logic (port from JS `evalButton`)

```python
def can_fire(b: HotButton, feed_state: str, age_sec: int) -> tuple[bool, str]:
    if feed_state == 'live':
        return True, ''
    if b.ord_type == 'MKT':
        if feed_state == 'down':
            return False, 'Connection down'
        return True, ''                          # MKT survives stale quote
    # LMT path:
    if feed_state == 'down':
        return False, 'No quote — feed down'
    need = {'BID': 'bid', 'ASK': 'ask', 'MID': 'bid/ask'}[b.anchor]
    return False, f'{need} stale {age_sec}s'
```

### Banner above the grid
When feed is stale or down, show a thin pill banner above the grid:
- Pulsing dot, message text, age on the right.
- Examples: `Market data disconnected — limit-anchored buttons disabled`, `Quotes stale (12s) — review before firing`.

### Click handler
- If `editing` mode: pencil = edit, ✕ = delete, body click = no-op.
- Else if `not can_fire`: shake animation, no submit.
- Else: build the order dict and call `OrderService.submit(...)`. Brief visual confirmation (flash or toast).

---

## 4. Add / Edit Modal

A modal dialog (`QDialog` with custom layout). Title: `Add Hot Button` or `Edit Hot Button`.

### Layout (top → bottom)

#### Row 1: Side (segmented control, full width)
`[ Buy ] [ Sell ]` — green/red active fill.

#### Row 2: Group (dropdown)
- Special first option: `Current account` (maps to `__current__`).
- Then user's defined groups.

#### Row 3: Symbol + Qty (2-column row)
- **Symbol** — text input. Placeholder: `Use focused symbol`. Empty = follow focused symbol at fire time.
- **Qty** — text input. Accepts `100`, `25%`, `1`. Empty = use default qty.

#### Row 4: Order Type (segmented, **full row**)
`[ Limit ] [ Market ]`

> ⚠ This row used to also contain Time-in-Force. **Removed.** Order Type now spans the full width.

#### Row 5: Limit-only group — fades to 0.35 opacity when MKT selected (`pointer-events: none`)
- **Limit anchor** (segmented): `[ @ Bid ] [ @ Ask ] [ Mid ]`
  > ⚠ The `Last` option was **removed**. Only three anchors.
- **Offset** (numeric input, ± ticks). Hint label: `ticks · ± from anchor`.

#### Row 6: Button label (text, max 28 chars)
- Hint label says `auto` when blank; live preview updates as user types.

#### Attached orders block (bordered card)
Header: `Attached Orders — sent as bracket on fill`

Two rows, each: `[switch] [name + sublabel] [value input] [unit dropdown]`

| | Name | Sublabel | Default | Units |
|---|---|---|---|---|
| TP | Take Profit | `limit · opposite side · same qty` | 80 | `ticks`, `%`, `$ price` |
| SL | Stop Loss | `stop · opposite side · same qty` | 80 | same |

Switch off → row fades to 0.45 opacity, inputs disabled.

#### Live preview pane (right side or bottom)
- Renders an actual hot-button-styled tile reflecting current draft state.
- Below it, summary line: `BUY 100 NQM6 · LMT @ Bid + 0t · group Live · Futures`
  > ⚠ No `· DAY` segment in the summary anymore (TIF removed).
- If TP or SL is on, append: `· attached TP 80t · SL 80t`

#### Footer
`[ Delete ]` (left, only in edit mode) ........ `[ Cancel ] [ Save ]` (right)

### State machine

```python
class HotButtonModal(QDialog):
    def __init__(self, draft: HotButton | None = None, groups: list[str] = ...):
        # draft=None  -> "Add" mode, seeded from default_draft()
        # draft set   -> "Edit" mode, footer shows Delete
        ...

    def default_draft(self) -> HotButton:
        return HotButton(
            id=0, side='BUY', group='__current__', sym='', qty='',
            ord_type='LMT', anchor='BID', offset=0, label='',
            tp=BracketLeg(on=False, v=80, u='ticks'),
            sl=BracketLeg(on=False, v=80, u='ticks'),
        )
```

Every input mutates the in-memory draft and triggers `sync_preview()`. No save until user clicks Save.

---

## 5. Persistence

- Buttons live in user settings, e.g. `QSettings` under key `hot_buttons/v1`.
- Serialize the list of `HotButton` to JSON. Use `dataclasses.asdict` + `json.dumps`.
- Load on app start; if missing, seed with the 5 defaults below.

### Default seed buttons

```python
DEFAULT_BUTTONS = [
    HotButton(1, 'BUY',  '__current__',     '',     '',    'LMT', 'BID', 0,  '',           BracketLeg(True,  80, 'ticks'), BracketLeg(False, 80, 'ticks')),
    HotButton(2, 'SELL', '__current__',     '',     '',    'LMT', 'ASK', 0,  '',           BracketLeg(True,  80, 'ticks'), BracketLeg(False, 80, 'ticks')),
    HotButton(3, 'BUY',  'Live · Futures',  'NQM6', '1',   'MKT', 'BID', 0,  'NQ Slam +1', BracketLeg(True, 120, 'ticks'), BracketLeg(True,  60, 'ticks')),
    HotButton(4, 'SELL', 'Live · Equities', '',     '25%', 'LMT', 'MID', 1,  'Trim 25%',   BracketLeg(False, 80, 'ticks'), BracketLeg(False, 80, 'ticks')),
    HotButton(5, 'BUY',  'Paper · Sandbox', 'SPY',  '200', 'LMT', 'BID', -2, 'SPY Dip',    BracketLeg(True,  50, 'ticks'), BracketLeg(True,  25, 'ticks')),
]
```

---

## 6. Group filtering

The hot button panel renders only buttons matching the **currently selected group** in the Hot Buttons header dropdown. The `__current__` sentinel buttons render in **every** group (they're "follow the focused account" buttons).

```python
def visible_buttons(all_buttons, current_group):
    return [b for b in all_buttons if b.group == '__current__' or b.group == current_group]
```

---

## 7. Feed state integration

Subscribe to the existing market data service. Maintain:
- `feed_state: 'live' | 'stale' | 'down'`
- `age_sec: int` — seconds since last quote on the focused symbol.

Re-render the grid when these change. When transitioning `stale|down → live`, briefly add `fresh-pulse` to buttons that were previously stale (track them in a set before re-render).

If feed `down`, set the banner text to `Market data disconnected — limit-anchored buttons disabled`. If `stale`, set to `Quotes stale ({age}s) — review before firing`. Hide banner when `live`.

---

## 8. Order construction (on click)

```python
def build_order(b: HotButton, focused_symbol: str, focused_account: str,
                last_bid: float, last_ask: float, tick_size: float) -> dict:
    sym = b.sym or focused_symbol
    qty = resolve_qty(b.qty, focused_account)  # parse '25%' against position size, else int

    if b.ord_type == 'MKT':
        return {
            'symbol': sym, 'side': b.side, 'qty': qty, 'type': 'MKT',
            'tif': 'DAY',
            'attached': build_brackets(b),
        }

    base = {'BID': last_bid, 'ASK': last_ask, 'MID': (last_bid + last_ask) / 2}[b.anchor]
    limit_price = round_to_tick(base + b.offset * tick_size, tick_size)
    return {
        'symbol': sym, 'side': b.side, 'qty': qty, 'type': 'LMT',
        'limit': limit_price, 'tif': 'DAY',
        'attached': build_brackets(b),
    }
```

`tif` is hardcoded to `'DAY'` server-side — no UI control.

---

## 9. Acceptance Checklist

- [ ] Grid renders all visible buttons in 2 columns + Add tile.
- [ ] Each button shows label + meta tags (or warn tag when stale).
- [ ] Add tile opens modal in Add mode; pencil opens modal in Edit mode (footer shows Delete).
- [ ] Modal: Order Type row spans full width (no TIF field).
- [ ] Modal: Limit anchor has exactly 3 options — Bid, Ask, Mid (no Last).
- [ ] MKT order type fades the limit-anchor/offset row.
- [ ] Live preview tile updates as user types/toggles.
- [ ] TP/SL switches enable/disable their inputs.
- [ ] Save persists to QSettings; reload restores correctly.
- [ ] Delete (in edit mode) removes the button after confirmation.
- [ ] Feed-down banner shows; LMT buttons disable; MKT buttons disable too.
- [ ] Feed-stale banner shows; LMT buttons disable with `⚠ bid stale 12s`-style text; MKT buttons remain enabled.
- [ ] `live` → `stale` → `live` transition triggers fresh-pulse on previously-stale buttons.
- [ ] Edit mode: wiggle animation, ✕ delete badges visible.
- [ ] Click on disabled button shakes, does not fire an order.
- [ ] Submit calls `OrderService.submit()` with TIF=`DAY` always.

---

## 10. File layout suggestion

```
hot_buttons/
  __init__.py
  models.py          # HotButton, BracketLeg, defaults
  store.py           # QSettings persistence
  widget.py          # HotButtonsPanel (grid + banner + edit mode)
  modal.py           # HotButtonModal (Add/Edit dialog)
  preview.py         # Shared preview-tile rendering
  feed_eval.py       # can_fire(), label_for(), build_order()
  styles.qss         # Qt stylesheet block for hot button visuals
```

Keep `feed_eval.py` pure (no Qt imports) so it's unit-testable.
