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
        "entry_price": entry,
        "quantity": quantity,
        "notional_usdt": notional_usdt,
        "margin_usdt": margin_usdt,
        "fits_balance": margin_usdt <= balance,
    }


def parse_bingx_contract_specs(payload):
    """Parse BingX public contract-info response."""
    from decimal import Decimal
    if not isinstance(payload, dict) or str(payload.get("code", "0")) not in {"0", "None"}:
        raise ValueError("Invalid BingX contract response")
    data = payload.get("data")
    if isinstance(data, dict):
        data = data.get("data", data.get("list", data.get("rows")))
    if not isinstance(data, list) or not data:
        raise ValueError("Missing BingX contract list")
    result = {}
    for item in data:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        try:
            precision = int(item.get("quantityPrecision"))
            if not 0 <= precision <= 18:
                precision = None
        except (TypeError, ValueError, OverflowError):
            precision = None
        def positive(key):
            try:
                value = Decimal(str(item.get(key)))
                return value if value.is_finite() and value > 0 else None
            except Exception:
                return None
        status = item.get("status")
        result[str(item["symbol"]).upper()] = {
            "quantity_precision": precision,
            "min_quantity": positive("tradeMinQuantity"),
            "min_notional": positive("tradeMinUSDT"),
            "active": str(status) == "1",
            "api_open": str(item.get("apiStateOpen", "")).lower() == "true",
            "max_long_leverage": positive("maxLongLeverage"),
            "max_short_leverage": positive("maxShortLeverage"),
        }
    if not result:
        raise ValueError("No valid BingX contract records")
    return result


def apply_bingx_contract_constraints(sizing, spec, side, leverage):
    """Round down to quantityPrecision and verify minimums and API status."""
    from decimal import Decimal, InvalidOperation, ROUND_DOWN

    direction = str(side).strip().upper()
    if direction not in {"LONG", "BUY", "SHORT", "SELL"}:
        return {"validated": False, "reasons": ["invalid_side"]}
    try:
        lev = Decimal(str(leverage))
        if not lev.is_finite() or lev <= 0:
            raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        return {"validated": False, "reasons": ["invalid_leverage"]}

    precision = spec.get("quantity_precision")
    if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= 18:
        return {"validated": False, "reasons": ["unknown_or_invalid_quantity_precision"]}
    try:
        quantity_input = Decimal(str(sizing["quantity"]))
        entry = Decimal(str(sizing["entry_price"]))
        distance = Decimal(str(sizing["stop_distance"]))
        if (not quantity_input.is_finite() or quantity_input <= 0
                or not entry.is_finite() or entry <= 0
                or not distance.is_finite() or distance <= 0):
            raise ValueError
        quantity = quantity_input.quantize(
            Decimal(1).scaleb(-precision), rounding=ROUND_DOWN
        )
        min_qty = spec.get("min_quantity")
        min_value = spec.get("min_notional")
        if min_qty is not None:
            min_qty = Decimal(str(min_qty))
            if not min_qty.is_finite() or min_qty <= 0:
                min_qty = None
        if min_value is not None:
            min_value = Decimal(str(min_value))
            if not min_value.is_finite() or min_value <= 0:
                min_value = None
        max_key = "max_long_leverage" if direction in {"LONG", "BUY"} else "max_short_leverage"
        max_lev = spec.get(max_key)
        if max_lev is not None:
            max_lev = Decimal(str(max_lev))
            if not max_lev.is_finite() or max_lev <= 0:
                max_lev = None
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return {"validated": False, "reasons": ["invalid_sizing_or_contract_metadata"]}

    notional = quantity * entry
    min_qty_ok = None if min_qty is None else quantity >= min_qty
    min_value_ok = None if min_value is None else notional >= min_value
    lev_ok = None if max_lev is None else lev <= max_lev
    reasons = []
    if quantity <= 0: reasons.append("quantity_rounds_to_zero")
    if min_qty_ok is not True: reasons.append("minimum_quantity_unknown_or_failed")
    if min_value_ok is not True: reasons.append("minimum_notional_unknown_or_failed")
    if spec.get("active") is not True: reasons.append("contract_not_active")
    if spec.get("api_open") is not True: reasons.append("api_open_not_confirmed")
    if lev_ok is not True: reasons.append("leverage_unknown_or_too_high")
    return {
        "validated": not reasons, "reasons": reasons, "quantity": quantity,
        "quantity_precision": precision,
        "notional_usdt": notional, "risk_usdt": quantity * distance,
        "margin_usdt": notional / lev, "min_quantity_ok": min_qty_ok,
        "min_notional_ok": min_value_ok, "leverage_ok": lev_ok,
    }
