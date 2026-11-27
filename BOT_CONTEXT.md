# Trading Bot — canonical project context

> Last updated: 2026-11-27  
> Repository: https://github.com/danil0555/gold-bot  
> Default branch: `main`  
> Purpose: persistent handoff record across ChatGPT conversations. This file is a project-state snapshot, not a substitute for checking the live repository, open PRs, CI, deployment console, or the latest user instructions.

## How to resume work

At the start of every new Trading Bot conversation:

1. Read this file from the current repository default branch.
2. Check the latest `main` commit and compare it with the deployed FadeHost commit (ask the user for a fresh Console screenshot/log if not available).
3. List and inspect open PRs, especially their base/head branches, exact head SHAs, draft status, and exact-head CI.
4. Check the latest GitHub Actions runs relevant to the active research capture and merge workflows.
5. Read `planned_tasks/` and identify the next unfinished item; do not redo completed work.
6. Reconcile this document against current code and verified external state. If sources conflict, apply this precedence:
   - latest explicit user instruction;
   - current repository code and current GitHub branch/PR state;
   - verified production deployment state;
   - project documentation and planned tasks;
   - earlier conversation history;
   - unapproved proposals.
7. Continue from the last verified stopping point. When the user says «Продолжай», do useful concrete repository/testing/research work and inspect planned tasks as part of the workflow.
8. Never claim access to every past chat. Use available conversation context, this file, GitHub and connected sources; if something cannot be retrieved, say so and do not invent it.
9. After every substantial milestone, update this file in a branch/PR with verified facts, changed files, commits, tests, remaining tasks, and permissions. Do not write secrets, API keys, tokens, or private credentials here.

## Product and production invariants

- Telegram market-scanning/signal bot for BingX USDT-M Futures.
- Production strategy baseline: Strategy B. Do not change its formulas, entry filters, thresholds, or ranking merely to increase signal count.
- Production risk sizing: 10% per signal; leverage: fixed 5x; TP1: 1.8R; TP2: 3R.
- Futures-only scope; configured production universe: 14 USDT-M symbols (TON-USDT was removed because BingX returned 109425 / contract not found).
- Strategies C/D/E remain disabled by default.
- Real exchange order placement remains disabled. No private order endpoints or autonomous order execution.
- No trade-monitoring / “My Trades” / “I entered” lifecycle feature; user explicitly abandoned that direction.
- Bot should remain selective and quiet when no high-quality setup exists. Do not weaken filters to force messages.
- A nominal 10% risk setting does not cap total portfolio loss: simultaneous positions, fees, funding, slippage, gaps and execution conditions can increase realized losses. Never imply profitability or guarantee results.
- Do not change API credentials or secrets.
- Preserve production SQLite data and schema. The user said `trading_bot_v9.db` has only about an hour of data and can be recreated if needed, but inspect and back up before any migration/recreation and warn first.
- No deployment/restart and no live orders without explicit, current user permission.

## Hosting and deployment

