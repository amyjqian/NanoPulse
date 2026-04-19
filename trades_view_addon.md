**Context:**
We are continuing development on "NanoPulse", a high-performance desktop trading platform using Python, `PySide6`, `qasync`, and `ib_async`. The UI uses a strict dark-mode theme with `#CC6600` as the high-contrast accent color.

**Task:**
Design and implement the advanced "Trades" tab interface. This includes a top control toolbar, a dynamic view area that switches between "Flat Log" and "Summary", and a sophisticated Preferences Dialog for column management.

**Component 1: Top Control Toolbar**
Create a horizontal layout containing:
1. **Summary Toggle:** A switch (e.g., a styled `QCheckBox` or custom toggle widget). When OFF, show the standard Trades view. When ON, show the Summary view.
2. **Date Filter:** A `QComboBox` with options: "Today", "Last 2 Days", up to "Last 7 Days".
3. **Global Search:** A `QLineEdit` with a search icon placeholder. It must filter rows dynamically based on Symbol, Action, Account, or Security Type.
4. **Settings Button:** A `QPushButton` (gear icon) aligned to the far right that opens the Preferences Dialog.

**Component 2: Data Models & Views (`QStackedWidget`)**
Use a `QStackedWidget` to instantly swap between two `QTableView` instances based on the Summary Toggle state. 
* **Search/Filter Implementation:** Both views MUST be wrapped in a `QSortFilterProxyModel`. Wire the Global Search `QLineEdit` and Date Filter `QComboBox` to this proxy model so filtering is instant and doesn't block the event loop or require re-fetching from the IBKR API.

* **View A (Summary OFF - Flat Trades):** Use the standard execution log model we previously defined.
* **View B (Summary ON - Aggregated Trades):** Create a new `QAbstractTableModel`. This model must group the raw `Fill` objects by `contract.symbol`.
    * **Summary Columns:** Financial Instrument, Buys (sum of BOT shares), Sells (sum of SLD shares), Net (Buys - Sells), Avg Buy Price (VWAP of buys), Avg Sell Price (VWAP of sells), Net Total, Commission (summed), Net Incl. Comm, Realized P&L, Exchange List (comma-separated unique routing venues).

**Component 3: Preferences Dialog (Column Management)**
When the Settings button is clicked, open a modal `QDialog`.
1. **Context Awareness:** The dialog must know which view is currently active (Trades vs. Summary). It should display the column configuration for the *active* view.
2. **Layout:** Left sidebar navigation (selects "Columns"). Main area shows a title (e.g., "Trades Columns") and two side-by-side lists: "Selected Columns" and "Available Columns".
3. **Selected Columns List (`QListWidget`):** * Must support drag-and-drop internal reordering (`setDragDropMode(QAbstractItemView.InternalMove)`).
    * Each row needs a "hamburger" icon (≡) indicator for dragging.
    * Each row needs a "minus" (-) icon button to move the item back to Available Columns.
4. **Available Columns List (`QListWidget`):**
    * Include a quick-search `QLineEdit` directly above it to filter available columns.
    * Each row needs a "plus" (+) icon button to move the item to Selected Columns.
5. **Persistence:** When the user clicks "Apply" or closes the dialog, dynamically update the visible columns in the active `QTableView` using `setColumnHidden()` or by updating the underlying model's `headerData`.

**Requirements:**
* Ensure all styling matches the dark mode spec. Use `#CC6600` for active toggles, highlighted list items, and focus borders on the search inputs.
* Provide modular, object-oriented PySide6 code. Separate the Dialog class, the Proxy Models, and the Main Widget.