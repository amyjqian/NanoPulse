# NanoPulse — Ladder sizing: percent of account value

Implementation spec for Claude Code. Adds a **unit toggle** to the ladder panel's existing size-preset
row so the trader can size an order either in **shares** or as a **percent of account value**.

- **Repo:** `/Users/qian/PycharmProjects/NanoPulse`
- **Visual reference:** `NanoPulse Ladder Sizing.dc.html` → option **1a** (the toggle is live; click `SH` / `%`)
- **Affected widget:** the ladder panel's size row — currently `10 · 25 · 50 · 100 · 500 · …`

> **Styling rule — read before writing UI code.** Do **not** copy hex values from this document as-is.
> First read the app's existing style source (`*.qss`, any theme/StyleSheet/constants module) and the
> ladder panel itself, then reuse those tokens and widget classes. The colors in §6 are the mockup's
> values, given only to identify *which existing role* each element maps to. The share-mode row must
> look **byte-identical to today's row** — this change adds one button and one alternate state, it does
> not restyle the ladder.

---

## 1. What changes

Today the row is five absolute share-count presets plus a custom spinner. This adds a second **unit**:
percent of account value. It is a unit change, not five more presets.

| | Share mode (default) | Percent mode |
|---|---|---|
| Toggle button | `SH`, slate fill | `%`, gold fill |
| Presets | `10 · 25 · 50 · 100 · 500` | `10% · 25% · 50% · 75% · 100%` |
| Chip color | existing slate/blue-selected | gold-on-dark / solid gold selected |
| Spinner step | 10 shares | 1 %, capped at 100 |

**Why the whole row recolors:** `25` and `25%` are three characters apart and ~78× apart in size
(25 shares vs 1,946 shares at NVDA 182.40 on a $1.42M account). The gold treatment on every chip —
not the small toggle button — is what prevents a mis-read. Do not implement the toggle without it.

---

## 2. Layout

```
┌────┬──────┬──────┬──────┬──────┬──────┬─────┐
│ SH │  10  │  25  │  50  │ 100  │ 500  │ … ▲▼│      share mode
└────┴──────┴──────┴──────┴──────┴──────┴─────┘
┌────┬──────┬──────┬──────┬──────┬──────┬─────┐
│ %  │ 10%  │ 25%  │ 50%  │ 75%  │ 100% │ … ▲▼│      percent mode (gold)
└────┴──────┴──────┴──────┴──────┴──────┴─────┘
  38px   five equal-width chips, gap 5px     34px
```

- The toggle is **38px wide, same height as the chips (30px)**, first item in the row.
- Chips keep equal widths via the existing stretch behavior — the row geometry does not change
  between modes, only labels and colors.
- The `…` spinner keeps its current position and width at the end of the row.
- Net cost vs today: **~43px of row width** (38px button + one gap).

---

## 3. Behavior

### 3.1 Toggle
- **Default `SH`** on every app start. Do not persist percent mode across sessions — a trader
  returning to the panel must find it in the unit they expect.
- Single button, **click cycles** `SH → % → SH`.
- The label shows the unit you are **in**, not the one you'd switch to. (Verified as unambiguous
  only because the chips recolor; that redundancy is load-bearing.)
- Tooltip states the mode and the action: *"Sizing in shares — click for percent of account value"*
  / *"Sizing by percent of account value — click for shares"*.
- Do **not** bind this to a bare keyboard shortcut. If you want one, require a modifier — an
  accidental keypress silently rescales every subsequent click on a live ladder.

### 3.2 Presets
- Share presets: whatever the panel uses today (`10 25 50 100 500`), unchanged, user-configurable
  if they already are.
- Percent presets: `10 · 25 · 50 · 75 · 100`.
- Selection is **per unit** — remember the selected share chip and the selected percent chip
  independently.

### 3.3 Custom spinner
- Steps **10** in share mode, **1** in percent mode. Percent is **clamped to 0–100**; shares clamp
  to the existing max.
- Holds a **separate value per unit.** Type `750` in shares, toggle to `%`, toggle back → `750` is
  still there. Carrying one number across the toggle would turn 750 shares into 750%.
- Typing clears the chip selection and outlines the field in the active unit's accent color.
- Integers only; reject non-digits at the validator, don't silently coerce.

---

## 4. Resolution math

```python
from decimal import Decimal, ROUND_DOWN

def shares_from_percent(pct: Decimal, account_value: Decimal, price: Decimal) -> int:
    if price <= 0:
        return 0
    raw = (account_value * pct / Decimal(100)) / price
    return int(raw.to_integral_value(rounding=ROUND_DOWN))
```