- Runtime host: FadeHost, bot `gold-bot`.
- Dashboard: https://laplace.fadehost.com/bots/bot_50f411135baf42c39cecda0878054b11
- FadeHost dashboard is private/authenticated; a URL alone does not grant access through ChatGPT.
- On the Free plan, pushes to GitHub do not automatically deploy. User must select **Redeploy (pull latest)** to fetch the latest repository commit. **Restart** only restarts currently deployed code and does not fetch changes.
- The user must perform the FadeHost action manually. Do not request passwords/tokens or screenshots containing secrets.
- Last verified deployed commit in available history: `f07741335840ff4b22499eec4c5201290adf663b` (PR #24). PR #25 and PR #26 were merged later, but their deployment was not verified. Thus do not state that production includes #25/#26 until a fresh FadeHost Console SHA confirms it.
- On 2026-11-27 the user reported all three free deploys for the day had been used and the next free deploy would be available in about 20 hours. Do not assume quota has reset; ask/check with the user before recommending a deploy.
- No current permission to deploy PR #26 or any subsequent changes.

## Main branch — verified state

Latest known `main` SHA: `179a7d23ee3fedbc35ea3e79f0893594d810c1d9` (PR #26 squash merge).

### Merged production PRs

- PR #23 — https://github.com/danil0555/gold-bot/pull/23  
  Exposed scan rejection reasons in Telegram status; no strategy changes. Exact-head CI passed. User deployed it manually.
- PR #24 — https://github.com/danil0555/gold-bot/pull/24  
  Removed unavailable `TON-USDT` from shared symbol universe, therefore both REST scan and WebSocket subscription. Added AST-based symbol-universe regression test. Merge SHA `f07741335840ff4b22499eec4c5201290adf663b`. User explicitly authorized merge and deployment; FadeHost deployment and BingX API/WS health were verified.
- PR #25 — https://github.com/danil0555/gold-bot/pull/25  
  Live scan progress, current symbol, elapsed time, and per-symbol outcomes/logs; no strategy changes. Merge SHA `e7da50719ac80e031d15093541a399afca929c93`. Exact-head CI passed. Deployment not independently verified.
- PR #26 — https://github.com/danil0555/gold-bot/pull/26  
  Preserves previous completed scan duration while a new scan runs; clears active-scan state and retains partial per-symbol results when symbol analysis is cancelled. Added test and roadmap update. Merge SHA `179a7d23ee3fedbc35ea3e79f0893594d810c1d9`. Exact-head CI run `46013439823` passed. User has NOT authorized FadeHost deployment; do not deploy.

## Open PRs requiring review (do not merge without permission)

- PR #20 — https://github.com/danil0555/gold-bot/pull/20  
  Draft research-only PR. Base: `research/capture-v2-4h-run`; head: `research/capture-v2-multiday-merge-v1`. Implements strict merge of capture-scoped SQLite databases while preserving capture/session provenance, v2 features/candles/derivatives/events and separately maintaining legacy convenience tables. Refuses schema/session/capture-ID conflicts, path aliasing, overwrite, and invalid DBs; stages to `.partial`, checks integrity/FKs, then atomic rename. Five-capture real-artifact integration passed on commit `8d40c3bb6029e99df1fd9e6929751286dffa0681`, push run `46029620213`, artifact `15214450010`. Verified merged DB: 5 sessions, 9,609 provenance observations, 2,398 minute features, 2,406 minute candles, 4,800 derivatives snapshots, 55 collector events; integrity check OK, FK check empty. PR remains Draft/unmerged. Research branch runtime safe-default check uses 2% risk; this is not production and must never be confused with production 10%.
- PR #21 — https://github.com/danil0555/gold-bot/pull/21  
  Draft reliability PR: atomic signal + audit writes, stale-signal expiry + audit transaction, fail-closed DB integrity/foreign-key validation before backup/migration. No strategy/risk/order behavior changes. No production DB opened/migrated; no deployment. Verify exact-head CI before further work.
- PR #22 — https://github.com/danil0555/gold-bot/pull/22  
  Draft API telemetry PR: clear stale BingX API error on valid successful response, record/display last successful response timestamp. Exact-head CI run `45891145506` passed; 22 addon/reliability/API tests + 59 signal/balance/sizing/portfolio tests (81 total). Safe defaults confirmed: real trading OFF, add-ons OFF, risk 10%, leverage 5x. Base was older `a863b12...`; review for conflicts/current base before merge. No deployment.
- Check whether any newer PRs exist; this list is a dated snapshot.

## Market-data capture research

Research capture is isolated from production. Never merge research into `main` or change production signals based on exploratory results without independent validation and explicit approval.

### Capture sessions 1–5

Five separate 4-hour windows (not continuous coverage) were collected from BTC-USDT and ETH-USDT. Their total duration is 20 hours over about 41.5 elapsed hours, with gaps.

1. 2026-11-25 21:36:49.903–2026-11-26 01:36:49.905 UTC; 1,923 observations, 480 features, 482 candles, 960 derivatives.
2. 2026-11-26 06:43:59.705–2026-11-26 10:43:59.707 UTC; 1,919 observations, 478 features, 480 candles, 960 derivatives.
3. 2026-11-26 18:41:52.256–2026-11-26 22:41:52.258 UTC; 1,923 observations, 480 features, 482 candles, 960 derivatives.
4. 2026-11-27 06:53:19.865–2026-11-27 10:53:19.867 UTC; 1,923 observations, 480 features, 482 candles, 960 derivatives.
5. 2026-11-27 11:08:00.654–2026-11-27 15:08:00.656 UTC; 1,921 observations, 480 features, 480 candles, 960 derivatives.

Capture #5 DB integrity check OK, foreign-key check empty, both symbols had continuous 240-minute features/candles, clean stop event. Event-study outputs are exploratory only: every event/horizon had just one independent 24-hour cluster and was labelled `insufficient_independent_clusters`. Some attractive small-sample statistics are not validated edge, not trade recommendations, and must not be promoted into live signals.

### Capture #8 / next workflow run (state must be refreshed)

- GitHub Actions run ID `46029620348`, branch `research/capture-v2-multiday-merge-v1`, head commit `8d40c3bb6029e99df1fd9e6929751286dffa0681`.
- Last observed state: workflow event push, capture job `140609819523` was `in_progress`, started `2026-11-27T15:58:00Z`. Expected capture duration 4 hours. This is a stale checkpoint; first re-query the run before stating whether it finished.
- If completed, inspect uploaded SQLite DB and event-study artifact; verify integrity, foreign keys, session duration, symbol coverage, minute completeness, derivatives, collector events, report caveats. Compare with prior sessions.
- Do not infer completion from elapsed time or fabricate artifacts if none are present.

## Scan diagnostics — current interpretation

- Production logs observed 14/14 symbols checked; a representative scan completed in about 18.9 seconds.
- BingX API OK, WebSocket connected, trades and 1m candles received.
- Zero signals were accepted in observed scans because Strategy B filters rejected candidates (trend/momentum alignment absent, ADX below threshold, price too far from EMA20/exhaustion).
- This is not evidence that scanner is stuck. Keep filters unchanged; inspect fresh logs and market conditions before diagnosing.
- PR #25/#26 diagnostics deployment needs confirmation via FadeHost deployed SHA.

## Planned work and next actions

1. Refresh GitHub Actions run `46029620348`; inspect artifacts if completed.
2. Review exact-head CI and diffs for open PRs #20, #21, #22; keep research branch isolated and do not merge any PR without user permission.
3. Ask user for a fresh FadeHost Console SHA when deployment status matters. Do not deploy unless user explicitly authorizes it and quota/action are available.
4. Continue collecting independent capture windows only if useful; do not overstate a small or discontinuous sample.
5. Keep BOT_CONTEXT.md updated after each meaningful milestone.

## Permission ledger

- PR #19: user authorized merge into research branch `research/capture-v2-4h-run` only; merged there.
- PR #23: merged and deployed with user authorization.
- PR #24: user authorized merge and deployment; merged and deployed.
- PR #25: merge authorized and merged; deployment not verified.
- PR #26: merge authorized and merged; deployment NOT authorized/verified.
- PR #20: no merge authorization; Draft research-only.
- PR #21: no merge authorization; Draft.
- PR #22: no merge authorization; Draft.
- Any future merge, FadeHost redeploy/restart, database migration, credential change, or live order action requires explicit user authorization.

## End-of-stage handoff

At the end of each substantial work stage, report:
- what changed and why;
- branch, commit SHA, PR URL;
- tests/CI with exact run IDs and results;
- production/deployment status separately from repository merge status;
- research artifacts and limitations;
- remaining tasks and any required user permission.

Append a concise `/RESTORE_BOT_CONTEXT` handoff instruction to the conversation. Never claim automatic access to unavailable chat history; use this file plus current repository and verified sources to restore continuity.
