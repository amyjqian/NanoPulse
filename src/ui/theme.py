"""
Centralized styling variables for NanoPulse UI.
"""
from PySide6.QtGui import QColor


def hover_shades(colour: str) -> tuple[str, str]:
    """(hover, pressed) background shades for flat action buttons.

    The surface lightens ~30% under the cursor and darkens ~15% while
    pressed — no border, no layout shift (the old style drew a 1 px
    white frame on hover, which both looked dated and nudged the label).
    """
    c = QColor(colour)
    return c.lighter(130).name(), c.darker(115).name()


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
    CLR_CLOSE = "#6E757E"
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
