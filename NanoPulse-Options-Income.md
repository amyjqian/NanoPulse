# NanoPulse — Options Income (Covered Calls & Cash-Secured Puts)

Implementation spec for Claude Code. Adds an **Options income** ticket to the existing PySide6
desktop trading app.

- **Repo:** `/Users/qian/PycharmProjects/NanoPulse`
- **Visual reference:** `NanoPulse Login.dc.html` → switch to the **Options income** view
  (bottom "VIEW" bar). Entry point shown in the **Positions** view.
- **Entry-point comparison:** `Options Entry Points.dc.html` (options 1a / 1b / 1c)

> **Styling rule — read before writing UI code.** Do **not** copy hex values from this document
> as-is. First read the app's existing style source (`*.qss`, any `StyleSheet`/theme/constants
> module, and the existing Positions/Orders panels) and reuse those tokens, widget subclasses and
> button classes. The colors listed in §9 are the mockup's values, provided only to identify
> *which* existing role each element maps to (e.g. "same orange as Flatten"). Match the existing
> app's font, row height, padding, border-radius and density — the ticket must look native next
> to the Positions panel, not like a new design.

---

## 1. What this feature does

Lets a trader sell premium against collateral they already hold:

| Strategy | Collateral | Requirement |
|---|---|---|
| **Covered Call** (CC) | Shares held | ≥ 100 shares of the underlying, per contract |
| **Cash-Secured Put** (CSP) | Buying power | BP ≥ strike × 100, per contract |

Both are **SELL to open**, single-leg, limit orders. No naked positions are possible: quantity is
hard-capped by available collateral.

---

## 2. Entry point

The ticket is the permanent **Options** tab beside **Positions** and **Trades** on the Trade
page (`PositionsTradesTabs`). There is no popup and no context-menu entry any more.

**Accounts:** the ticket has no account of its own. It follows the Account List selection
(All / a user group such as gamma, core, beta / individual cards) exactly like Buy/Sell in the
order panel:

- a submit sends the **same qty to every checked + connected account**, one payload per account
  (`_target_accounts=[account]`), each with its own `orderRef`;
- **MAX** is the smallest per-account capacity, so no account is over-sized; the worker's whatIf
  pre-check still gates each order;
- the Submit button is disabled with the reason when any checked account is ineligible, when no
  account is checked, or when the "Orders on all accounts" guard (Settings → Trading) applies;
- submitted orders appear in the Orders panel above the tabs, one row per account, with status
  and Cancel there — the ticket keeps no order list of its own.

The header is the Book Trader's ticker strip (symbol · last · bid · ask · change). No share count
is shown — MAX already reflects each account's holdings. Chips show the bare ticker.

**Market data** (chain, strikes, quotes, symbol search) streams on the highest-priority *connected*
TWS connection — the same one the order panel's price feed uses — never on a checked account's
connection. Orders still go to each account's own connection.

Implementation notes:
- `QTableView`/`QTableWidget` → `setContextMenuPolicy(Qt.CustomContextMenu)` +
  `customContextMenuRequested` → build a `QMenu`, `exec(globalPos)`.
- The menu must be **clamped to the screen** (QMenu does this natively).
- Only **one** options entry in the menu — strategy (CC vs CSP) is chosen *inside* the ticket, not
  in the menu.
- Opening from an account row pre-fills that **account**. Opening from a stock row (optional
  future addition) should also pre-fill the **symbol**.

