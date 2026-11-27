# Production bot planned tasks

## Completed in current diagnostics patch (PR in review)
- Show scan progress while a scan is running: completed pairs out of the configured universe.
- Show the symbol currently being analyzed and elapsed time for the active scan.
- Keep rejection reasons from the previous completed scan clearly labeled while a new scan is in progress.
- Show a compact per-symbol outcome list (accepted, rejected, skipped, or error).
- Log per-symbol analysis start and finish with progress and elapsed time.

## Deliberately unchanged
- Strategy B entry filters, thresholds, indicator formulas, and signal ranking.
- Risk 10%, leverage 5x, TP1 1.8R, TP2 3R.
- Futures-only market scope and the 14-symbol USDT-M universe.
- C/D/E add-on strategies remain disabled by default.
- Real exchange orders remain disabled; no account credentials or private order endpoints are used.
- Database schema and production SQLite data are untouched.

## Next planned diagnostics
1. Preserve the previous completed scan duration while a new scan is running.
2. Clear active-scan state and retain partial per-symbol results if symbol analysis is cancelled.
3. Observe the live progress fields after the patch is deployed.
4. If a pair repeatedly takes materially longer than the observed ~1.3–1.5 seconds, profile its API stages before adding any timeout.
5. Consider a concise per-symbol rejection breakdown in a separate Telegram diagnostics view if the status message becomes too long.

## Validation gate
- Run compile/import/safe-default checks and deterministic scan diagnostics tests on the exact PR head.
- Do not merge or deploy until the PR and CI are reviewed and the user explicitly approves deployment.
