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


def test_pattern_mining_handles_no_episodes():
    panel = pd.DataFrame({
        "volume_surprise_z": np.arange(30, dtype=float),
        "trades_surprise_z": np.arange(30, dtype=float) ** 2,
    })
    report = mine_patterns(panel, [])
    assert report["status"] == "no_events"
    assert report["patterns"] == []
