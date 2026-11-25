"""Conservative position sizing for linear USDT-M contracts.

The result is a theoretical estimate before fees, funding, slippage and
exchange-specific quantity-step/minimum-notional constraints.
"""

import math


def calculate_linear_usdtm_position_size(
    balance_usdt: float,
    entry_price: float,
    stop_price: float,
    side: str,
    risk_percent: float = 10.0,
    leverage: float = 5.0,
) -> dict[str, float | bool]:
    """Estimate base-asset quantity from a fixed USDT loss budget at stop."""
    values = (balance_usdt, entry_price, stop_price, risk_percent, leverage)
    if any(not math.isfinite(float(value)) for value in values):
        raise ValueError("All sizing inputs must be finite numbers.")

    balance = float(balance_usdt)
    entry = float(entry_price)
    stop = float(stop_price)
    risk_pct = float(risk_percent)
    lev = float(leverage)
    direction = str(side).strip().upper()

    if balance <= 0 or entry <= 0 or stop <= 0:
        raise ValueError("Balance, entry and stop prices must be positive.")
    if not 0 < risk_pct <= 100:
        raise ValueError("Risk percent must be greater than 0 and at most 100.")
    if lev <= 0:
        raise ValueError("Leverage must be positive.")

    if direction in {"LONG", "BUY"}:
        if stop >= entry:
            raise ValueError("A LONG stop must be below entry.")
    elif direction in {"SHORT", "SELL"}:
        if stop <= entry:
            raise ValueError("A SHORT stop must be above entry.")
    else:
        raise ValueError("Side must be LONG/BUY or SHORT/SELL.")

    stop_distance = abs(entry - stop)
    risk_usdt = balance * risk_pct / 100.0
    quantity = risk_usdt / stop_distance
    notional_usdt = quantity * entry
    margin_usdt = notional_usdt / lev

    return {
        "risk_usdt": risk_usdt,
        "stop_distance": stop_distance,
        "quantity": quantity,
        "notional_usdt": notional_usdt,
        "margin_usdt": margin_usdt,
        "fits_balance": margin_usdt <= balance,
    }
