import numpy as np
import pandas as pd

from scripts.market_research import add_features, build_panel, BASE_FEATURES, CROSS_VENUE_FEATURES


def sample_frame(periods=260, offset=0.0):
    times = np.arange(periods, dtype=np.int64) * 3_600_000 + 1_700_000_000_000
    close = 100 + np.arange(periods) * 0.08 + np.sin(np.arange(periods) / 5) + offset
    return pd.DataFrame({
        "time": times,
        "open": close * 0.999,
        "high": close * 1.004,
        "low": close * 0.996,
        "close": close,
        "volume": 1000 + (np.arange(periods) % 17) * 20,
    })


def sample_binance_frame(periods=260, offset=0.0):
    frame = sample_frame(periods, offset)
    frame["quote_volume"] = frame["close"] * frame["volume"]
    frame["trade_count"] = 200 + (np.arange(periods) % 11) * 4
    frame["taker_buy_base"] = frame["volume"] * 0.55
    frame["taker_buy_quote"] = frame["quote_volume"] * 0.55
    return frame


def test_feature_builder_produces_causal_finite_recent_features():
    frame = sample_frame()
    btc_returns = frame.set_index("time")["close"].pct_change()
    features = add_features(frame, btc_returns)
    assert set(BASE_FEATURES).issubset(features.columns)
    assert features.iloc[-1][BASE_FEATURES].notna().all()
    assert np.isfinite(features.iloc[-1][FEATURES].to_numpy(dtype=float)).all()


def test_feature_values_before_future_change_are_unchanged():
    original = sample_frame()
    changed = original.copy()
    changed.loc[220:, ["open", "high", "low", "close", "volume"]] *= 1.7
    original_features = add_features(original)
    changed_features = add_features(changed)
    cols = [c for c in BASE_FEATURES if c not in {"btc_ret_1h", "relative_ret_1h", "relative_ret_24h", "btc_corr_72h"}]
    pd.testing.assert_frame_equal(
        original_features.loc[:219, cols],
        changed_features.loc[:219, cols],
        check_dtype=False,
    )


def test_panel_labels_use_forward_returns_and_cost_barrier():
    btc = sample_frame()
    alt = sample_frame(offset=10.0)
    panel = build_panel(
        {"BTC-USDT": btc, "ALT-USDT": alt},
        cost=0.0012,
        binance_frames={
            "BTC-USDT": sample_binance_frame(),
            "ALT-USDT": sample_binance_frame(offset=10.0),
        },
    )
    row = panel.loc[panel["symbol"] == "BTC-USDT"].iloc[100]
    expected = btc.loc[101, "close"] / btc.loc[100, "close"] - 1
    assert abs(row["fwd_1h"] - expected) < 1e-12
    assert row["label_1h"] in (0.0, 1.0, np.nan) or pd.isna(row["label_1h"])
    assert pd.isna(panel.loc[panel.index[-1], "fwd_24h"])
    assert set(CROSS_VENUE_FEATURES).issubset(panel.columns)
    assert abs(row["binance_taker_imbalance_lag1"] - 0.1) < 1e-9
