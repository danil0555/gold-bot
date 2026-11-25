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


## 2026-11-25 — daily coverage gate hardening
- The original multi-day gate could accept 600 closed minutes spread very unevenly across three UTC dates (for example, 590 + 5 + 5). A total-count threshold and a date-count threshold alone do not guarantee meaningful observations on each date.
- Added a configurable minimum of 120 closed minutes per symbol per UTC date (default), included per-day counts and undercovered-date diagnostics in the report, and made any undercovered observed date block `holdout_ready`.
- This remains a history-sufficiency check, not a continuous-coverage or data-quality certification. Capture-session breaks and within-session gaps still require separate diagnostics because the current table does not encode session identity.
- Added regression tests for sparse dates and invalid daily thresholds. Research-only; no production files, database, credentials, deployment or exchange orders changed.


## 2026-11-25 — explicit collector session metadata and gap diagnostics
- Collector creates a unique capture session row with start/end UTC epoch milliseconds and the exact symbol list, plus start/stop diagnostic events.
- Database merger carries `collector_sessions` metadata and remains backward-compatible with older captures that lack the table.
- Multi-day coverage report emits per-session, per-symbol candle counts, maximum internal gap and number of internal gaps. These are diagnostics only; they do not yet block holdout readiness.
- Regression tests cover session diagnostics, sparse UTC dates and legacy-capture compatibility.
- Next: validate the exact branch through CI before relying on this metadata in another research run. Consider a strict gap gate only after inspecting real within-session gap distributions.
- Research-only: production code, production DB, credentials, deployment and exchange orders untouched.

## Capture-session integrity follow-up

- The collector runs its normal shutdown path in a `finally` block, but an abrupt process/host termination can leave `collector_sessions.ended_at_ms` null.
- The multi-day coverage gate now reports these as `open_capture_sessions` and blocks `holdout_ready` until every recorded capture session is closed. This avoids treating a potentially incomplete capture as a finalized research dataset.
- Sessions whose time intervals overlap while sharing one or more symbols are listed in `ambiguous_capture_sessions`; each affected session is marked `session_attribution_ambiguous=true`.
- This ambiguity exists because `minute_candles` is keyed by `(symbol, minute_ts)`, not by `capture_id`. The diagnostic does not guess which capture produced a shared candle and does not silently claim exact per-session attribution.
- These checks improve provenance diagnostics; they do not establish uninterrupted market-data coverage, data correctness, statistical significance, predictive edge, or profitability.

## Capture-scoped observation provenance v1

- New collector databases create `capture_observations`, keyed by capture UUID, symbol, observation timestamp and type (`candle`, `features`, `derivatives`).
- Closed candles, flushed minute features and derivatives snapshots each write an idempotent link to the active capture session. Market tables retain their existing de-duplicated keys; the new table records which capture observed each item without rebuilding or rewriting historical market tables.
- The multi-day gate uses these exact capture links for per-session candle counts and gap diagnostics when the table exists. Legacy databases without the table retain the earlier time-window estimate and identify that source in `attribution_source`.
- The merger carries the new mapping table and creates it for older first-input databases. Legacy source captures without the mapping table remain accepted; they simply contribute no exact capture links.
- This is provenance, not a claim that two sessions observing the same market minute represent two independent market outcomes. Research samples must still be deduplicated by market timestamp for statistical inference.
- Existing market tables and old databases are not migrated or rewritten. The feature is research-only and requires a fresh collector run to populate exact links; old merged captures cannot be retroactively attributed exactly.

- Mixed old/new merged databases use an explicit `session` provenance marker: modern sessions with no candle links correctly report zero exact observations, while legacy sessions without a marker use a clearly labeled time-window estimate. This avoids treating every old session as empty merely because the merged database contains the newer mapping table.

- The merge output upgrades the earlier mapping-table CHECK constraint to allow the explicit session marker, preserving existing observation links. This matters when an older PR #14 capture is the first input and a newer capture is merged after it.


## Capture-scoped storage v2 — 2026-11-25

- A code audit found that v1 provenance links alone do not preserve per-capture payloads: canonical `minute_features` remains keyed by `(symbol, minute_ts)`, so overlapping collectors can overwrite each other's minute aggregate while both provenance links remain.
- Added separate v2 tables keyed by capture ID for minute aggregates, closed candles, and derivatives snapshots; canonical cross-capture tables remain unchanged. A schema-version marker distinguishes v2 sessions from v1 and legacy sessions.
- The collector's trades and depth updates are aggregated in memory into minute features; v2 preserves each session's aggregate, not every raw trade/depth tick.
- Merger and coverage-gate support are included. The gate prefers exact v2 candle rows, then v1 observation links, then explicitly labeled legacy time-window estimates.
- Added tests for overlapping collector feature values, merge preservation, and v2 coverage attribution. CI must pass on the exact branch head before this storage design is treated as validated.
- Research only. No production main, production DB, credentials, deployment, strategy, risk, or exchange order behavior changed.
- Statistical warning: multiple captures observing the same market minute remain one market-time outcome, not independent samples.