- **Define the base explicitly.** "% of account value" is ambiguous on a margin account. Use
  **net liquidation value** and say so in the tooltip. Do not use buying power as the base — 100%
  of BP on a 4× margin account is a 4× leveraged position behind a single click.
- **Price source:** last trade for the selected symbol. If the symbol has no live price yet, the
  percent chips must disable rather than resolve against a stale or zero price.
- **Floor to whole shares.** Never round up.
- **Clamp to buying power** after resolving, and clamp to the instrument's lot size for futures.
- Resolve to 0 → **disable the chip** (grey, non-clickable). A rung click must never fire a 0-qty order.
- Recompute on: symbol change, price tick, account switch, and account-value update.

### Futures
Percent-of-account sizing is meaningless for MNQ/MES/NQ/ES/MCL — those size in contracts against
margin, not notional. Either **hide the toggle** for futures symbols or resolve against initial
margin per contract. Hiding is the safer default; pick one deliberately and comment why.

---

## 5. The safety gap you must close

The mockup's readout line was removed at the user's request, so **in percent mode there is no
resolved share count on screen**. The ladder fires on the rung click, so without it the trader
commits size they cannot see. Put the resolved quantity somewhere already in the panel — do not
add a confirmation dialog (it defeats the point of a ladder):

- **Preferred:** the `Bid Size` / `Ask Size` header row, or wherever the panel already shows working
  quantity — show `1,946 sh` while percent mode is active.
- **Alternative:** on the chip itself, two-line (`25%` over `1,946 sh`) — see option **1c** in the
  reference mockup. Costs ~8px of row height and re-renders on every tick.

Whichever you choose, the number must be visible **before** the rung click, not after.

---

## 6. Element → existing-style mapping

| Element | Use the same style as | Mockup value |
|---|---|---|
| Row container | existing size-row container | `#131a26` on `#26303f` border |
| `SH` toggle | existing unselected preset chip, one step lighter | bg `#3a4250`, border `#4a5464`, text `#e8ecf2` |
| `%` toggle (active) | new accent — reuse a warning/attention token if one exists | bg `#c9a227`, border `#e0b93a`, text `#1a1608` |
| Share chip, unselected | existing preset chip | `#2b3444` / `#c8d0dc` |
| Share chip, selected | existing selected-preset blue | `#1f7ae0` / `#fff` |
| Percent chip, unselected | dark gold — must not read as a share chip | `#2b2a1e`, border `#4a4126`, text `#d9c07a` |
| Percent chip, selected | solid gold | `#c9a227` / `#1a1608` |
| `…` spinner | existing spinner, unchanged | `#1b2330`, border `#2b3444` |
| Spinner active outline | active unit's accent | `#1f7ae0` (SH) / `#c9a227` (%) |

All chip text is **tabular figures**, same size as today (~12.5px). Any sub-label must be ≥11px and
clear 4.5:1 contrast — an unreadable size caption is worse than none.

---

## 7. Suggested implementation

```
app/ui/ladder/
  size_row.py        # SizeRow(QWidget): toggle + preset chips + spinner, emits size_changed(int shares)
  size_model.py      # unit state, per-unit selection + custom value, percent→shares resolution
```

- `SizeRow` exposes **one signal carrying resolved shares** — `size_changed(int)`. The ladder should
  never learn about percent; it receives a share count exactly as it does today. This keeps the
  change contained to one widget.
- Keep the resolution in `size_model.py` (pure, `Decimal`, unit-testable) — no math in the widget.
- Chip styling by dynamic property + `style().polish()` (`unit="sh"|"pct"`, `selected=true|false`)
  rather than per-widget stylesheets, so it stays in the app's `.qss`.

---

## 8. Acceptance checklist

- [ ] Row opens in `SH` mode and is pixel-identical to today's row apart from the new 38px button.
- [ ] Clicking the toggle cycles `SH ↔ %`; labels become `10% 25% 50% 75% 100%` and every chip recolors gold.
- [ ] Percent mode is not persisted across restarts.
- [ ] Share and percent selections are remembered independently.
- [ ] Spinner steps 10 / 1, clamps percent to 100, and keeps a separate value per unit.
- [ ] `25%` on a $1,420,000 account at 182.40 resolves to **1,946 shares** (floor, not 1,947).
- [ ] Resolved quantity is visible on screen before any rung click (§5).
- [ ] Resolution floors to whole shares, clamps to buying power, and disables chips that resolve to 0.
- [ ] Percent chips disable when no live price is available.
- [ ] Futures symbols: toggle hidden (or margin-based resolution, documented).
- [ ] Recomputes on symbol change, price tick and account switch.
- [ ] `SizeRow` emits only resolved share counts; the ladder code is unchanged.
- [ ] Unit tests on `size_model`: floor behavior, 0-price, BP clamp, per-unit custom values.
