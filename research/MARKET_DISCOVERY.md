# Market discovery research (experimental)

This branch is isolated from production. It does not change `main.py` runtime behavior, use credentials, access the SQLite database, or place orders.

## What the first research pass does

- Queries BingX's public USDT-M ticker and discovers the currently most liquid USDT perpetual contracts.
- Downloads 180 days of completed 1-hour OHLCV candles (plus a warm-up window).
- Builds a broad feature set: multi-horizon returns, realized volatility, ATR, EMA spread/slope, RSI, Bollinger width, range location, volume surprise, candle anatomy, UTC time seasonality, and BTC-relative returns/correlation.
- Creates forward-return labels at 1h, 4h, 12h, and 24h. A neutral band around zero is excluded using a configurable round-trip cost hurdle.
- Uses chronological 60/20/20 train/validation/test partitions and purges observations near split boundaries by the forecast horizon.
- Compares a regularized logistic model and histogram gradient boosting with a simple 24-hour momentum baseline.
- Reports direction accuracy, coverage, Brier score, mean/median net signal return, and sensitivity to 12/20/30 bps round-trip costs.

## Important interpretation limits

This is a research screen, not a production strategy and not an executable portfolio backtest. Hourly forecasts overlap; the reported signal returns do not yet model shared capital, simultaneous positions, funding payments, exchange minimums, market impact, or liquidation. A positive average in this screen is not enough to enable trading.

The universe is ranked using today's 24-hour volume. It therefore has survivorship/listing-history bias: contracts delisted during the 180-day period are not included. A later pass must reconstruct a point-in-time historical universe before claiming a market-wide result.

The first pass uses OHLCV only. Historical order-book snapshots, trade aggressor flow, liquidation events and open-interest snapshots are not reconstructed from candles. BingX exposes public endpoints for current depth/trades, funding history and current open interest, but those sources must be collected or sourced historically before they can be used in a valid 180-day test.

## Acceptance gates before any production integration

1. No look-ahead, timestamp leakage, or use of an unfinished candle.
2. Better than naive baselines on multiple untouched time windows, not just one split.
3. Positive net expectancy after realistic fees, spread, slippage and funding.
4. Portfolio simulation with non-overlapping positions, correlated-asset exposure caps, stop gaps, minimum order sizes and liquidation constraints.
5. Paper-trading period with predictions logged before outcomes are known.
6. Production remains read-only/manual until explicitly approved; real exchange order placement stays disabled.

## Run locally

```bash
python -m pip install -r requirements.txt pytest scikit-learn
python -m pytest -q tests/test_market_research.py
python scripts/market_research.py --days 180 --universe 30 --min-quote-volume 5000000
```

The JSON report is written to `research-output/market_research_180d.json`.


## Event-agnostic microstructure discovery (experimental)

The read-only capture pipeline also runs `scripts.microstructure_discovery`.
It evaluates continuous features rather than relying only on four hand-written
events: taker imbalance, normalized CVD, book/depth imbalance, microprice,
spread, trade/volume surprise, open-interest change, funding, momentum and
realized volatility.

- Feature ranks use a per-symbol trailing window; no future rows enter the rank.
- Forward returns are retained only when the target closed candle is exactly
  contiguous with the event timestamp.
- Reports top-decile LONG and bottom-decile SHORT screens at 1m, 5m and 15m,
  with separate trend, volatility and cross-sectional breadth slices.
- Each directional observation pays the configured round-trip cost assumption.
- Fewer than four independent 24-hour clusters is explicitly marked
  insufficient for inference. A single capture never approves a strategy.
- The screen is exploratory and does not adjust for multiple testing, simulate
  portfolio overlap, or measure actual execution costs. It cannot be used as
  a trading signal.

Run locally:

```bash
python -m scripts.microstructure_discovery --db /path/to/market_microstructure.sqlite3 --cost-bps 12
python -m pytest -q tests/test_microstructure_discovery.py
```
