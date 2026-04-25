**Context:**
We are building a high-performance desktop trading platform called "NanoPulse" using Python, `PySide6`, `qasync`, and `ib_async`. The UI is an institutional-grade, dark-mode platform. Use `#CC6600` for high-contrast accent elements.

**Task:**
Create the main central widget using a `QTabWidget` to toggle between a "Positions" view and a "Trades" view.

**UI Architecture & Styling:**
1. **Container:** Implement a `QTabWidget` with no visible pane borders to match modern flat design.
2. **Tab Bar Styling:** Style the `QTabBar` for dark mode. Unselected tabs should have subdued gray text. The selected (active) tab must use the `#CC6600` accent color for its text and have a 2px solid `#CC6600` bottom border to indicate it is active. 
3. **Tab 1 ("Positions"):** For now, insert a placeholder `QTableView` formatted for dark mode.
4. **Tab 2 ("Trades"):** Insert the highly optimized, flat `QTableView` Execution Log.

**Trades View Data Architecture (The "Unified Pipeline"):**
The Trades tab must implement the following data model:
1. **Startup/Reconnect:** Call `await ib.reqExecutionsAsync(ExecutionFilter())` to fetch all session trades from midnight up to the current moment. 
2. **Live Data:** Subscribe to `ib.execDetailsEvent`. Extract ONLY the `fill` object from the callback (`def on_live_trade(trade, fill):`).
3. **Flat Logic:** Pass both startup and live `Fill` objects through the same data-extraction pipeline. Do NOT aggregate child fills. Route multi-exchange fills as distinct, chronological rows. Insert live trades at the top (index 0) using `beginInsertRows`.

**Trades Table Columns & Mapping:**
Flatten the nested `Fill` object into these columns (handle missing attributes gracefully):
* `Time`: `fill.execution.time` (HH:MM:SS)
* `Symbol`: `fill.contract.symbol`
* `Action`: `fill.execution.side`
* `Quantity`: `fill.execution.shares`
* `Price`: `fill.execution.price`
* `Exchange`: `fill.execution.exchange`
* `OrderRef`: `fill.execution.orderRef`
* `Account`: `fill.execution.acctNumber`
* `Expiry`: `fill.contract.lastTradeDateOrContractMonth`
* `Strike / Right`: `fill.contract.strike` / `fill.contract.right`
* `Commission`: `fill.commissionReport.commission` (handle async arrival/None)
* `Realized PNL`: `fill.commissionReport.realizedPNL`
* `Order ID`: `fill.execution.orderId`
* `Submitter`: `fill.execution.clientId`
* `Liquidation`: `fill.execution.liquidation`
* `Last Liquidity`: `fill.execution.lastLiquidity`

**Requirements:**
* Ensure `qasync` best practices so API calls do not block the PySide6 event loop.
* Do not include parent-level aggregate math (like `avgPrice`) in the Trades model.
* Provide the complete, runnable code including the `QTabWidget` setup, the `QAbstractTableModel` for the Trades, and the dark mode stylesheet.

reference screen