Open the ticket as a modal `QDialog` (recommended — it's a transactional order ticket) or a
dockable panel if the app already uses docks for order entry. Follow whatever the existing
order-entry panels do.

---

## 3. Layout

Five stacked regions, left column + right rail (see reference):

```
┌───────────────────────────────────────────────────────────────────────────┐
│ NVDA  182.40  +1.85 (+1.01%) · 300 shares @ 164.20 · mgn-jack-02 [MARGIN] ✕│  header
├───────────────────────────────────────────────────────────────────────────┤
│ [QQQ 200 sh][SPY 100 sh][NVDA 300 sh][TSLA no shares][NBIS 400 sh]  [🔍 ] │  symbols
├───────────────────────────────────────────────────────────────────────────┤
│ ┌ Sell Covered Call ────── ELIGIBLE ┐ ┌ Sell Cash-Secured Put ─ ELIGIBLE ┐│  strategy
│ │ Collateral: 300 shares · covers 3 │ │ Collateral: BP 2,523,356         ││
├───────────────────────────────────────────────────────────────────────────┤
│ Expiry [Jul 31 3d][Aug 7 10d][Aug 14 17d][Aug 21 24d][More ▾]             │  expiry
├──────────────────────────────────────┬────────────────────────────────────┤
│ Strike  Delta  Credit  Yield  Ann.   │ ORDER                              │
│ 185.00 OTM  .42  $440  2.41%  46.3%  │ SELL 1 NVDA Aug 14 190.00 CALL     │
│ 187.50 OTM  .36  $360  1.97%  37.9%  │        @ 2.90 LMT                  │
│ 190.00 OTM  .28  $290  1.59%  30.5%◀ │ Contracts   [−] 1 [+]  [MAX 3]     │
│ 192.50 OTM  .22  $225  1.23%  23.7%  │ Limit price [−] 2.90 [+]           │
│ 195.00 OTM  .17  $174  0.95%  18.3%  │ [Bid 2.85][Mid 2.90][Ask 2.95]     │
│ 200.00 OTM  .10   $99  0.54%  10.4%  │ ─────────────────────────────      │
│                                      │ Break-even        $161.30          │
│                                      │ If called away    +$8,610          │
│                                      │ ✓ coverage assurance note          │
│                                      │ ─────────────────────────────      │
│                                      │ Credit received       +$290        │
│                                      │ Shares reserved 100 of 300         │
│                                      │ Ann. 30.5%                         │
│                                      │ [Cancel]      [ Sell to Open ]     │
└──────────────────────────────────────┴────────────────────────────────────┘
```

**Critical layout rule:** the limit price and the credit/collateral/annualized totals must be
**always visible without scrolling**. In the reference the rail is a 3-zone layout: static header
(order line + contracts + price), scrolling middle (break-even, outcome, coverage note), static
footer (credit + collateral + annualized, then the buttons). Reproduce that with a
`QVBoxLayout` where only the middle section is inside a `QScrollArea`.

---

## 4. Controls & behavior

### 4.1 Symbol
- **Preset chips:** QQQ, SPY, NVDA, TSLA, NBIS. Each chip shows the account's share count
  (`300 sh`) or `no shares`.
- **Search box** (right of the chips): type-to-filter by ticker prefix *or* company name, max 6
  results, each showing `TICKER · Company Name · position`. Enter selects the first match, Escape
  closes. Selecting a searched symbol **appends it as a chip**.
- **Right-click a chip → remove it.** Guards: cannot remove the last remaining chip; removing the
  active chip selects the first remaining one and reloads the ticket.
- Chip list should persist per user (`QSettings`).
- Changing symbol resets strike selection and limit price (see §4.6).

### 4.2 Strategy cards
Two mutually-exclusive cards. Each shows the collateral basis and a status badge:
- CC badge: `ELIGIBLE` when shares ≥ 100, else `NEEDS 100 SH` (amber).
- CSP badge: `ELIGIBLE` when the account is margin with sufficient BP.
- **Ineligible strategies stay visible and greyed with the reason** — never hide them; the user
  needs to know the feature exists and why it's unavailable.

### 4.3 Expiry
- Chips for the **next four Fridays** (label + DTE): e.g. `Jul 31 3d`, `Aug 7 10d`, `Aug 14 17d`,
  `Aug 21 24d`. Compute from today's date — do not hardcode.
- A **`More ▾` dropdown** for everything further out: remaining weeklies, monthlies (3rd Friday)
  and LEAPS, each as `Oct 16, 2026 (80d)` with a checkmark on the selection.
- When a dropdown expiry is selected, the `More` button's label becomes that date so the selection
  stays visible.
- Populate real expiries from IB (`reqSecDefOptParams`) and filter to those actually listed.

### 4.4 Strike chain
Columns: **Strike** (+ OTM/ITM tag), **Delta**, **Credit/contract**, **Yield**, **Annualized**.
- Six strikes, OTM-biased: calls above spot, puts below. Strike increments follow the underlying's
  real listed increments (the mockup approximates 2.50 under $200, 5.00 above — replace with IB
  data).
- Selecting a row sets the strike and resets the limit price to that leg's mid.
- Selected row is highlighted with a left accent bar.
- `Yield = mid / (spot for CC | strike for CSP) × 100`
- `Annualized = yield × 365 / DTE`
- Annualized is the column income sellers actually decide on — keep it, right-aligned, tabular
  figures.

### 4.5 Contracts
- `[−] n [+]` stepper plus a **`MAX n`** button.
- `max = floor(shares / 100)` for CC; `floor(BP / (strike × 100))` for CSP.
- The stepper must **clamp to max** — over-selling should be structurally impossible in the UI,
  and re-validated server-side before transmit.

### 4.6 Limit price  ← three input methods, all synced
1. **`[−]` / `[+]` step by 1 cent** (0.01).
2. **Typed entry** — a text field. Accept digits and a single decimal point while typing; round to
   2 decimals on `editingFinished`/focus-out so a partial entry like `3.` never breaks the math.
   Use a `QDoubleValidator` or equivalent.
3. **Bid / Mid / Ask chips**, each showing the actual price. Default = **Mid**.

A one-line hint changes with the choice ("At the bid — fills fastest, gives up the spread." /
"At the ask — best credit, may not fill." / "Mid-market — the usual starting point.").

Changing **symbol, strategy, expiry or strike** resets the price to the new leg's mid.

### 4.7 Submit
- Button label **Sell to Open**, styled with the app's existing SELL red.
- **Disabled** when quantity is 0, with the label replaced by the reason
  (`Not enough shares` / `Not enough buying power`) and a tooltip.
- On submit, place a single-leg SELL LMT option order on the pre-filled account.

---

## 5. Calculations

With `px` = limit price, `qty` = contracts, `K` = strike, `basis` = share cost basis:

```
credit            = px * 100 * qty
collateral (CC)   = qty * 100 shares reserved
collateral (CSP)  = K * 100 * qty  (cash / BP reserved)
break-even (CC)   = basis - px
break-even (CSP)  = K - px
if called away    = (K - basis) * 100 * qty + credit
if assigned, cost = (K - px) * 100 * qty
yield             = px / (spot for CC | K for CSP) * 100
annualized        = yield * 365 / DTE
```

Use `Decimal` for money, not float. Round display to 2 dp; round premiums to the contract's real
tick size.

---

## 6. IB / data integration

Follow the app's existing IB client patterns (same wrapper, threading and request-id allocation
the Positions and Orders panels already use).

