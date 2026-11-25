import pytest

from scripts.portfolio_risk import calculate_portfolio_risk


def position(risk, status="OPEN", confirmed=True):
    return {"remaining_risk_usdt": risk, "status": status, "confirmed": confirmed}


def test_counts_only_confirmed_open_positions_and_calculates_remaining_budget():
    result = calculate_portfolio_risk(
        balance_usdt=1000,
        open_positions=[
            position("75"),
            position("50", status="CLOSED"),
            position("100", confirmed=False),
        ],
        proposed_risk_usdt=100,
        max_total_risk_percent=20,
    )
    assert result["risk_cap_usdt"] == 200
    assert result["open_risk_usdt"] == 75
    assert result["available_risk_usdt"] == 125
    assert result["projected_risk_usdt"] == 175
    assert result["remaining_after_proposal_usdt"] == 25
    assert result["counted_open_positions"] == 1
    assert result["allowed"] is True


def test_blocks_proposal_that_would_exceed_explicit_portfolio_cap():
    result = calculate_portfolio_risk(1000, [position(150)], 100, 20)
    assert result["projected_risk_usdt"] == 250
    assert result["remaining_after_proposal_usdt"] == -50
    assert result["available_risk_usdt"] == 50
    assert result["allowed"] is False


def test_allows_proposal_exactly_at_cap():
    result = calculate_portfolio_risk(1000, [position(100)], 100, 20)
    assert result["allowed"] is True
    assert result["remaining_after_proposal_usdt"] == 0


def test_does_not_invent_a_portfolio_cap_or_count_signal_only():
    result = calculate_portfolio_risk(1000, [], 100, 10)
    assert result["allowed"] is True
    assert result["open_risk_usdt"] == 0
    assert result["counted_open_positions"] == 0


def test_open_confirmed_position_without_remaining_risk_fails_closed():
    with pytest.raises(ValueError, match="remaining_risk_usdt"):
        calculate_portfolio_risk(1000, [{"status": "OPEN", "confirmed": True}], 10, 20)


@pytest.mark.parametrize("value", [-1, "NaN", "Infinity", None, "not-a-number"])
def test_rejects_invalid_position_risk(value):
    with pytest.raises(ValueError):
        calculate_portfolio_risk(1000, [position(value)], 10, 20)


@pytest.mark.parametrize("cap", [0, -1, 100.01, "NaN", "Infinity"])
def test_rejects_invalid_portfolio_cap(cap):
    with pytest.raises(ValueError):
        calculate_portfolio_risk(1000, [], 10, cap)


def test_rejects_negative_proposed_risk_and_invalid_balance():
    with pytest.raises(ValueError):
        calculate_portfolio_risk(1000, [], -1, 20)
    with pytest.raises(ValueError):
        calculate_portfolio_risk(0, [], 1, 20)


def test_requires_position_records_to_be_mappings():
    with pytest.raises(ValueError, match="must be a mapping"):
        calculate_portfolio_risk(1000, [10], 10, 20)
