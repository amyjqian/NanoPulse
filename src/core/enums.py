"""
Common enums used across NanoPulse core and UI components.
"""
from enum import Enum


class OrderAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MKT = "MKT"
    LMT = "LMT"
    ADAPTIVE = "ADAPTIVE"
    PEG_PRIM = "PEG_PRIM"
