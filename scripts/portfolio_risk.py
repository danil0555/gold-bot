"""Portfolio-level risk budget helpers.

Pure calculation only: no database reads, inferred entries, or exchange actions.
Callers must pass user-confirmed OPEN positions and their remaining stop risk.
"""

from decimal import Decimal, InvalidOperation


def _non_negative(value, field):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a finite non-negative number")
    if not number.is_finite() or number < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return number


def calculate_portfolio_risk(balance_usdt, open_positions, proposed_risk_usdt, max_total_risk_percent):
    """Check a proposal against an explicit portfolio risk cap.

    Count a position only when confirmed is exactly True and status is OPEN.
    Such records must provide remaining_risk_usdt measured to the CURRENT stop
    for the remaining quantity. Unconfirmed and closed records are ignored.
    The cap is mandatory: this helper deliberately does not invent policy.
    """
    balance = _non_negative(balance_usdt, "balance_usdt")
    proposed = _non_negative(proposed_risk_usdt, "proposed_risk_usdt")
    cap_pct = _non_negative(max_total_risk_percent, "max_total_risk_percent")
    if balance <= 0:
        raise ValueError("balance_usdt must be greater than zero")
    if cap_pct <= 0 or cap_pct > 100:
        raise ValueError("max_total_risk_percent must be greater than 0 and at most 100")
    if not isinstance(open_positions, (list, tuple)):
        raise ValueError("open_positions must be a list or tuple")

    open_risk = Decimal("0")
    counted = 0
    for index, position in enumerate(open_positions):
        if not isinstance(position, dict):
            raise ValueError(f"open_positions[{index}] must be a mapping")
        if position.get("confirmed") is not True or str(position.get("status", "")).upper() != "OPEN":
            continue
        if "remaining_risk_usdt" not in position:
            raise ValueError(f"open_positions[{index}] is OPEN but has no remaining_risk_usdt")
        open_risk += _non_negative(position["remaining_risk_usdt"], f"open_positions[{index}].remaining_risk_usdt")
        counted += 1

    cap_usdt = balance * cap_pct / Decimal("100")
    available = max(Decimal("0"), cap_usdt - open_risk)
    projected = open_risk + proposed
    return {
        "balance_usdt": balance,
        "max_total_risk_percent": cap_pct,
        "risk_cap_usdt": cap_usdt,
        "open_risk_usdt": open_risk,
        "available_risk_usdt": available,
        "proposed_risk_usdt": proposed,
        "projected_risk_usdt": projected,
        "remaining_after_proposal_usdt": cap_usdt - projected,
        "counted_open_positions": counted,
        "allowed": projected <= cap_usdt,
    }
