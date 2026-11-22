#!/usr/bin/env python3
"""Read-only cross-sectional crypto market research on public BingX USDT-M candles.

This is a research screen, not a live trading strategy or a portfolio backtest.
It deliberately separates signal prediction from executable PnL simulation.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import numpy as np
import pandas as pd

BASE = "https://open-api.bingx.com"
TICKER_PATH = "/openApi/swap/v2/quote/ticker"
KLINE_PATH = "/openApi/swap/v3/quote/klines"
INTERVAL = "1h"
INTERVAL_MS = 3_600_000
PAGE_CANDLES = 800
REQUEST_PAUSE = 1.05
LAST_REQUEST = 0.0
STABLE_BASES = {"USDC", "USDE", "FDUSD", "TUSD", "USDP", "DAI", "BUSD"}


def _rows(payload):
    data = payload.get("data", payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "list", "rows", "tickers", "contracts"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [data] if data else []
    return []


def get_json(path, params=None, retries=4):
    global LAST_REQUEST
    params = params or {}
    query = urllib.parse.urlencode(params)
    url = BASE + path + ("?" + query if query else "")
    last_error = None
    for attempt in range(retries):
        pause = REQUEST_PAUSE - (time.monotonic() - LAST_REQUEST)
        if pause > 0:
            time.sleep(pause)
        request = urllib.request.Request(
            url, headers={"User-Agent": "gold-bot-market-research/1.0", "X-SOURCE-KEY": "BX-AI-SKILL"}
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read().decode("utf-8")
            LAST_REQUEST = time.monotonic()
            payload = json.loads(raw)
            code = payload.get("code", 0)
            if code not in (0, "0", None):
                raise RuntimeError(f"BingX API error {code}: {payload.get('msg')}")
            return payload
        except Exception as exc:
            LAST_REQUEST = time.monotonic()
            last_error = exc
            if attempt + 1 >= retries:
                break
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Request failed: {url}: {last_error}") from last_error


def _number(row, *keys):
    for key in keys:
        value = row.get(key)
        try:
            number = float(value)
            if math.isfinite(number):
                return number
        except (TypeError, ValueError):
            pass
    return 0.0


def discover_universe(limit=30, min_quote_volume=5_000_000):
    """Discover today's liquid USDT perpetuals. This is not a historical universe."""
    rows = _rows(get_json(TICKER_PATH))
    ranked = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).upper()
        if not symbol.endswith("-USDT"):
            continue
        base = symbol.split("-", 1)[0]
        if base in STABLE_BASES or not base:
            continue
        last = _number(row, "lastPrice", "last", "close")
        quote_volume = _number(
            row, "quoteVolume", "turnover", "quoteVol", "amount", "tradeValue"
        )
        if quote_volume <= 0:
            quote_volume = _number(row, "volume", "vol") * last
        if last <= 0 or quote_volume < min_quote_volume:
            continue
        ranked.append({"symbol": symbol, "quote_volume_24h": quote_volume})
    ranked.sort(key=lambda item: item["quote_volume_24h"], reverse=True)
    return ranked[:limit]


def fetch_klines(symbol, start_ms, end_ms):
    rows = []
    cursor = int(start_ms)
    while cursor < end_ms:
        window_end = min(cursor + PAGE_CANDLES * INTERVAL_MS, end_ms)
        payload = get_json(
            KLINE_PATH,
            {"symbol": symbol, "interval": INTERVAL,
             "startTime": cursor, "endTime": window_end, "limit": 1000},
        )
        batch = _rows(payload)
        parsed = []
        for row in batch:
            try:
                if isinstance(row, (list, tuple)) and len(row) >= 6:
                    stamp = int(float(row[0]))
                    values = [float(row[i]) for i in range(1, 6)]
                elif isinstance(row, dict):
                    stamp = int(float(row.get("time", row.get("openTime", row.get("timestamp", 0)))))
                    values = [float(row[k]) for k in ("open", "high", "low", "close", "volume")]
                else:
                    continue
                if stamp < 10_000_000_000:
                    stamp *= 1000
                if cursor <= stamp < window_end and all(math.isfinite(v) for v in values):
                    parsed.append((stamp, *values))
            except (TypeError, ValueError, OverflowError, KeyError):
                continue
        rows.extend(parsed)
        cursor = window_end
        if not batch:
            continue
    frame = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    if frame.empty:
        raise RuntimeError(f"No {INTERVAL} candles returned for {symbol}")
    return (frame.drop_duplicates("time").sort_values("time")
            .reset_index(drop=True))


