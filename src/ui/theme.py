"""
Centralized styling variables for NanoPulse UI.
"""


class Theme:
    # Panels and Backgrounds
    BG_PANEL = "#151922"
    BG_INPUT = "#1B2130"
    BG_MAIN = "#0E1117"

    # Text
    TEXT = "#E6EBF2"
    TEXT_DIM = "#A3AFC2"

    # Borders and Accents
    BORDER = "#262E3F"
    ACCENT = "#3FB950"

    # Action Colors
    CLR_BUY = "#2EA043"
    CLR_SELL = "#F85149"
    CLR_BUY_LMT = "#1F6F37"
    CLR_SELL_LMT = "#8B2A28"

    # Position Colors
    CLR_FLATTEN = "#E08A3C"
    CLR_REVERSE = "#A371F7"
    CLR_CLOSE = "#9DA3AB"
    CLR_CANCEL = "#3A414C"

    # Percent-sizing accent (gold) — the SH/% unit toggle and the
    # percent-mode size chips.  Deliberately distinct from CLR_FLATTEN's
    # orange: the gold row is the visual guard against misreading "25%"
    # as 25 shares.
    CLR_PCT = "#2DB4A4"          # solid gold (selected chip / active toggle)
    CLR_PCT_BORDER = "#43C6B7"   # gold border
    CLR_PCT_BG = "rgba(45,180,164,.12)"       # dark-gold unselected chip background
    CLR_PCT_TEXT = "#2DB4A4"     # gold text on dark chip
    CLR_PCT_ON = "#0d1a17"       # near-black text on solid gold
