"""
Centralized styling variables for NanoPulse UI.
"""


class Theme:
    # Panels and Backgrounds
    BG_PANEL = "#1e2230"
    BG_INPUT = "#272b3a"
    BG_MAIN = "#161a25"

    # Text
    TEXT = "#C9D1D9"
    TEXT_DIM = "#8B949E"

    # Borders and Accents
    BORDER = "#30363D"
    ACCENT = "#3FB950"

    # Action Colors
    CLR_BUY = "#2e7d32"
    CLR_SELL = "#c62828"
    CLR_BUY_LMT = "#1b5e20"
    CLR_SELL_LMT = "#8b0000"

    # Position Colors
    CLR_FLATTEN = "#cc6600"
    CLR_REVERSE = "#8E44AD"
    CLR_CLOSE = "#8E8E93"
    CLR_CANCEL = "#3A3A3C"

    # Percent-sizing accent (gold) — the SH/% unit toggle and the
    # percent-mode size chips.  Deliberately distinct from CLR_FLATTEN's
    # orange: the gold row is the visual guard against misreading "25%"
    # as 25 shares.
    CLR_PCT = "#c9a227"          # solid gold (selected chip / active toggle)
    CLR_PCT_BORDER = "#e0b93a"   # gold border
    CLR_PCT_BG = "#2b2a1e"       # dark-gold unselected chip background
    CLR_PCT_TEXT = "#d9c07a"     # gold text on dark chip
    CLR_PCT_ON = "#1a1608"       # near-black text on solid gold
