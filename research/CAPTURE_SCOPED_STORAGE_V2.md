# Capture-scoped market storage v2

## Problem found in provenance v1

The v1 `capture_observations` table records that a capture observed a candle, feature minute, or derivatives timestamp. However, the canonical market tables remain keyed only by market symbol and timestamp. Their `INSERT OR REPLACE` writes can overwrite a minute aggregate or snapshot from an overlapping capture. Both captures can retain provenance links while only the last canonical payload survives. Therefore, a provenance link alone does not preserve the exact feature values produced by each capture.

The collector currently aggregates individual trades and depth snapshots in memory into a per-minute `minute_features` row. It does not persist each raw trade or each raw depth update. This v2 change preserves each capture's minute aggregate, not a tick-by-tick raw feed.

## v2 storage

New collector sessions write separate research tables:

- `capture_minute_features`: full minute aggregate keyed by `(capture_id, symbol, minute_ts)`.
- `capture_minute_candles`: closed candle payload keyed by `(capture_id, symbol, minute_ts)`.
- `capture_derivatives_snapshots`: derivatives payload keyed by `(capture_id, symbol, ts_ms)`.
- `capture_storage_versions`: declares that a session uses schema version 2.

The existing canonical `minute_features`, `minute_candles`, and `derivatives_snapshots` tables remain unchanged for cross-capture market-time deduplication and existing research consumers. The v2 tables preserve each capture's actual stored payload separately. `capture_observations` remains a compact provenance index.

The merger creates empty v2 tables when its first input is an older capture, copies v2 rows from newer inputs, and uses the composite capture keys so separate sessions are not collapsed. The coverage gate prefers v2 capture-scoped closed-candle rows, falls back to v1 exact observation links, and only then uses the explicitly labeled legacy time-window estimate.

## Validation requirements

- Two collectors writing the same symbol/minute must preserve both independent minute-feature payloads in v2 storage even though the canonical market-time table remains deduplicated.
- Merging two v2 captures must preserve both capture IDs and payload values.
- The coverage gate must count v2 candles from the capture-scoped table rather than infer membership from wall-clock ranges.
- Old capture databases must remain mergeable; no production DB or schema is involved.

## Interpretation limits

- This stores the minute aggregates generated from trades and depth snapshots, not individual raw trade/depth events.
- Two captures observing the same market minute are still one market-time outcome for statistical inference. Do not count them as independent outcomes merely because their capture IDs differ.
- The v2 schema is additive and research-only. Existing canonical tables are not rewritten by the collector change. No strategy, signal, risk, production runtime, or order behavior is changed.
