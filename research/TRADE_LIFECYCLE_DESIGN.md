# Trade lifecycle and personal position registry — design draft

Status: research design only. Not connected to Telegram handlers, production signals, or the live database.

## Product rules already agreed

- A forecast is not a personal trade. It becomes one only after the user presses **«Я вошёл в сделку»**.
- After confirmation, ask for the actual filled entry price. Do not silently substitute the forecast reference price.
- Keep at most five simultaneously active, user-confirmed positions per user.
- Do not add a **«Просто наблюдаю»** action.
- The personal-trades view contains only the user's confirmed trades; research signals and unconfirmed forecasts do not appear there.
- Show symbol, direction, actual entry, current market price, current SL, TP1/TP2, status, last important recommendation and remaining quantity.
- Provide refresh, correction of actual entry, update of manually changed SL, and stop/close monitoring with confirmation.
- Keep a separate closed-trade history with realized outcome and R multiple.
- Notifications should be event-driven and limited to material changes. No order placement or exchange position synchronization is in scope.

## Proposed additive data model

### `user_trades` — current state

- `id`: stable trade ID
- `user_id`: owner; every read/update must filter by this ID
- `signal_id`: originating forecast
- `market`, `symbol`, `side`
- `actual_entry_price`: user-entered fill, never inferred
- `initial_quantity`, `remaining_quantity`
- `current_sl`, `tp1`, `tp2`
- `status`: `OPEN`, `PARTIALLY_CLOSED`, `CLOSED`, `STOPPED`
- `opened_at`, `closed_at`, `updated_at`
- `realized_pnl_usdt`, `realized_r`
- `last_important_event`, `last_notification_at`

Constraint: no more than one personal trade per user and source signal unless a future explicit product rule permits re-entry. A partial unique index on (user_id, signal_id) may be used if the supported SQLite version and migration path are verified.

### `user_trade_events` — append-only audit trail

- `id`, `trade_id`, `user_id`
- `event_type`: entered, entry_corrected, stop_updated, partial_close, target_reached, stop_reached, monitoring_stopped, closed
- `old_values_json`, `new_values_json`, `price`, `quantity`, `created_at`
- Foreign key to `user_trades`; events must be scoped to the owning user in application queries too.

Do not rewrite event history when a user corrects a value. Append a correction event and update the current-state row in the same transaction.

## Critical transaction rules

1. **Enter trade:** begin an immediate SQLite write transaction; verify the signal belongs to a valid forecast; count that user's OPEN/PARTIALLY_CLOSED positions; reject if count is already five; insert the trade and its entry event atomically; commit.
2. **Race protection:** the five-trade limit must be checked and inserted under the same write transaction. A UI-side count is not sufficient.
3. **Ownership:** every callback carries a trade ID but every SQL read/update also requires `user_id = current_user_id`. Never trust a Telegram callback payload as authorization.
4. **Partial close:** quantity must be positive and no greater than remaining quantity. Record realized quantity/PnL and reduce remaining quantity in one transaction. When remaining quantity reaches zero, close monitoring.
5. **Entry correction:** only before any partial close/realized outcome unless a separately designed correction workflow exists. Preserve the old value in the event.
6. **Stop update:** validate positive price and correct side. For LONG the stop must remain below current market; for SHORT above current market. If the stop crosses entry, remaining stop-loss risk is zero, but that does not imply a guaranteed profit.
7. **Stop monitoring:** require explicit confirmation. Stopping monitoring is not equivalent to closing an exchange position; make that distinction explicit in Telegram.
8. **Restart recovery:** rebuild active views from persisted rows; do not rely on in-memory dictionaries as the source of truth.

## Portfolio risk calculation

For a linear USDT-M position, estimate remaining price risk using the actual entry, current stop and remaining base quantity:

- LONG: `max(actual_entry - current_sl, 0) * remaining_quantity`
- SHORT: `max(current_sl - actual_entry, 0) * remaining_quantity`

Sum only user-confirmed active positions. Add proposed signal risk and compare with an explicit user-approved portfolio cap. The current bot has no reliable position registry yet, so the pure portfolio-risk helper is not connected to signal delivery.

This is a price-risk estimate only. Fees, funding, slippage, gaps, liquidation, partial execution and exchange-specific contract behavior can increase realized loss. A stop moved beyond entry must not be presented as guaranteed risk-free.

## Notification policy

Send only material events: entry confirmation, TP1 reached, stop changed/invalidated, target/stop reached, important setup invalidation, and monitoring stopped. Deduplicate by trade ID + event type + event timestamp/key. Avoid repeated minute-by-minute price messages.

## Migration and rollout gates

- Schema addition only; no destructive migration, no rewrite of existing `users`, `user_settings`, `signals` or `signal_events`.
- Back up and verify the DB before migration; abort startup if backup verification fails.
- Add schema and repository tests first, then pure lifecycle tests, then Telegram handlers behind a disabled feature flag.
- Test ownership isolation, five-trade concurrency, duplicate callbacks, partial close idempotency, invalid SL, restart recovery, and old DB compatibility.
- Shadow/read-only tests first. No exchange order endpoints, no private API credentials, no automatic order execution.
- Do not deploy or apply migration to the live database without explicit approval.

## Open product decision

The total portfolio risk cap is not selected. Do not hard-code a value or treat the five-position limit as a risk cap: five trades at 10% nominal risk each could stack to 50% before fees and slippage.
