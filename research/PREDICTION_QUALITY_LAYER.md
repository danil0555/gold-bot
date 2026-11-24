# Prediction Quality Layer v1 (shadow mode)

This module is a research-only feature layer. It is not connected to Telegram
signal generation and cannot place orders.

## Included

- Exact-timestamp 1m/5m/15m/60m returns; missing minutes stay missing.
- Time-based rolling volatility/trend windows that do not treat sparse rows as consecutive minutes.
- Causal market regime features; thresholds use trailing data only.
- Exact-timestamp BTC benchmark context and relative returns.
- Backward-only derivatives snapshots with a 120-second default freshness limit.
- Open-interest/price quadrants as positioning proxies.
- Frozen reliability-bin probability calibration and held-out Brier evaluation.
- Error summaries split by market regime and predicted direction.

## Important limitations

1. The collector currently records open interest and funding, not exchange liquidation
   events. The OI/price quadrants are **not liquidation data** and must not be described
   as liquidations.
2. Calibration must be fitted only on a chronological training segment, then frozen
   and evaluated on a later untouched segment. Do not fit and score on the same rows.
3. Regime labels are descriptive, not a proven predictor. They must be validated by
   symbol, time period and execution costs before influencing signal acceptance.
4. Missing BTC or derivatives context stays missing; it is never silently filled. BTC self-relative returns are zero only after the corresponding benchmark return is available.
5. OI/price positioning labels require strictly positive/negative changes; a zero or neutral change remains unknown.
6. Rolling windows are time-based, not row-count based, so data gaps cannot masquerade as dense one-minute observations.
5. The module is intentionally shadow-only. Integration into the live signal path
   requires separate review and explicit approval.

## Run tests

`python -m pytest tests/test_prediction_quality_layer.py -q`