- **Chain metadata:** `reqSecDefOptParams(underlyingSymbol, "", secType, conId)` → expirations +
  strikes. Cache per symbol/session.
- **Quotes:** `reqMktData` per option contract for bid/ask; `reqMktData` with generic tick 106 (or
  `modelGreeks`) for delta/IV. Subscribe only to the visible strikes; cancel on close/change.
- **Contract:**
  ```python
  c = Contract()
  c.symbol = "NVDA"; c.secType = "OPT"; c.exchange = "SMART"; c.currency = "USD"
  c.lastTradeDateOrContractMonth = "20260814"   # YYYYMMDD
  c.strike = 190.0; c.right = "C"               # "C" call, "P" put
  c.multiplier = "100"
  ```
- **Order:**
  ```python
  o = Order()
  o.action = "SELL"; o.orderType = "LMT"; o.totalQuantity = qty
  o.lmtPrice = float(px); o.tif = "DAY"
  o.account = account_id
  ```
- **Pre-trade check:** send with `o.whatIf = True` first to confirm margin impact, then transmit.
- Handle "no option chain for this symbol" (many futures and some ETFs) by disabling both
  strategies with a clear reason.

---

## 7. Validation & risk rules

- [ ] Quantity never exceeds collateral capacity (UI clamp **and** pre-transmit re-check).
- [ ] CC requires ≥ 100 shares *in the selected account* — not aggregated across accounts.
- [ ] CSP requires margin (or cash-secured cash) ≥ `K × 100 × qty`.
- [ ] Reserved collateral is reflected in buying power immediately after fill, so a CSP can't be
      double-spent against a Sweep in.
