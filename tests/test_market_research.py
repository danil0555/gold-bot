import io
import zipfile
import numpy as np
import pandas as pd

from scripts.market_research import add_features, build_panel, parse_binance_kline_archive, _select_threshold, add_bingx_flow_features, BASE_FEATURES, BINGX_FLOW_FEATURES, CROSS_VENUE_FEATURES, _select_available_features


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
        "quote_volume": close * (1000 + (np.arange(periods) % 17) * 20),
        "trade_count": 200 + (np.arange(periods) % 11) * 4,
        "taker_buy_base": (1000 + (np.arange(periods) % 17) * 20) * 0.55,
        "taker_buy_quote": close * (1000 + (np.arange(periods) % 17) * 20) * 0.55,
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
    assert np.isfinite(features.iloc[-1][BASE_FEATURES].to_numpy(dtype=float)).all()


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
    # A positive move smaller than the cost barrier is still part of the
    # directional dataset; costs belong in PnL evaluation, not sample filtering.
    assert row["label_1h"] == 1.0
    small_move = panel.loc[(panel["symbol"] == "BTC-USDT") & (panel["fwd_1h"].abs() < 0.0012)]
    assert len(small_move) > 0
    assert small_move["label_1h"].notna().all()
    assert (small_move["label_1h"] == (small_move["fwd_1h"] > 0).astype(float)).all()
    assert pd.isna(panel.loc[panel.index[-1], "fwd_24h"])
    assert set(CROSS_VENUE_FEATURES).issubset(panel.columns)
    assert abs(row["binance_taker_imbalance_lag1"] - 0.1) < 1e-9
    assert np.isfinite(row["btc_ret_24h"])
    assert np.isfinite(row["btc_rv_24h"])



def test_binance_public_archive_parser_handles_header_and_taker_buy_fields():
    csv_text = (
        "open_time,open,high,low,close,volume,close_time,quote_volume,number_of_trades,"
        "taker_buy_base_asset_volume,taker_buy_quote_asset_volume,ignore\n"
        "1700000000000,100,101,99,100.5,10,1700003599999,1005,42,6,603,0\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("BTCUSDT-1h-2023-11.csv", csv_text)
    parsed = parse_binance_kline_archive(buffer.getvalue())
    assert len(parsed) == 1
    assert parsed.iloc[0]["time"] == 1_700_000_000_000
    assert parsed.iloc[0]["quote_volume"] == 1005
    assert parsed.iloc[0]["trade_count"] == 42
    assert parsed.iloc[0]["taker_buy_quote"] == 603


def test_binance_cross_venue_features_use_previous_completed_candle():
    bingx = sample_frame()
    original = sample_binance_frame()
    changed = original.copy()
    changed.loc[150:, ["close", "quote_volume", "trade_count", "taker_buy_quote"]] *= 2
    base = build_panel(
        {"BTC-USDT": bingx}, 0.0012, {"BTC-USDT": original}
    )
    altered = build_panel(
        {"BTC-USDT": bingx}, 0.0012, {"BTC-USDT": changed}
    )
    cols = list(CROSS_VENUE_FEATURES)
    pd.testing.assert_frame_equal(
        base.loc[:149, cols], altered.loc[:149, cols], check_dtype=False
    )



def test_validation_threshold_selection_returns_out_of_sample_ready_cutoff():
    y = np.array([1] * 100 + [0] * 100)
    probabilities = np.array([0.82] * 100 + [0.18] * 100)
    returns = np.array([0.02] * 100 + [-0.02] * 100)
    threshold, report = _select_threshold(y, probabilities, returns, 0.0012, 0.58)
    assert 0.52 <= threshold <= 0.75
    assert report["method"] == "validation_mean_net_return_minus_one_standard_error"
    assert report["validation_metrics"]["signals"] == 200


def test_validation_threshold_falls_back_when_too_few_validation_signals():
    y = np.array([1, 0, 1, 0])
    probabilities = np.array([0.9, 0.1, 0.9, 0.1])
    returns = np.array([0.01, -0.01, 0.01, -0.01])
    threshold, report = _select_threshold(y, probabilities, returns, 0.0012, 0.58)
    assert threshold == 0.58
    assert report["method"] == "fixed_fallback_no_validation_threshold_met"



def test_bingx_flow_features_are_lagged_and_finite():
    frame = sample_frame()
    features = add_bingx_flow_features(frame)
    assert set(BINGX_FLOW_FEATURES).issubset(features.columns)
    assert features.iloc[-1][BINGX_FLOW_FEATURES].notna().all()
    changed = frame.copy()
    changed.loc[150:, ["close", "quote_volume", "trade_count", "taker_buy_quote"]] *= 2
    changed_features = add_bingx_flow_features(changed)
    pd.testing.assert_frame_equal(
        features.loc[:149, BINGX_FLOW_FEATURES],
        changed_features.loc[:149, BINGX_FLOW_FEATURES],
        check_dtype=False,
    )


def test_bingx_only_panel_has_flow_features_without_binance_data():
    btc = sample_frame()
    alt = sample_frame(offset=10.0)
    panel = build_panel({"BTC-USDT": btc, "ALT-USDT": alt}, 0.0012, {})
    assert set(BINGX_FLOW_FEATURES).issubset(panel.columns)
    assert panel.loc[panel["symbol"] == "BTC-USDT"].iloc[-2][BINGX_FLOW_FEATURES].notna().all()
    assert panel["binance_taker_imbalance_lag1"].isna().all()



def test_feature_coverage_selection_uses_training_only_and_keeps_partial_rows():
    frame = sample_frame(periods=260)
    panel = pd.DataFrame({
        "time": frame["time"],
        "good": np.sin(np.arange(260) / 7),
        "sparse": np.where(np.arange(260) % 2 == 0, np.nan, 1.0),
        "future_only": np.where(np.arange(260) >= 200, np.arange(260), np.nan),
    })
    end = int(frame["time"].iloc[180])
    selected, coverage = _select_available_features(
        panel, ["good", "sparse", "future_only"], end, min_coverage=0.60
    )
    assert selected == ["good"]
    assert coverage["good"] == 1.0
    assert coverage["sparse"] < 0.60
    assert coverage["future_only"] == 0.0
    # Feature selection is diagnostic only: it does not drop panel rows.
    assert len(panel) == 260
