# Production bot planned tasks

## Completed

- [x] Show scan progress while a scan is running: completed pairs out of the configured universe (PR #25).
- [x] Show the symbol currently being analyzed and elapsed time for the active scan (PR #25).
- [x] Keep rejection reasons from the previous completed scan clearly labeled while a new scan is in progress (PR #25).
- [x] Show a compact per-symbol outcome list (accepted, rejected, skipped, or error) (PR #25).
- [x] Log per-symbol analysis start and finish with progress and elapsed time (PR #25).
- [x] Preserve the previous completed scan duration while a new scan is running (PR #26).
- [x] Clear active-scan state and retain partial per-symbol results if symbol analysis is cancelled (PR #26).

## Deliberately unchanged

- Strategy B entry filters, thresholds, indicator formulas, and signal ranking.
- Risk 10%, leverage 5x, TP1 1.8R, TP2 3R.
- Futures-only market scope and the 14-symbol USDT-M universe.
- C/D/E add-on strategies remain disabled by default.
- Real exchange orders remain disabled; no account credentials or private order endpoints are used.
- Database schema and production SQLite data are untouched by the scan diagnostics work.

## Next planned diagnostics

1. Verify the live progress and cancellation-state fields after PR #25/#26 are confirmed deployed on FadeHost.
2. If a pair repeatedly takes materially longer than the observed ~1.3–1.5 seconds, profile its API stages before adding any timeout.
3. Consider a concise per-symbol rejection breakdown in a separate Telegram diagnostics view if the status message becomes too long.
4. Keep a short sample of completed scan logs and compare duration, symbols checked, errors, and rejection distribution; do not loosen Strategy B filters merely to increase signal count.

## Validation and release gate

- Run compile/import/safe-default checks and deterministic diagnostics tests on the exact PR head.
- Confirm the deployed FadeHost commit separately from GitHub `main`.
- Do not merge or deploy future changes until the PR and CI are reviewed and the user explicitly approves the relevant action.