- [ ] Shares backing a covered call must be flagged as locked so Flatten / 25-50-75% cannot sell
      them out from under an open short call (warn or block).
- [ ] Limit price > 0 and on the contract's tick increment.
- [ ] Expiry not in the past; reject stale chain data.
- [ ] Admin role cannot place these orders (per the existing role model).

---

## 8. Suggested file layout

Match the repo's existing structure; this is a guide, not a mandate:

```
app/
  options/
    __init__.py
    chain.py            # chain fetch/cache, expiry + strike selection, greeks
    pricing.py          # credit / yield / annualized / break-even math (Decimal)
    eligibility.py      # CC & CSP collateral capacity + reasons
    order.py            # Contract/Order construction, whatIf check, transmit
  ui/
    options/
      options_ticket.py       # the dialog/panel (QDialog)
      symbol_selector.py      # chips + search + right-click remove
      expiry_selector.py      # 4 Friday chips + More dropdown
      chain_table.py          # strike ladder (QTableView + model)
      ticket_rail.py          # 3-zone rail: price / detail / totals+buttons
  ui/positions_panel.py       # + customContextMenuRequested → "Options income…"
```

---

## 9. Element → existing-style mapping

Map each element to the role it already has in the app. (Mockup hexes in parentheses are for
identification only — use the repo's tokens.)

| Element | Use the same style as |
|---|---|
| Window / panel background | existing Positions panel background (`#0a0a0d`) |
| Card / rail background | existing sub-panel background (`#101013`) |
| Borders, dividers | existing table gridline / panel border (`#1d1d22`, `#2a2a31`) |
| Primary text / muted text | existing table value / header text (`#eeeef0` / `#8a8a92`) |
| Numeric cells | existing P&L cells — **tabular figures, right-aligned**, mono where the app uses mono |
| Selected row, active chip | existing selected-account blue (`#2d7ff9` / tinted `rgba(45,127,249,.16)`) |
| Positive / gain | existing green (`#5fce77`, `#34b34a`) |
| **Sell to Open** button | existing **SELL** button red (`#c9433c`) |
| Warning / ineligible badge | existing warning amber (`#e0a63a`) |
| Secondary buttons (steppers, Bid/Mid/Ask) | existing 25/50/75% grey buttons (`#2a2a31`) |
| Accent icons / eligible badge | existing teal accent (`#2ec4a6`) |
| Context menu | existing app `QMenu` styling |

---

## 10. Acceptance checklist

- [ ] Right-clicking the account row shows the menu with a single **Options income…** item.
- [ ] Ticket opens pre-filled with that account; strategy chosen inside the ticket.
- [ ] Preset chips QQQ/SPY/NVDA/TSLA/NBIS render with live share counts.
- [ ] Search filters by ticker and company name; Enter picks the top hit; picked symbols become chips.
- [ ] Right-click removes a chip; last chip cannot be removed; removing the active chip reselects.
- [ ] Expiry shows the next four Fridays computed from today, plus a `More` dropdown of later dates.
- [ ] Chain shows six strikes with delta, credit, yield and annualized; selecting one resets price to mid.
- [ ] Price is settable three ways (±0.01 stepper, typed entry, Bid/Mid/Ask) and all stay in sync.
- [ ] Credit, break-even, if-called/if-assigned, collateral and annualized recompute on every change.
- [ ] Limit price and the credit/collateral/annualized totals are visible without scrolling.
- [ ] `MAX` respects collateral; the stepper cannot exceed it.
- [ ] Ineligible strategy shows the reason and disables Sell to Open with an explanatory label.
- [ ] Order transmits as SELL / LMT / OPT on the correct account, after a `whatIf` margin check.
- [ ] The ticket is visually indistinguishable in style from the existing panels.
