import numpy as np
import pandas as pd

from scripts.market_research import add_features, build_panel, FEATURES


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


def test_feature_builder_produces_causal_finite_recent_features():
    frame = sample_frame()
    btc_returns = frame.set_index("time")["close"].pct_change()
    features = add_features(frame, btc_returns)
    assert set(FEATURES).issubset(features.columns)
    assert features.iloc[-1][FEATURES].notna().all()
    assert np.isfinite(features.iloc[-1][FEATURES].to_numpy(dtype=float)).all()


def test_feature_values_before_future_change_are_unchanged():
    original = sample_frame()
    changed = original.copy()
    changed.loc[220:, ["open", "high", "low", "close", "volume"]] *= 1.7
    original_features = add_features(original)
    changed_features = add_features(changed)
    cols = [c for c in FEATURES if c not in {"btc_ret_1h", "relative_ret_1h", "relative_ret_24h", "btc_corr_72h"}]
    pd.testing.assert_frame_equal(
        original_features.loc[:219, cols],
        changed_features.loc[:219, cols],
        check_dtype=False,
    )


def test_panel_labels_use_forward_returns_and_cost_barrier():
    btc = sample_frame()
    alt = sample_frame(offset=10.0)
    panel = build_panel({"BTC-USDT": btc, "ALT-USDT": alt}, cost=0.0012)
    row = panel.loc[panel["symbol"] == "BTC-USDT"].iloc[100]
    expected = btc.loc[101, "close"] / btc.loc[100, "close"] - 1
    assert abs(row["fwd_1h"] - expected) < 1e-12
    assert row["label_1h"] in (0.0, 1.0, np.nan) or pd.isna(row["label_1h"])
    assert pd.isna(panel.loc[panel.index[-1], "fwd_24h"])
