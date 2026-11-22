#!/usr/bin/env python3
"""Read-only cross-sectional crypto market research on public BingX USDT-M candles.

This is a research screen, not a live trading strategy or a portfolio backtest.
It deliberately separates signal prediction from executable PnL simulation.
"""
from __future__ import annotations

import argparse
import json
import math
import io
import zipfile
import urllib.error
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

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
BINANCE_ARCHIVE_BASE = "https://data.binance.vision/data/futures/um"
BINANCE_LAST_REQUEST = 0.0


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


def download_archive_bytes(url, retries=3):
    global BINANCE_LAST_REQUEST
    last_error = None
    for attempt in range(retries):
        pause = 0.05 - (time.monotonic() - BINANCE_LAST_REQUEST)
        if pause > 0:
            time.sleep(pause)
        request = urllib.request.Request(
            url, headers={"User-Agent": "gold-bot-binance-public-archive/1.0"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
            BINANCE_LAST_REQUEST = time.monotonic()
            return data
        except urllib.error.HTTPError as exc:
            BINANCE_LAST_REQUEST = time.monotonic()
            if exc.code == 404:
                return None
            last_error = exc
        except Exception as exc:
            BINANCE_LAST_REQUEST = time.monotonic()
            last_error = exc
        if attempt + 1 < retries:
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"Binance archive download failed: {url}: {last_error}") from last_error


def parse_binance_kline_archive(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not names:
            raise RuntimeError("Binance archive contains no CSV")
        with archive.open(names[0]) as handle:
            raw = pd.read_csv(handle, header=None, dtype=str)
    if raw.empty:
        return pd.DataFrame(columns=[
            "time","open","high","low","close","volume","quote_volume",
            "trade_count","taker_buy_base","taker_buy_quote"
        ])
    first = pd.to_numeric(raw.iloc[0, 0], errors="coerce")
    if pd.isna(first):
        raw = raw.iloc[1:].copy()
    if raw.shape[1] < 11:
        raise RuntimeError(f"Unexpected Binance kline CSV width: {raw.shape[1]}")
    selected = raw.iloc[:, [0,1,2,3,4,5,7,8,9,10]].copy()
    selected.columns = [
        "time","open","high","low","close","volume","quote_volume",
        "trade_count","taker_buy_base","taker_buy_quote"
    ]
    for column in selected.columns:
        selected[column] = pd.to_numeric(selected[column], errors="coerce")
    selected = selected.dropna(subset=["time","open","high","low","close"])
    selected["time"] = selected["time"].astype("int64")
    # USD-M futures archives use milliseconds; tolerate microsecond timestamps too.
    selected.loc[selected["time"] > 100_000_000_000_000, "time"] //= 1000
    return selected.drop_duplicates("time").sort_values("time").reset_index(drop=True)


def _month_starts(start_date, end_date):
    cursor = datetime(start_date.year, start_date.month, 1, tzinfo=timezone.utc)
    end_month = datetime(end_date.year, end_date.month, 1, tzinfo=timezone.utc)
    months = []
    while cursor <= end_month:
        months.append(cursor)
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year+1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month+1)
    return months


def _archive_url(partition, symbol, filename):
    return f"{BINANCE_ARCHIVE_BASE}/{partition}/klines/{symbol}/1h/{filename}"


def discover_universe(limit=30, min_quote_volume=5_000_000):
    """Discover today's liquid BingX USDT contracts; Binance archive presence is checked per symbol."""
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
        quote_volume = _number(row, "quoteVolume", "turnover", "quoteVol", "amount", "tradeValue")
        if quote_volume <= 0:
            quote_volume = _number(row, "volume", "vol") * last
        if last <= 0 or quote_volume < min_quote_volume:
            continue
        ranked.append({"symbol": symbol, "quote_volume_24h": quote_volume})
    ranked.sort(key=lambda item: item["quote_volume_24h"], reverse=True)
    return ranked[:limit]


def fetch_klines(symbol, start_ms, end_ms):
    """Fetch BingX candles including quote volume, trade count and taker-buy quote volume."""
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
                    core = [float(row[i]) for i in range(1, 6)]
                    quote_volume = float(row[7]) if len(row) > 7 else core[3]*core[4]
                    trade_count = float(row[8]) if len(row) > 8 else np.nan
                    taker_buy_base = float(row[9]) if len(row) > 9 else np.nan
                    taker_buy_quote = float(row[10]) if len(row) > 10 else np.nan
                elif isinstance(row, dict):
                    stamp = int(float(row.get("time", row.get("openTime", row.get("timestamp", 0)))))
                    core = [float(row[k]) for k in ("open", "high", "low", "close", "volume")]
                    quote_volume = float(row.get("quoteVolume", row.get("quoteAssetVolume", core[3]*core[4])))
                    trade_count = float(row.get("count", row.get("tradeCount", np.nan)))
                    taker_buy_base = float(row.get("takerBuyBaseVolume", np.nan))
                    taker_buy_quote = float(row.get("takerBuyQuoteVolume", np.nan))
                else:
                    continue
                if stamp < 10_000_000_000:
                    stamp *= 1000
                if cursor <= stamp < window_end and all(math.isfinite(v) for v in core):
                    parsed.append((stamp, *core, quote_volume, trade_count, taker_buy_base, taker_buy_quote))
            except (TypeError, ValueError, OverflowError, KeyError):
                continue
        rows.extend(parsed)
        cursor = window_end
        if not batch:
            continue
    frame = pd.DataFrame(
        rows, columns=["time", "open", "high", "low", "close", "volume",
                       "quote_volume", "trade_count", "taker_buy_base", "taker_buy_quote"]
    )
    if frame.empty:
        raise RuntimeError(f"No {INTERVAL} candles returned for {symbol}")
    return frame.drop_duplicates("time").sort_values("time").reset_index(drop=True)


def fetch_binance_klines(symbol, start_ms, end_ms):
    """Fetch Binance USD-M 1h klines from official monthly/daily public ZIP archives."""
    start_date = datetime.fromtimestamp(start_ms/1000, tz=timezone.utc).date()
    end_date = datetime.fromtimestamp((end_ms-1)/1000, tz=timezone.utc).date()
    today_utc = datetime.now(timezone.utc).date()
    last_complete_day = min(end_date, today_utc-timedelta(days=1))
    months = _month_starts(start_date, end_date)
    current_month = datetime(today_utc.year, today_utc.month, 1, tzinfo=timezone.utc)
    complete_months = [m for m in months if m < current_month]
    if not complete_months:
        raise RuntimeError("No completed Binance monthly archive in requested period")
    # Probe the latest completed month first. This filters non-Binance instruments
    # before spending requests on the rest of the historical window.
    probe_month = complete_months[-1]
    probe_name = f"{symbol}-1h-{probe_month.year}-{probe_month.month:02d}.zip"
    probe_data = download_archive_bytes(_archive_url("monthly", symbol, probe_name))
    if probe_data is None:
        raise RuntimeError(f"No Binance USD-M archive for {symbol} in {probe_month:%Y-%m}")
    frames = [parse_binance_kline_archive(probe_data)]
    for month in complete_months[:-1]:
        name = f"{symbol}-1h-{month.year}-{month.month:02d}.zip"
        data = download_archive_bytes(_archive_url("monthly", symbol, name))
        if data is not None:
            frames.append(parse_binance_kline_archive(data))
    if last_complete_day >= current_month.date():
        day = current_month.date()
        while day <= last_complete_day:
            name = f"{symbol}-1h-{day.isoformat()}.zip"
            data = download_archive_bytes(_archive_url("daily", symbol, name))
            if data is not None:
                frames.append(parse_binance_kline_archive(data))
            day += timedelta(days=1)
    frame = pd.concat(frames, ignore_index=True).drop_duplicates("time").sort_values("time")
    frame = frame.loc[(frame["time"] >= start_ms) & (frame["time"] < end_ms)].copy()
    if frame.empty:
        raise RuntimeError(f"No Binance {INTERVAL} archive rows returned for {symbol}")
    return frame.reset_index(drop=True)


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
    x["weekday_sin"] = np.sin(2*np.pi*dt.dt.day_of_week/7)
    x["weekday_cos"] = np.cos(2*np.pi*dt.dt.day_of_week/7)
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


BINGX_FLOW_FEATURES = [
    "bingx_taker_imbalance_lag1", "bingx_taker_imbalance_4h_lag1",
    "bingx_quote_volume_z_lag1", "bingx_trade_count_z_lag1",
]


def add_bingx_flow_features(frame):
    """Causal, prior-completed-candle order-flow proxies from BingX klines."""
    x = frame.sort_values("time").copy()
    close = x["close"].astype(float)
    quote = x.get("quote_volume", close*x["volume"].astype(float)).astype(float)
    trades = x.get("trade_count", pd.Series(np.nan, index=x.index)).astype(float)
    taker_buy = x.get("taker_buy_quote", pd.Series(np.nan, index=x.index)).astype(float)
    imbalance = (2*taker_buy/quote.replace(0, np.nan))-1
    quote_mean = quote.rolling(24, min_periods=18).mean()
    quote_std = quote.rolling(24, min_periods=18).std()
    trade_mean = trades.rolling(24, min_periods=18).mean()
    trade_std = trades.rolling(24, min_periods=18).std()
    out = pd.DataFrame(index=x["time"])
    out["bingx_taker_imbalance_lag1"] = imbalance.shift(1).to_numpy()
    out["bingx_taker_imbalance_4h_lag1"] = imbalance.rolling(4, min_periods=3).mean().shift(1).to_numpy()
    out["bingx_quote_volume_z_lag1"] = ((quote-quote_mean)/quote_std.replace(0, np.nan)).shift(1).to_numpy()
    out["bingx_trade_count_z_lag1"] = ((trades-trade_mean)/trade_std.replace(0, np.nan)).shift(1).to_numpy()
    return out


BASE_FEATURES = [
    "ret_1h", "ret_3h", "ret_6h", "ret_12h", "ret_24h", "ret_72h", "ret_168h",
    "rv_24h", "rv_72h", "rv_ratio", "atr_pct_14", "ema_gap_12_26",
    "ema_slope_12_6", "rsi_14", "bb_width_20", "range_pos_24", "range_pos_72",
    "volume_z_24", "volume_ratio_24", "body_pct_range", "upper_wick_pct",
    "lower_wick_pct", "close_location", "hour_sin", "hour_cos",
    "weekday_sin", "weekday_cos", "btc_ret_1h", "relative_ret_1h",
    "relative_ret_24h", "btc_corr_72h",
]
CROSS_VENUE_FEATURES = [
    "binance_ret_1h_lag1", "binance_ret_4h_lag1",
    "binance_taker_imbalance_lag1", "binance_taker_imbalance_4h_lag1",
    "binance_quote_volume_z_lag1", "binance_trade_count_z_lag1",
    "cross_venue_return_gap_lag1",
]
FEATURES = BASE_FEATURES + CROSS_VENUE_FEATURES
BINGX_FEATURES = BASE_FEATURES + BINGX_FLOW_FEATURES


def add_cross_venue_features(bingx_frame, binance_frame):
    b = binance_frame.sort_values("time").set_index("time")
    close = b["close"].astype(float)
    quote_volume = b["quote_volume"].astype(float)
    trades = b["trade_count"].astype(float)
    quote = quote_volume.replace(0, np.nan)
    taker_imbalance = (2*b["taker_buy_quote"].astype(float)/quote)-1
    vol_mean = quote_volume.rolling(24, min_periods=18).mean()
    vol_std = quote_volume.rolling(24, min_periods=18).std()
    trade_mean = trades.rolling(24, min_periods=18).mean()
    trade_std = trades.rolling(24, min_periods=18).std()
    binance_ret_1h = close.pct_change()
    binance_ret_4h = close.pct_change(4)
    bingx_ret_1h = bingx_frame.sort_values("time").set_index("time")["close"].pct_change()
    features = pd.DataFrame(index=b.index)
    features["binance_ret_1h_lag1"] = binance_ret_1h.shift(1)
    features["binance_ret_4h_lag1"] = binance_ret_4h.shift(1)
    features["binance_taker_imbalance_lag1"] = taker_imbalance.shift(1)
    features["binance_taker_imbalance_4h_lag1"] = taker_imbalance.rolling(4, min_periods=3).mean().shift(1)
    features["binance_quote_volume_z_lag1"] = ((quote_volume-vol_mean)/vol_std.replace(0, np.nan)).shift(1)
    features["binance_trade_count_z_lag1"] = ((trades-trade_mean)/trade_std.replace(0, np.nan)).shift(1)
    features["cross_venue_return_gap_lag1"] = (binance_ret_1h-bingx_ret_1h).shift(1)
    return features


def build_panel(frames, cost, binance_frames):
    btc = frames.get("BTC-USDT")
    btc_returns = None
    btc_context = None
    if btc is not None:
        btc_indexed = btc.sort_values("time").set_index("time")
        btc_returns = btc_indexed["close"].pct_change()
        btc_context = pd.DataFrame(index=btc_indexed.index)
        btc_context["btc_ret_24h"] = btc_indexed["close"].pct_change(24)
        btc_context["btc_rv_24h"] = (
            btc_returns.rolling(24, min_periods=18).std() * math.sqrt(24)
        )
    pieces = []
    for symbol, raw in frames.items():
        f = add_features(raw, btc_returns).set_index("time")
        flow = add_bingx_flow_features(raw)
        f = f.join(flow, how="left")
        if symbol in binance_frames:
            cross = add_cross_venue_features(raw, binance_frames[symbol])
            f = f.join(cross, how="left")
        else:
            for column in CROSS_VENUE_FEATURES:
                f[column] = np.nan
        if btc_context is not None:
            f = f.join(btc_context, how="left")
        else:
            f["btc_ret_24h"] = np.nan
            f["btc_rv_24h"] = np.nan
        f = f.reset_index()
        f["symbol"] = symbol
        for horizon in (1, 4, 12, 24):
            future = f["close"].shift(-horizon)/f["close"]-1
            f[f"fwd_{horizon}h"] = future
            # Binary direction target for every completed outcome. Do not filter
            # samples by whether the FUTURE move exceeded transaction costs:
            # that would create outcome-dependent selection / look-ahead bias.
            f[f"label_{horizon}h"] = np.where(
                future.notna(), (future > 0).astype(float), np.nan
            )
        pieces.append(f)
    panel = pd.concat(pieces, ignore_index=True)
    return panel.replace([np.inf, -np.inf], np.nan)


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


def _select_threshold(y_true, probabilities, forward_returns, cost, fallback):
    """Choose a symmetric probability threshold on validation only."""
    candidates = [round(x, 2) for x in np.arange(0.52, 0.751, 0.02)]
    minimum_signals = max(50, int(len(y_true) * 0.005))
    scored = []
    for threshold in candidates:
        metrics = _metrics(y_true, probabilities, forward_returns, cost, threshold)
        if metrics["signals"] < minimum_signals:
            continue
        # Conservative selection: mean net return minus one standard error.
        values = np.asarray(forward_returns, dtype=float)
        p = np.asarray(probabilities, dtype=float)
        direction = np.where(p >= threshold, 1, np.where(p <= 1-threshold, -1, 0))
        net = direction[direction != 0] * values[direction != 0] - cost
        score = float(net.mean() - net.std(ddof=1) / math.sqrt(len(net))) if len(net) > 1 else -math.inf
        scored.append((score, threshold, metrics))
    if not scored:
        return fallback, {"method": "fixed_fallback_no_validation_threshold_met",
                          "threshold": fallback, "minimum_signals": minimum_signals}
    score, threshold, metrics = max(scored, key=lambda item: (item[0], item[1]))
    return threshold, {
        "method": "validation_mean_net_return_minus_one_standard_error",
        "threshold": threshold,
        "minimum_signals": minimum_signals,
        "validation_score_pct": round(score*100, 5),
        "validation_metrics": metrics,
    }


def _select_available_features(panel, candidates, training_end_ms, min_coverage=0.60):
    """Select usable columns from the training period only; never discard rows globally."""
    training = panel.loc[panel["time"] < training_end_ms, candidates]
    coverage = training.notna().mean()
    selected = [
        name for name in candidates
        if float(coverage.get(name, 0.0)) >= min_coverage
        and training[name].nunique(dropna=True) > 1
    ]
    report = {
        name: round(float(coverage.get(name, 0.0)), 4)
        for name in candidates
    }
    return selected, report


def _market_regime_breakdown(test, probabilities, threshold, cost):
    """Report held-out outcomes by BTC trend and volatility regime."""
    out = {}
    for regime, group in test.assign(_probability=probabilities).groupby("market_regime"):
        metrics = _metrics(
            group["label"].to_numpy(),
            group["_probability"].to_numpy(),
            group["forward_return"].to_numpy(),
            cost,
            threshold,
        )
        out[str(regime)] = metrics
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
    binance_frames = {}
    for item in universe:
        symbol = item["symbol"]
        binance_symbol = symbol.replace("-", "")
        print(f"FETCH {symbol} on BingX and Binance", flush=True)
        try:
            raw = fetch_klines(symbol, start, now)
            bn_raw = None if args.bingx_only else fetch_binance_klines(binance_symbol, start, now)
        except Exception as exc:
            print(f"SKIP_DATA_ERROR {symbol}: {exc}", flush=True)
            continue
        # Exclude the currently forming hourly candle.
        raw = raw.loc[raw["time"]+INTERVAL_MS <= now].copy().reset_index(drop=True)
        if not args.bingx_only and bn_raw is not None:
            bn_raw = bn_raw.loc[bn_raw["time"]+INTERVAL_MS <= now].copy().reset_index(drop=True)
        if len(raw) < args.days*24*0.9 or (bn_raw is not None and len(bn_raw) < args.days*24*0.9):
            print(f"SKIP_INCOMPLETE {symbol} bingx={len(raw)} binance={len(bn_raw) if bn_raw is not None else 'not_requested'}", flush=True)
            continue
        frames[symbol] = raw
        if bn_raw is not None:
            binance_frames[symbol] = bn_raw
        print(f"FETCHED {symbol} bingx={len(raw)} binance={len(bn_raw) if bn_raw is not None else 'not_requested'}", flush=True)
    if len(frames) < 5:
        raise RuntimeError(f"Only {len(frames)} symbols have sufficient history; need at least 5")
    panel = build_panel(frames, args.cost_bps/10_000, binance_frames)
    active_features = BINGX_FEATURES if args.bingx_only else FEATURES
    evaluation_start = now - args.days*86_400_000
    panel = panel.loc[panel["time"] >= evaluation_start].copy()
    # Keep rows with partially missing optional fields. A single unsupported API
    # field must not erase the entire cross-sectional history; the train-only
    # median imputer handles gaps after low-coverage columns are excluded.
    unique_times = np.sort(panel["time"].unique())
    if len(unique_times) < 24*30:
        raise RuntimeError(f"Insufficient aligned timestamps before feature filtering: {len(unique_times)}")
    cut1 = int(np.quantile(unique_times, 0.60))
    cut2 = int(np.quantile(unique_times, 0.80))
    requested_features = list(active_features)
    active_features, feature_coverage = _select_available_features(
        panel, requested_features, cut1, min_coverage=0.60
    )
    if len(active_features) < 8:
        raise RuntimeError(
            "Insufficient usable features from training data: "
            f"{len(active_features)}; coverage={feature_coverage}"
        )
    print(
        "FEATURE_COVERAGE="
        + json.dumps({"selected": active_features, "coverage": feature_coverage}, ensure_ascii=False),
        flush=True,
    )
    train_context = panel.loc[panel["time"] < cut1, "btc_rv_24h"].dropna()
    btc_vol_cut = float(train_context.quantile(0.70)) if not train_context.empty else math.inf
    panel["btc_trend_regime"] = np.select(
        [panel["btc_ret_24h"] >= 0.01, panel["btc_ret_24h"] <= -0.01],
        ["up", "down"], default="sideways"
    )
    panel["btc_vol_regime"] = np.where(panel["btc_rv_24h"] >= btc_vol_cut, "high_vol", "normal_vol")
    panel["market_regime"] = panel["btc_trend_regime"] + "_" + panel["btc_vol_regime"]
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
        X_train, y_train = train[active_features], train[label_col].astype(int)
        X_valid, y_valid = valid[active_features], valid[label_col].astype(int)
        X_test, y_test = test[active_features], test[label_col].astype(int)
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
            classes = list(model[-1].classes_)
            valid_proba = model.predict_proba(X_valid)[:, classes.index(1)] if len(X_valid) else np.array([])
            p_valid = valid_proba
            selected_threshold, threshold_report = _select_threshold(
                y_valid.to_numpy(), p_valid, valid[ret_col].to_numpy(),
                args.cost_bps/10_000, args.threshold
            ) if len(X_valid) else (args.threshold, {"method":"fixed_fallback_no_validation_rows","threshold":args.threshold})
            proba = model.predict_proba(X_test)
            p_up = proba[:, classes.index(1)]
            costs = {}
            for bps in (args.cost_bps, 20, 30):
                c = bps/10_000
                costs[str(bps)+"bps"] = _metrics(
                    y_test.to_numpy(), p_up, test[ret_col].to_numpy(), c, selected_threshold
                )
            test_for_regime = test.assign(
                label=y_test.to_numpy(), forward_return=test[ret_col].to_numpy()
            )
            regime_breakdown = _market_regime_breakdown(
                test_for_regime, p_up, selected_threshold, args.cost_bps/10_000
            )
            horizon_result["models"][name] = {
                "threshold_selection": threshold_report,
                "cost_sensitivity":costs,
                "market_regime_breakdown":regime_breakdown,
            }
            if name == "logistic":
                coefs = model[-1].coef_[0]
                horizon_result["models"][name]["largest_abs_coefficients"] = [
                    {"feature":active_features[i],"coefficient":round(float(coefs[i]),5)}
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
        "method": ("BingX-only public 1h research" if args.bingx_only else "Read-only cross-venue BingX+Binance USDT-M 1h research") + "; time-ordered 60/20/20 split with horizon purge; validation-only threshold selection; held-out BTC trend/volatility regime breakdown; logistic and histogram gradient boosting; no execution/portfolio simulation.",
        "evaluation_days": args.days,
        "requested_universe": args.universe,
        "actual_symbols": list(frames),
        "universe_candidates": len(universe),
        "symbol_quote_volume_24h": [item for item in universe if item["symbol"] in frames],
        "features": active_features,\n        "requested_features": requested_features,\n        "training_feature_coverage": feature_coverage,\n        "feature_selection_rule": "At least 60% non-null coverage and non-constant values in the earliest 60% training period; missing rows imputed inside each model pipeline.",
        "market_data_source": "BingX public USDT-M klines including quote volume, trade count and taker-buy quote volume; prior completed candle flow features only.",
        "cross_venue_data_source": None if args.bingx_only else "Binance Vision USD-M public 1h monthly/daily ZIP archives; research-only under published non-commercial terms.",
        "label_cost_bps": args.cost_bps,\n        "target_definition": "Binary up/down direction on every row with a known future return; no samples are excluded based on future move size. Costs are applied only in validation/test net-return metrics.",
        "signal_probability_threshold": args.threshold,
        "data_limitations": [
            "Current listed contracts only: survivorship/listing-history bias remains.",
            ("BingX hourly candle flow fields are aggregate proxies, not full historical trade-by-trade order flow or historical order-book replay." if args.bingx_only else "Binance Vision historical candle flow fields are aggregate proxies, not full historical trade-by-trade order flow or historical order-book replay."),
            "Historical liquidations and long-run open-interest snapshots are not reconstructed.",
            "No portfolio sizing, overlapping-signal capital allocation, funding, exchange-specific fills or liquidation simulation in this screening report.",
            "Model scores are research diagnostics, not trading probabilities until calibrated and independently replicated.",
            "Thresholds are selected on validation only; final reported performance is on a later held-out test segment.",
            "BTC trend regimes use +/-1% 24h return; high-volatility cutoff is the training-period 70th percentile of BTC 24h realized volatility.",
            ("This BingX-only research path does not depend on Binance Vision archives." if args.bingx_only else "Binance Vision archive-derived results are restricted to non-commercial, non-production research under published terms.")
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
    parser.add_argument("--bingx-only", action="store_true", help="Use only BingX data; no Binance Vision archives")
    parser.add_argument("--output-dir", default="research-output")
    args = parser.parse_args()
    if not 30 <= args.days <= 365:
        parser.error("--days must be between 30 and 365")
    if not 5 <= args.universe <= 100:
        parser.error("--universe must be between 5 and 100")
    if not 0.01 <= args.threshold <= 0.99:
        parser.error("--threshold must be between 0.50 and 0.90")
    run_research(args)
