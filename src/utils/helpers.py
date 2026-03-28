"""
Math utilities and helpers for tick sizes, PnL calculations, and position sizing.
"""


def round_to_tick(price: float, tick_size: float = 0.01) -> float:
    """Round price to nearest tick size."""
    return round(price / tick_size) * tick_size


def calculate_pnl(entry_price: float, exit_price: float, quantity: int, multiplier: int = 1) -> float:
    """Calculate PnL for a trade."""
    return (exit_price - entry_price) * quantity * multiplier


def calculate_position_size(account_balance: float, risk_percent: float, stop_loss_ticks: int) -> int:
    """Calculate position size based on account balance and risk parameters."""
    risk_amount = account_balance * (risk_percent / 100)
    tick_value = 5  # Default ES/NQ tick value in dollars
    position_size = risk_amount / (stop_loss_ticks * tick_value)
    return int(position_size)

