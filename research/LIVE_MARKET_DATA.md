# Live microstructure data recorder

This experimental worker is read-only. It uses BingX's public USDT-M WebSocket market-data feed and public REST market-data endpoints. It does not need an API key and cannot place orders.

## What it records

The worker subscribes to two channels per selected contract:

- `{symbol}@trade`: individual public trades. Aggressor side is inferred from BingX's `m` flag (buyer-maker means seller-taker).
- `{symbol}@depth20@500ms`: limited 20-level order-book snapshots.

It aggregates the stream into one row per symbol per UTC minute:

- aggressive buy/sell quote turnover, trade count and cumulative-volume delta;
- average/minimum/maximum quoted spread;
- top-5 and top-20 bid/ask depth in USDT terms;
- order-book imbalance and microprice offset;
- last observed mid-price.

A separate table stores periodic open-interest and funding/mark/index-price snapshots. Reconnects and REST errors are written to a collector-events table. Repeated trade IDs are deduplicated in memory.

## Data integrity cautions

- The depth channel's `ts` is when BingX constructs the message, not a guaranteed matching-engine book timestamp. It is not proof of the source book's exact age.
- The collector uses limited-depth snapshots rather than incremental-depth reconstruction. This avoids relying on update IDs as a reliable sequence/checksum; BingX documents that IDs alone cannot prove book completeness.
- The worker stores minute aggregates, not every raw depth update or every raw trade. This is a deliberate storage compromise for a small hosting plan.
- Gaps during disconnects are not fabricated or interpolated. Reconnect events are logged.
- OI and funding are snapshots from the time collection begins; this does not backfill 180 days of historical OI or order-book data.

## Database safety

Default path: `research-data/market_microstructure.sqlite3`.

This is a new research database. It does not open, migrate, delete, or write to the production `trading_bot_v9.db`.

## Run

```bash
python scripts/market_data_collector.py --universe 20 --duration-hours 0
```

Stop with Ctrl+C. For a short test:

```bash
python scripts/market_data_collector.py --symbols BTC-USDT,ETH-USDT --duration-hours 0.05 --db-path /tmp/market_microstructure.sqlite3
```

A duration of zero means run continuously. The default universe is selected by current 24-hour USDT quote volume when no explicit symbols are supplied.

## Operational requirements

Run this as a separate FadeHost worker only after the code passes its live smoke test and the user approves the runtime change. Keep the research database on persistent storage with enough disk space. Do not merge this worker into production's startup path automatically.
