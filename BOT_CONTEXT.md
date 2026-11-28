# Trading Bot — canonical project context

> Last updated: 2026-11-28 (PR #32 and research captures #9/#12 verified)  
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

Latest verified `main` SHA: `d1a54d8cfa38b3bf8d64282fa5fe9b74b3582b7c` (PR #32 squash merge). PR #22 code merge is `f7f4c97ed099bef3dc20984fd7793e42c12dda04`.

### Merged documentation PRs

- PR #27 — https://github.com/danil0555/gold-bot/pull/27  
  Added this canonical context file and reconciled `planned_tasks/production-scan-diagnostics.md`. Documentation-only; exact-head CI run `46057225162` passed. Squash merge SHA `58550ee9e513c7cc2e5d1c1da425f2392e6573d8`. No runtime or deployment changes.
- PR #28 — https://github.com/danil0555/gold-bot/pull/28  
  Recorded PR #27 merge and refreshed the capture #8 checkpoint. Documentation-only; exact-head CI run `46057434710` passed. Squash merge SHA `3b85d7d1019bc63e7dad4b543fb0b81c7fbc6e75`. No runtime or deployment changes.
- PR #29 — https://github.com/danil0555/gold-bot/pull/29  
  Recorded PR #28 merge and the capture #8 checkpoint. Documentation-only; exact-head CI run `46057681405` passed. Squash merge SHA `4ce5d348b2ed81c8ee50dd5ccdff2a6ac4118cae`. No runtime or deployment changes.
- PR #30 — https://github.com/danil0555/gold-bot/pull/30  
  Recorded six-capture integration validation, PR #22 merge and the pending capture checkpoint; added API telemetry deployment verification to planned tasks. Documentation-only; exact-head CI run `46062030832` passed. Squash merge SHA `ace34ea42312755c55b7628beab96cf84a9fc8df`. No runtime or deployment changes.

### Merged production PRs

- PR #23 — https://github.com/danil0555/gold-bot/pull/23  
  Exposed scan rejection reasons in Telegram status; no strategy changes. Exact-head CI passed. User deployed it manually.
- PR #24 — https://github.com/danil0555/gold-bot/pull/24  
  Removed unavailable `TON-USDT` from shared symbol universe, therefore both REST scan and WebSocket subscription. Added AST-based symbol-universe regression test. Merge SHA `f07741335840ff4b22499eec4c5201290adf663b`. User explicitly authorized merge and deployment; FadeHost deployment and BingX API/WS health were verified.
- PR #25 — https://github.com/danil0555/gold-bot/pull/25  
  Live scan progress, current symbol, elapsed time, and per-symbol outcomes/logs; no strategy changes. Merge SHA `e7da50719ac80e031d15093541a399afca929c93`. Exact-head CI passed. Deployment not independently verified.
- PR #26 — https://github.com/danil0555/gold-bot/pull/26  
  Preserves previous completed scan duration while a new scan runs; clears active-scan state and retains partial per-symbol results when symbol analysis is cancelled. Added test and roadmap update. Merge SHA `179a7d23ee3fedbc35ea3e79f0893594d810c1d9`. Exact-head CI run `46013439823` passed. User has NOT authorized FadeHost deployment; do not deploy.

## Open PRs requiring review


- PR #20 — https://github.com/danil0555/gold-bot/pull/20  
  Merged only into research base `research/capture-v2-4h-run`, merge SHA `3c9abdb9191640676a20a6db5ea0b4460c6860c6`; never merged into production `main`. Implements strict merge of capture-scoped SQLite databases preserving capture/session provenance, v2 features/candles/derivatives/events and separately maintaining legacy convenience tables. Exact-head PR validation run `46061627933` passed. Push integration run `46061621061` passed all six real artifacts; merged artifact `15229047580` independently inspected: 6 sessions, 11,532 observations, 2,878 features, 2,888 candles, 5,760 derivatives snapshots, 66 events; integrity OK, FK check empty, no legacy duplicate rows discarded. Research branch runtime safe-default check uses 2% risk; this is not production and must never be confused with production 10%.
- PR #21 — https://github.com/danil0555/gold-bot/pull/21  
  Draft reliability PR: atomic signal + audit writes, stale-signal expiry + audit transaction, fail-closed DB integrity/foreign-key validation before backup/migration. No strategy/risk/order behavior changes. No production DB opened/migrated; no deployment. Verify exact-head CI before further work.
- PR #22 — https://github.com/danil0555/gold-bot/pull/22  
  Merged to production `main`, squash SHA `f7f4c97ed099bef3dc20984fd7793e42c12dda04`. Clears stale BingX API error on valid successful response, records/displays last successful response timestamp. Exact-head CI run `45891145506` passed; 22 addon/reliability/API tests + 59 signal/balance/sizing/portfolio tests (81 total). Safe defaults confirmed: real trading OFF, add-ons OFF, risk 10%, leverage 5x. No DB migration, strategy or risk changes. FadeHost deployment is NOT verified.
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

### Capture #8 — completed and independently inspected

- GitHub Actions run `46029620348`, branch `research/capture-v2-multiday-merge-v1`, head `8d40c3bb6029e99df1fd9e6929751286dffa0681`; completed successfully.
- Artifacts: DB `15227733846`, event study `15227813816`.
- Session `2026-11-27T15:59:06Z`–`19:59:06Z`, BTC-USDT and ETH-USDT. 1,923 provenance observations, 480 features, 482 candles, 960 derivatives snapshots, 11 collector events; all 6 WS subscription acknowledgements and clean stop.
- Independent SQLite check: integrity `ok`, FK check empty; both symbols have uninterrupted 240-minute feature sequences. Depth samples were zero in 2 BTC minutes (0.83%) and 3 ETH minutes (1.25%).
- Event study: four event hypotheses, 1m/5m/15m horizons; all had only one independent 24h cluster vs required minimum 4. With fixed 12 bps costs all four hypotheses had negative mean net returns at all horizons. No strategy signal is approved.

### Six-capture merge validation and pooled event study

- PR #20 research-only merge passed exact-head PR validation run `46061627933` and six-artifact push integration run `46061621061` on head `a0ffde2a1fdd5933c57fad15d4639c1985be5bad`; merge SHA into research base `3c9abdb9191640676a20a6db5ea0b4460c6860c6`.
- Merged artifact `15229047580`: 6 sessions, 11,532 observations, 2,878 features, 2,888 candles, 5,760 derivatives snapshots, 66 events; integrity `ok`, FK check empty, zero legacy duplicates.
- PR #31 — https://github.com/danil0555/gold-bot/pull/31 — merged only into `research/capture-v2-4h-run`, merge SHA `eec54ba3df345e33bafd42cade43b7c99b62d610`. Exact-head CI run `46062576165` passed after fixing a test expectation. It added gap-aware segment resets to the event-study pipeline and a six-capture pooled report.
- Pooled artifact `15228738674` from run `46062576165`: 2,876 matched symbol/minute rows across 12 continuous segments (six capture sessions × two symbols). The analysis now resets rolling thresholds, returns, OI deltas and forward labels at each missing/non-consecutive minute.
- Every event/horizon has only 3 independent 24h clusters, below the minimum 4. Every result remains `insufficient_independent_clusters`. After 12 bps costs, mean net returns were negative for all four hypotheses at 1m/5m/15m. No signal is approved.
- The two next scheduled capture workflows have now completed successfully; see the verified #9/#12 checkpoint below.

## Scan diagnostics — current interpretation

- Production logs observed 14/14 symbols checked; a representative scan completed in about 18.9 seconds.
- BingX API OK, WebSocket connected, trades and 1m candles received.
- Zero signals were accepted in observed scans because Strategy B filters rejected candidates (trend/momentum alignment absent, ADX below threshold, price too far from EMA20/exhaustion).
- This is not evidence that scanner is stuck. Keep filters unchanged; inspect fresh logs and market conditions before diagnosing.
- PR #25/#26 diagnostics deployment needs confirmation via FadeHost deployed SHA.

## Planned work and next actions

1. Preserve capture #9/#12 as separate provenance sessions; next research step is a deliberate pooled analysis that retains all capture/segment boundaries and only reports inference when independent-cluster requirements are met.
2. Keep PR #21 on hold until production SQLite integrity and foreign-key checks can be verified; do not let fail-closed startup guard surprise the running bot.
3. Verify PR #22's API-success timestamp in Telegram only after the new `main` commit is deployed; merge does not equal deployment.
4. Confirm FadeHost's deployed SHA and current deploy quota with the user before recommending a redeploy; latest verified deployed SHA remains `f077413...`.
5. Continue collecting independent capture windows and require multiple independent time clusters, untouched holdouts and realistic cost/execution simulation before strategy consideration.
6. Keep BOT_CONTEXT.md updated after each meaningful milestone.

### Captures #9 and #12 — completed and independently inspected (2026-11-28)

- Capture #9: Actions run `46061561700`, branch `research/capture-v2-multiday-merge-v1`, head `517c2fcf31435f69bb4bbc475056c10ac544dab1`; success. Four-hour session `2026-11-27T20:08:52.982Z`–`2026-11-28T00:08:52.984Z`. DB artifact `15239625601`; event-study artifact `15239555778`.
- Capture #12: Actions run `46062718647`, branch `research/capture-v2-4h-run`, head `eec54ba3df345e33bafd42cade43b7c99b62d610`; success. Four-hour session `2026-11-28T00:09:23.387Z`–`2026-11-28T04:09:23.388Z`. DB artifact `15247044348`; event-study artifact `15247812336`.
- Both DB artifacts independently inspected: SQLite `integrity_check=ok`, `foreign_key_check` empty. Each has one capture session, 1,923 provenance observations, 480 features (240 per symbol), 482 candles, 960 derivatives snapshots, and 11 collector events; both BTC-USDT and ETH-USDT present, six WS subscription acknowledgements, clean stop.
- Capture #9 depth samples were zero in 1 BTC feature minute and 2 ETH feature minutes; #12 had zero in 1 BTC minute and 1 ETH minute. These are isolated sparse-depth minutes, not missing feature rows.
- The two four-hour windows are adjacent but not continuous: #9's last feature minute is 00:07 UTC, #12's first is 00:09 UTC. Preserve this one-minute gap as a segment boundary; do not calculate rolling features or forward labels across it.
- Both event studies use a fixed 12 bps round-trip cost. All four hypotheses have negative mean net returns at all 1m/5m/15m horizons in both captures. #9 has only 1–2 independent 24h clusters per horizon; #12 has one. All remain `insufficient_independent_clusters` (minimum required: 4). Results are exploratory and must not be used as trade instructions or production signals.
- No production code, strategy, risk, leverage, DB, credentials, or deployment changed.

## Permission ledger

- PR #19: user authorized merge into research branch `research/capture-v2-4h-run` only; merged there.
- PR #23: merged and deployed with user authorization.
- PR #24: user authorized merge and deployment; merged and deployed.
- PR #25: merge authorized and merged; deployment not verified.
- PR #26: merge authorized and merged; deployment NOT authorized/verified.
- PR #20: broad user permission covered routine research work; six-artifact integration passed and it was merged only into `research/capture-v2-4h-run`; no production `main` changes.
- PR #21: remains Draft; not merged because fail-closed startup behavior depends on the real production DB passing integrity/FK checks, which has not been inspected. Keep it separate until production DB health is verified.
- PR #22: merged to `main` after exact-head CI passed; no deployment permission inferred.
- PR #27: explicitly authorized by user's broad approval and merged; documentation-only.
- PR #28: explicitly authorized by user's broad approval and merged; documentation-only.
- PR #29: explicitly authorized by user's broad approval and merged; documentation-only.
- PR #30: explicitly authorized by user's broad approval and merged; documentation-only.
- PR #31: broad user permission covered routine research work; gap-aware pooled event-study validation passed and it was merged only into `research/capture-v2-4h-run`; no production `main` changes.
- PR #32: update context after PR #30 and PR #31 merges; documentation-only.
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
