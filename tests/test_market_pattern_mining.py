import numpy as np
import pandas as pd

from scripts.market_pattern_mining import mine_patterns


def test_mines_repeated_feature_combinations_against_non_event_controls():
    rows = 180
    panel = pd.DataFrame({
        "symbol": ["BTC-USDT"] * rows,
        "minute_ts": np.arange(rows, dtype=np.int64) * 60_000,
        "close": np.full(rows, 100.0),
        "rv_60m": np.full(rows, 0.0002),
        "rv_30m": np.full(rows, 0.0002),
        "volume_surprise_z": np.linspace(-2, 2, rows),
        "trades_surprise_z": np.linspace(-1.5, 1.5, rows),
        "taker_imbalance": np.sin(np.arange(rows) / 8),
        "book_imbalance5": np.cos(np.arange(rows) / 9),
        "book_imbalance20": np.sin(np.arange(rows) / 11),
        "microprice_offset_bps": np.linspace(-3, 3, rows),
        "spread_bps": np.linspace(0.5, 2.0, rows),
        "open_interest_change_pct": np.linspace(-0.01, 0.01, rows),
        "funding_rate": np.linspace(-0.001, 0.001, rows),
    })
    episodes = []
    for idx in (145, 150, 155, 160, 165, 170):
        row = panel.iloc[idx]
        episodes.append({
            "symbol": "BTC-USDT",
            "minute_ts": int(row.minute_ts),
            "start_ts": int(row.minute_ts),
            "horizon_minutes": 5,
            "direction": "UP",
            "pre_move_features": {
                name: float(row[name]) for name in (
                    "volume_surprise_z", "trades_surprise_z", "taker_imbalance",
                    "book_imbalance5", "book_imbalance20", "microprice_offset_bps",
                    "spread_bps", "open_interest_change_pct", "funding_rate",
                )
            },
        })
    report = mine_patterns(panel, episodes, min_support=2)
    assert report["status"] in {
        "exploratory_patterns_found", "no_patterns_met_support"
    }
    assert report["control_counts_by_direction"]["UP"] > 0
    assert report["warning"].find("same sample") >= 0
    assert all(p["direction"] == "UP" for p in report["patterns"])
    assert all(p["symbol"] == "BTC-USDT" for p in report["patterns"])
    assert all(p["horizon_minutes"] == 5 for p in report["patterns"])
    assert "UTC-hour" in report["control_method"]
    assert all(p["matched_strata_count"] >= 1 for p in report["patterns"])
    assert all(0 <= p["event_match_coverage_pct"] <= 100 for p in report["patterns"])
    assert all(0 <= p["control_match_coverage_pct"] <= 100 for p in report["patterns"])


def test_pattern_mining_handles_no_episodes():
    panel = pd.DataFrame({
        "volume_surprise_z": np.arange(30, dtype=float),
        "trades_surprise_z": np.arange(30, dtype=float) ** 2,
    })
    report = mine_patterns(panel, [])
    assert report["status"] == "no_events"
    assert report["patterns"] == []


def test_controls_exclude_large_moves_in_both_directions_and_event_windows():
    from scripts.market_pattern_mining import _build_control_frame

    rows = 140
    close = np.full(rows, 100.0)
    close[15] = 102.0
    close[35] = 98.0
    panel = pd.DataFrame({
        "symbol": ["BTC-USDT"] * rows,
        "minute_ts": np.arange(rows, dtype=np.int64) * 60_000,
        "close": close,
        "rv_60m": np.full(rows, 0.0001),
        "rv_30m": np.full(rows, 0.0001),
    })
    event_start = int(panel.loc[70, "minute_ts"])
    episodes = [{
        "symbol": "BTC-USDT",
        "start_ts": event_start,
        "horizon_minutes": 5,
        "direction": "UP",
        "pre_move_features": {},
    }]

    controls = _build_control_frame(panel, episodes)
    assert not controls.empty
    assert set(controls["direction"]) == {"UP", "DOWN"}

    timestamps = controls.drop_duplicates(["minute_ts"])["minute_ts"].astype(int)
    for stamp in timestamps:
        end = int(stamp) + 5 * 60_000
        assert not (int(stamp) <= event_start + 5 * 60_000 and end >= event_start)

        idx = int(stamp) // 60_000
        forward_return = panel.loc[idx + 5, "close"] / panel.loc[idx, "close"] - 1
        assert abs(forward_return) < 0.004



def test_pattern_mining_handles_missing_event_volatility_without_crashing():
    rows = 180
    panel = pd.DataFrame({
        "symbol": ["BTC-USDT"] * rows,
        "minute_ts": np.arange(rows, dtype=np.int64) * 60_000,
        "close": np.full(rows, 100.0),
        "rv_60m": np.linspace(0.0001, 0.0003, rows),
        "volume_surprise_z": np.linspace(-2, 2, rows),
        "trades_surprise_z": np.linspace(-1.5, 1.5, rows),
    })
    episodes = [{
        "symbol": "BTC-USDT", "minute_ts": 150 * 60_000,
        "start_ts": 150 * 60_000, "horizon_minutes": 5, "direction": "UP",
        "pre_move_features": {"volume_surprise_z": 1.5, "trades_surprise_z": 1.2},
    }]
    report = mine_patterns(panel, episodes, min_support=1)
    assert report["status"] in {"exploratory_patterns_found", "no_patterns_met_support"}



def test_control_frame_uses_supplied_training_volatility_cuts():
    from scripts.market_pattern_mining import _build_control_frame

    rows = 90
    panel = pd.DataFrame({
        "symbol": ["BTC-USDT"] * rows,
        "minute_ts": np.arange(rows, dtype=np.int64) * 60_000,
        "close": np.full(rows, 100.0),
        "rv_60m": np.linspace(0.0001, 0.0003, rows),
        "rv_30m": np.linspace(0.0001, 0.0003, rows),
    })
    episode = [{
        "symbol": "BTC-USDT", "start_ts": 80 * 60_000,
        "horizon_minutes": 5, "direction": "UP", "pre_move_features": {},
    }]
    controls = _build_control_frame(
        panel, episode, volatility_cuts={"BTC-USDT": [0.00005, 0.00015]}
    )
    assert not controls.empty
    assert set(controls["volatility_bucket"]) == {2}