def add_features(frame, btc_returns=None):
    x = frame.copy()
    close = x["close"].astype(float)
    high = x["high"].astype(float)
    low = x["low"].astype(float)
    open_ = x["open"].astype(float)
    volume = x["volume"].astype(float)
    ret = close.pct_change()
    for n in (1, 3, 6, 12, 24, 72, 168):
        x[f"ret_{n}h"] = close.pct_change(n)
    x["rv_24h"] = ret.rolling(24, min_periods=18).std() * math.sqrt(24)
    x["rv_72h"] = ret.rolling(72, min_periods=54).std() * math.sqrt(72)
    x["rv_ratio"] = x["rv_24h"] / x["rv_72h"].replace(0, np.nan)
    tr = pd.concat([(high-low), (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    x["atr_pct_14"] = tr.rolling(14, min_periods=10).mean() / close
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    x["ema_gap_12_26"] = (ema12-ema26) / close
    x["ema_slope_12_6"] = ema12.pct_change(6)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    x["rsi_14"] = 100 - 100/(1+rs)
    mid = close.rolling(20, min_periods=15).mean()
    sd = close.rolling(20, min_periods=15).std()
    x["bb_width_20"] = 4*sd/mid.replace(0, np.nan)
    x["range_pos_24"] = (close-low.rolling(24, min_periods=18).min()) / (
        high.rolling(24, min_periods=18).max()-low.rolling(24, min_periods=18).min()
    ).replace(0, np.nan)
    x["range_pos_72"] = (close-low.rolling(72, min_periods=54).min()) / (
        high.rolling(72, min_periods=54).max()-low.rolling(72, min_periods=54).min()
    ).replace(0, np.nan)
    vol_mean = volume.rolling(24, min_periods=18).mean()
    vol_std = volume.rolling(24, min_periods=18).std()
    x["volume_z_24"] = (volume-vol_mean)/vol_std.replace(0, np.nan)
    x["volume_ratio_24"] = volume/vol_mean.replace(0, np.nan)
    candle_range = (high-low).replace(0, np.nan)
    x["body_pct_range"] = (close-open_)/candle_range
    x["upper_wick_pct"] = (high-pd.concat([open_, close], axis=1).max(axis=1))/candle_range
    x["lower_wick_pct"] = (pd.concat([open_, close], axis=1).min(axis=1)-low)/candle_range
    x["close_location"] = (close-low)/candle_range
    dt = pd.to_datetime(x["time"], unit="ms", utc=True)
    x["hour_sin"] = np.sin(2*np.pi*dt.dt.hour/24)
    x["hour_cos"] = np.cos(2*np.pi*dt.dt.hour/24)
    x["weekday_sin"] = np.sin(2*np.pi*dt.dt.dayofweek/7)
    x["weekday_cos"] = np.cos(2*np.pi*dt.dt.dayofweek/7)
    if btc_returns is not None:
        btc = pd.Series(btc_returns.values, index=btc_returns.index)
        aligned = pd.Series(x["time"].map(btc), index=x.index)
        x["btc_ret_1h"] = aligned
        x["relative_ret_1h"] = x["ret_1h"]-aligned
        x["relative_ret_24h"] = x["ret_24h"] - (
            x["time"].map(btc_returns.rolling(24, min_periods=18).sum())
        )
        x["btc_corr_72h"] = pd.Series(ret).rolling(72, min_periods=54).corr(aligned)
    return x


FEATURES = [
    "ret_1h", "ret_3h", "ret_6h", "ret_12h", "ret_24h", "ret_72h", "ret_168h",
    "rv_24h", "rv_72h", "rv_ratio", "atr_pct_14", "ema_gap_12_26",
    "ema_slope_12_6", "rsi_14", "bb_width_20", "range_pos_24", "range_pos_72",
    "volume_z_24", "volume_ratio_24", "body_pct_range", "upper_wick_pct",
    "lower_wick_pct", "close_location", "hour_sin", "hour_cos",
    "weekday_sin", "weekday_cos", "btc_ret_1h", "relative_ret_1h",
    "relative_ret_24h", "btc_corr_72h",
]


def build_panel(frames, cost):
    btc = frames.get("BTC-USDT")
    btc_returns = None
    if btc is not None:
        btc_returns = btc.set_index("time")["close"].pct_change()
    pieces = []
    for symbol, raw in frames.items():
        f = add_features(raw, btc_returns)
        f["symbol"] = symbol
        for horizon in (1, 4, 12, 24):
            future = f["close"].shift(-horizon)/f["close"]-1
            f[f"fwd_{horizon}h"] = future
            f[f"label_{horizon}h"] = np.where(
                future > cost, 1.0, np.where(future < -cost, 0.0, np.nan)
            )
        pieces.append(f)
    panel = pd.concat(pieces, ignore_index=True)
    panel = panel.replace([np.inf, -np.inf], np.nan)
    return panel


def _metrics(y_true, probabilities, forward_returns, cost, threshold):
    y = np.asarray(y_true, dtype=int)
    p = np.asarray(probabilities, dtype=float)
    r = np.asarray(forward_returns, dtype=float)
    pred = (p >= 0.5).astype(int)
    out = {
        "observations": int(len(y)),
        "direction_accuracy_pct": round(float((pred == y).mean()*100), 2) if len(y) else None,
        "baseline_majority_accuracy_pct": round(float(max((y==0).mean(), (y==1).mean())*100), 2) if len(y) else None,
        "brier_score": round(float(np.mean((p-y)**2)), 5) if len(y) else None,
    }
    direction = np.where(p >= threshold, 1, np.where(p <= 1-threshold, -1, 0))
    mask = direction != 0
    net = direction[mask]*r[mask]-cost
    out.update({
        "trade_threshold": threshold,
        "signals": int(mask.sum()),
        "coverage_pct": round(float(mask.mean()*100), 2) if len(mask) else 0,
        "signal_direction_accuracy_pct": round(float((direction[mask] == np.where(r[mask] > 0, 1, -1)).mean()*100), 2) if mask.any() else None,
        "mean_net_return_pct": round(float(net.mean()*100), 5) if len(net) else None,
        "median_net_return_pct": round(float(np.median(net)*100), 5) if len(net) else None,
        "positive_net_signal_pct": round(float((net > 0).mean()*100), 2) if len(net) else None,
        "net_profit_factor": round(float(net[net>0].sum()/abs(net[net<0].sum())), 4) if (net<0).any() and (net>0).any() else None,
    })
    return out


def run_research(args):
    now = int(time.time()*1000)
    # Extra history is fetched for indicator warm-up; only the requested period is evaluated.
    start = now - (args.days+10)*86_400_000
    universe = discover_universe(args.universe, args.min_quote_volume)
    if not universe:
        raise RuntimeError("BingX returned no eligible USDT perpetuals")
    print(f"UNIVERSE_COUNT={len(universe)}", flush=True)
    print("UNIVERSE="+",".join(x["symbol"] for x in universe), flush=True)
    frames = {}
    for item in universe:
        symbol = item["symbol"]
        print(f"FETCH {symbol}", flush=True)
        raw = fetch_klines(symbol, start, now)
        # Exclude the currently forming hourly candle.
        raw = raw.loc[raw["time"]+INTERVAL_MS <= now].copy().reset_index(drop=True)
        if len(raw) < args.days*24*0.9:
            print(f"SKIP_INCOMPLETE {symbol} rows={len(raw)}", flush=True)
            continue
        frames[symbol] = raw
        print(f"FETCHED {symbol} rows={len(raw)}", flush=True)
    if len(frames) < 5:
        raise RuntimeError(f"Only {len(frames)} symbols have sufficient history; need at least 5")
    panel = build_panel(frames, args.cost_bps/10_000)
    evaluation_start = now - args.days*86_400_000
    panel = panel.loc[panel["time"] >= evaluation_start].copy()
    panel = panel.dropna(subset=FEATURES, how="any")
    unique_times = np.sort(panel["time"].unique())
    if len(unique_times) < 24*30:
        raise RuntimeError(f"Insufficient aligned history after features: {len(unique_times)} timestamps")
    cut1 = int(np.quantile(unique_times, 0.60))
    cut2 = int(np.quantile(unique_times, 0.80))
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.ensemble import HistGradientBoostingClassifier

    results = {}
    for horizon in (1, 4, 12, 24):
        label_col, ret_col = f"label_{horizon}h", f"fwd_{horizon}h"
        purge_ms = horizon*INTERVAL_MS
        train = panel.loc[(panel["time"] < cut1-purge_ms) & panel[label_col].notna()]
        valid = panel.loc[(panel["time"] >= cut1) & (panel["time"] < cut2-purge_ms) & panel[label_col].notna()]
        test = panel.loc[(panel["time"] >= cut2) & panel[label_col].notna()]
        if len(train) < 500 or len(test) < 100:
            results[f"{horizon}h"] = {"status":"insufficient_samples","train":len(train),"validation":len(valid),"test":len(test)}
            continue
        X_train, y_train = train[FEATURES], train[label_col].astype(int)
        X_test, y_test = test[FEATURES], test[label_col].astype(int)
        models = {
            "logistic": make_pipeline(
                SimpleImputer(strategy="median"), StandardScaler(),
                LogisticRegression(max_iter=1200, class_weight="balanced", C=0.2)
            ),
            "hist_gradient_boosting": make_pipeline(
                SimpleImputer(strategy="median"),
                HistGradientBoostingClassifier(
                    max_iter=120, learning_rate=0.05, max_leaf_nodes=15,
                    l2_regularization=2.0, random_state=17
                )
            ),
        }
        horizon_result = {
            "train_rows": int(len(train)), "validation_rows": int(len(valid)),
            "test_rows": int(len(test)), "test_symbols": int(test["symbol"].nunique()),
            "train_period_utc": [
                datetime.fromtimestamp(train["time"].min()/1000, tz=timezone.utc).isoformat(),
                datetime.fromtimestamp(train["time"].max()/1000, tz=timezone.utc).isoformat()
            ],
            "test_period_utc": [
                datetime.fromtimestamp(test["time"].min()/1000, tz=timezone.utc).isoformat(),
                datetime.fromtimestamp(test["time"].max()/1000, tz=timezone.utc).isoformat()
            ],
            "models": {},
        }
        for name, model in models.items():
            model.fit(X_train, y_train)
            proba = model.predict_proba(X_test)
            classes = list(model[-1].classes_)
            p_up = proba[:, classes.index(1)]
            costs = {}
            for bps in (args.cost_bps, 20, 30):
                c = bps/10_000
                costs[str(bps)+"bps"] = _metrics(
                    y_test.to_numpy(), p_up, test[ret_col].to_numpy(), c, args.threshold
                )
            horizon_result["models"][name] = {"cost_sensitivity":costs}
            if name == "logistic":
                coefs = model[-1].coef_[0]
                horizon_result["models"][name]["largest_abs_coefficients"] = [
                    {"feature":FEATURES[i],"coefficient":round(float(coefs[i]),5)}
                    for i in np.argsort(np.abs(coefs))[::-1][:10]
                ]
        # A transparent momentum baseline, evaluated on the same test rows.
        momentum = np.where(test["ret_24h"].to_numpy() >= 0, 1, 0)
        baseline_p = np.where(momentum == 1, 0.500001, 0.499999)
        baseline = _metrics(
            y_test.to_numpy(), baseline_p, test[ret_col].to_numpy(),
            args.cost_bps/10_000, 0.500001
        )
        horizon_result["momentum_24h_baseline"] = baseline
        results[f"{horizon}h"] = horizon_result

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "method": "Read-only BingX USDT-M 1h OHLCV cross-sectional research; current listed universe ranked by current 24h quote volume; time-ordered 60/20/20 split with horizon purge; logistic and histogram gradient boosting; no execution/portfolio simulation.",
        "evaluation_days": args.days,
        "requested_universe": args.universe,
        "actual_symbols": list(frames),
        "symbol_quote_volume_24h": universe,
        "features": FEATURES,
        "label_cost_bps": args.cost_bps,
        "signal_probability_threshold": args.threshold,
        "data_limitations": [
            "Current listed contracts only: survivorship/listing-history bias remains.",
            "OHLCV only: historical order book, trade-by-trade aggressor flow, liquidations and open-interest snapshots are not reconstructed.",
            "No portfolio sizing, overlapping-signal capital allocation, funding, exchange-specific fills or liquidation simulation in this screening report.",
            "Model scores are research diagnostics, not trading probabilities until calibrated and independently replicated."
        ],
        "results": results,
    }
    os.makedirs(args.output_dir, exist_ok=True)
    path = os.path.join(args.output_dir, "market_research_180d.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print("MARKET_RESEARCH_REPORT_BEGIN")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("MARKET_RESEARCH_REPORT_END")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("# Cross-sectional crypto research\n\n")
            f.write(f"Universe: {len(frames)} current liquid USDT perps; evaluation: {args.days} days; 1h candles.\n\n")
            f.write("| Horizon | Model | Test rows | Signals | Mean net return | Direction accuracy |\n|---|---|---:|---:|---:|---:|\n")
            for horizon, item in results.items():
                for model_name, model_result in item.get("models", {}).items():
                    m = model_result["cost_sensitivity"].get(str(args.cost_bps)+"bps", {})
                    f.write(f"| {horizon} | {model_name} | {item['test_rows']} | {m.get('signals')} | {m.get('mean_net_return_pct')}% | {m.get('direction_accuracy_pct')}% |\n")
            f.write("\n**Limitations:** current universe only; OHLCV only; no order-book replay or executable portfolio simulation.\n")
    print(f"REPORT_PATH={path}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--universe", type=int, default=30)
    parser.add_argument("--min-quote-volume", type=float, default=5_000_000)
    parser.add_argument("--cost-bps", type=float, default=12.0)
    parser.add_argument("--threshold", type=float, default=0.58)
    parser.add_argument("--output-dir", default="research-output")
    args = parser.parse_args()
    if not 30 <= args.days <= 365:
        parser.error("--days must be between 30 and 365")
    if not 5 <= args.universe <= 100:
        parser.error("--universe must be between 5 and 100")
    if not 0.01 <= args.threshold <= 0.99:
        parser.error("--threshold must be between 0.50 and 0.90")
    run_research(args)
