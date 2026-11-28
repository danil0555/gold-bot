# Eight-capture pooled microstructure event study

**Status:** exploratory research only; no production signal or strategy approved.

## Data and validation

The pooled research database combines the six-capture merge with capture #9 and capture #12. The eight sessions span 2026-11-25 21:36 UTC through 2026-11-28 04:09 UTC: 32 hours of collection over roughly 54.5 elapsed hours, not a continuous 32-hour tape.

- Symbols: BTC-USDT and ETH-USDT.
- 8 capture sessions; 16 continuous symbol segments (8 per symbol).
- 15,378 provenance observations; 3,838 capture-scoped feature rows; 3,852 candles; 7,680 derivatives snapshots; 88 collector events.
- SQLite integrity: `ok`; foreign-key errors: 0.
- Missing and orphan provenance links: 0 for features, candles and derivatives.
- All 8 sessions have exact session links and clean stop events.
- 37/3,838 feature minutes (0.96%) had zero depth samples.
- There are 7 non-consecutive-minute boundaries per symbol. Rolling thresholds, OI deltas and forward labels reset at every gap. The analysis panel contains 3,836 matched closed-candle/feature rows.

## Method

Past-only rolling quantiles (120-minute lookback, 60 observations minimum), 5-minute per-symbol event thinning, next closed-minute forward returns at 1m/5m/15m, fixed 12 bps round-trip cost, and 24-hour time-block clustered uncertainty. The minimum configured number of independent clusters is four.

## Pooled results

Mean net directional return in basis points after 12 bps costs:

| Event hypothesis | Direction | 1m | 5m | 15m |
|---|---:|---:|---:|---:|
| Sell absorption | LONG | -12.572 | -13.715 | -15.151 |
| Buy absorption | SHORT | -12.028 | -10.337 | -8.745 |
| Bid support | LONG | -12.130 | -11.239 | -13.683 |
| OI expansion + sell pressure | SHORT | -11.430 | -9.364 | -6.731 |

Every one of the 12 event/horizon combinations has exactly four 24-hour clusters. This meets the pipeline's minimum gate, so clustered standard errors are reported, but four clusters are only the threshold—not robust evidence across multiple independent market regimes. All mean net returns remain negative.

## Decision

Do not add these hypotheses to Strategy B, loosen Strategy B filters, or trade these event labels. No live signal, risk, leverage, TP/SL, production database, credentials, or deployment behavior is changed.

The next research gate is to collect materially more independent UTC-day blocks across different volatility/trend regimes, preserve session boundaries, and then run untouched time-based holdouts plus realistic execution/funding/slippage simulation. A result that remains negative after costs should be rejected rather than optimized until it looks positive.

## Reproducibility

- Analysis implementation: `scripts/microstructure_event_study.py` on research branch `research/capture-v2-4h-run` (gap-aware PR #31).
- Machine-readable summary: [eight-capture-event-study.json](eight-capture-event-study.json).
- Source Actions artifacts: six-capture merged DB `15229047580`, capture #9 DB `15239625601`, capture #12 DB `15247044348`, and the corresponding event-study outputs `15228738674`, `15239555778`, `15247812336`.
- The pooled SQLite file is a disposable research merge, not a production database.
