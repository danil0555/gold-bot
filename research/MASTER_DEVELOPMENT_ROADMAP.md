## 2026-11-25 — exact latest PR-head validation and coverage-gate clarification
- Re-ran validation after the roadmap update. GitHub Actions run `45612012450` completed successfully on exact PR #12 head `47a4fcda6cf6f3a54fdfcd152b90f9b73dc0b549`; the log confirms **158 passed in 9.51s**, runtime import succeeded, and real orders/add-on strategies remained disabled in CI.
- Capture run `45573371850` used older checked-out SHA `5bf6a78598c2e06f80a1a3d21b6f81b747c06203`. Its merged-data check reported `753 unique minutes across 2 UTC dates; holdout_ready=False`. No frozen holdout or pattern-mining run was performed.
- The latest research branch now contains `scripts/multiday_coverage_gate.py`, which checks each expected symbol independently for at least 600 closed minutes and 3 UTC dates. That newer per-symbol gate was added after the capture run's checked-out SHA, so it was not the gate executed in run `45573371850`; it will apply to a future capture dispatched from the updated branch.
- The 4h raw capture itself is clean for candle continuity; the merged historical sample consists of three separate capture windows, not continuous history. Preserve this distinction in every downstream analysis.
- PR #12 remains Draft/open/unmerged and non-mergeable/dirty due to its stacked base advancing. No rebase or merge attempted.

## 2026-11-25 — BingX capture analysis and validation result
- Four-hour capture workflow `45573371850` completed successfully on `research/market-discovery-engine-v1` at SHA `5bf6a78598c2e06f80a1a3d21b6f81b747c06203`.
- Current capture data quality: 7 symbols (ADA, BTC, DOGE, ETH, LINK, SOL, XRP); 1,687 closed 1m candles; 1,680 feature minutes; 283,396 depth samples; 314,174 trades; 3,360 derivatives snapshots; zero missing 1m candles in the current four-hour interval; depth minute coverage 98.75–100%; maximum derivatives gap 30s; no collector warnings.
- The merged research database combines three capture sessions and contains 5,271 candles, 5,250 feature rows, and 10,500 derivatives snapshots. Important limitation: this is not continuous multi-day data. Per-symbol candles cover three separate windows (2026-11-24 06:27–10:57 UTC; 2026-11-24 19:10–23:10 UTC; 2026-11-25 08:05–12:05 UTC), with gaps of 493 and 535 minutes. There are only two UTC dates and one independent 24h cluster for inference.
- Pre-event discovery found 23 episodes: 9 upward and 8 downward 5m events, 5 downward 15m events, and 1 downward 30m event. All direction/horizon groups report `insufficient_independent_days`; discovery is exploratory only.
- Microstructure event study used 12 bps round-trip cost and 5m event thinning. Every event/horizon group has only one independent 24h cluster and is marked `insufficient_independent_clusters`. All reported mean net returns are negative; closest to break-even is buy-absorption SHORT hypothesis at 15m: -2.90 bps net (before any additional execution uncertainty). This is not a validated edge and must not be traded.
- GitHub validation run `45602287920` on exact PR #12 head `b15d0a69451be1e150d54cd6dafbb949994843cc` completed successfully; compile, dependency install, safe-default import, and deterministic research/strategy test step all succeeded. Read-only optional market/backtest steps were skipped by workflow conditions. The previous cancelled run `45602144553` is superseded by this successful run.
- Artifacts from the successful capture are available until 2026-12-09 or 2026-12-25 depending on artifact; retain the aggregate, data-quality, event-study, discovery, and raw-capture artifacts for further research.
- PR #12 remains Draft/open/unmerged; GitHub still reports a dirty/non-mergeable state because its base branch has advanced. No rebase, retarget, merge, or force-push.
- Next research requirement: collect at least four independent UTC 24h clusters, preferably across distinct market regimes, with full per-symbol candle/depth/trade/derivatives coverage. Keep strict missing-interval checks and avoid concatenating separate sessions as if continuous. Re-run event discovery, matched controls, walk-forward and execution-cost tests only after coverage gate passes.
- Production `main`, DB, hosting, credentials, deployment, and exchange orders remain untouched.

## 2026-11-25 — corrected tests submitted for CI
- Test-only fixes are now on the PR #12 branch: Strategy B replay fixture extended to 416 one-minute rows, yielding exactly 212 eligible decisions after the 205-row history requirement; execution-cost tests now expect direction-adjusted positive SHORT return and correctly distinguish missing entry candle (`minute_ts == signal_ts`) from missing exit candle.
- Commits: `93234b5dad8f042de7602d12766e0fe0d1cfc846` (replay fixture), `a63894fa70d95b1decee56c2a00c77aacb547665` (execution-cost assertions), `a5833603d48b390d8815b0e95a620361f3d89964` (roadmap).
- New validation run `45602144553` was queued on the exact new head `a5833603d48b390d8815b0e95a620361f3d89964`; at 11:12 UTC its dependency-install step was in progress. No test result yet.
- Prior failed run `45598368312` remains recorded as 5 failed / 153 passed. The corrections have not yet been proven until run `45602144553` completes.
- Correction to capture timing: run `45573371850` started 08:06 UTC; at 11:12 UTC it had run about 3h06 of the configured 4h capture. Its collector step was still within the expected capture duration, not demonstrably hung. Artifacts are uploaded only in later steps.
- PR #12 remains Draft/open/unmerged and GitHub currently reports it non-mergeable/dirty with its base branch 5 commits ahead. Do not rebase/retarget/merge without a separate audit.
- Production `main`, DB, hosting, credentials, deployment, and exchange orders remain untouched.

## 2026-11-25 — CI failure audit and regression-test corrections
- Inspected failed GitHub Actions run `45598368312` on PR #12 head `e435f6d3be1c7a659ec72ff3ba906f90e98fd0f5`: compileall and runtime safe-default import passed; deterministic suite reported 5 failures and 153 passes.
- Three failures were stale execution-cost assertions: a profitable SHORT must have positive direction-adjusted gross return, and removing the candle keyed by `signal_ts` removes the entry candle (removing `signal_ts + 1m` removes the exit candle).
- Two replay failures were caused by an undersized 1m fixture: only 8 decisions had the required 205 closed 1m bars, so it could not exercise the 30-minute cooldown or support the asserted 212 rejection observations. Extended the 1m fixture from 212 to 416 rows; this yields 212 eligible decisions while preserving the 205-row minimum-history condition.
- Updated tests only; no strategy, production code, execution model, or thresholds changed. Test commits: replay fixture `93234b5dad8f042de7602d12766e0fe0d1cfc846`; execution-cost expectations `a63894fa70d95b1decee56c2a00c77aacb547665`.
- New CI must validate the resulting branch head. The previous failure remains recorded and is not to be treated as a pass.
- PR #12 remains Draft/open/unmerged. Its base branch has advanced relative to the PR head and GitHub reports a non-mergeable/dirty state; no rebase, retarget, merge, or force-push was attempted.
- At the latest inspection (11:12 UTC), BingX capture run `45573371850` had been active for about 3h06 of its configured 4h capture duration. The public-data collector step was still running as expected; no artifacts are expected until later workflow steps. No data-derived conclusions are available.
- Production `main`, production database, hosting, credentials, deployment, and exchange orders remain untouched.
- Next: verify CI on the new head; if green, separately resolve the stacked-PR base divergence with an explicit, reviewable plan before any merge consideration.

## 2026-11-25 — per-symbol Strategy B rejection breakdown
- Extended the historical replay diagnostics to group rejection reasons, stages, and trend-check combinations by symbol as well as globally.
- This prevents aggregate statistics from hiding that a filter may be restrictive on one instrument but not another. It remains descriptive diagnostics only; no filter thresholds, signal criteria, or Strategy B decisions changed.
- Added a deterministic two-symbol regression test verifying independent reason/stage/trend-state counts.
- Commits: source `cbd7040e226d58ef618e35ed880aaeb546bda880`; test `88a979265b6f70a1e4b4de379501c2dd1f488628`. CI is not yet confirmed for the updated head.
- Production `main`, database, hosting, credentials, deployment, and exchange orders remain untouched.
- Next: confirm the full validation workflow on the latest branch SHA; then evaluate per-symbol rejection distributions on multi-day capture data before proposing isolated shadow experiments.

## 2026-11-25 — expose Strategy B trend-check rejection combinations
- Extended the replay diagnostics to preserve the production builder's existing `trend_checks` string when trend alignment fails. The report now counts exact BUY/SELL sub-check combinations in addition to rejection reason and stage.
- This distinguishes common alignment patterns (for example, 1H trend agrees but 5m momentum does not) without changing any trading condition or interpreting the combinations as causal probabilities.
- Added a deterministic test for aggregation of two distinct trend-check states.
- Commits: source `bfc5e46ec9a89c738074ca2d173854670c800455`; test `f5ca5aa93eb7280b0ceb498562b8b37c79b4140c`. PR #12 remains research-only; CI is not yet confirmed for this new head.
- Production `main`, DB, hosting, credentials, deployment, and exchange orders remain untouched.
- Next: obtain CI evidence for the new head, then evaluate per-symbol and per-regime rejection distributions on multi-day data before considering separate shadow-only filter experiments.

## 2026-11-25 — Strategy B replay rejection funnel diagnostics
- Continued the planned frequency-versus-quality investigation without changing Strategy B rules, thresholds, signal output criteria, production code, or live behavior.
- The historical replay now passes a diagnostics dictionary to the checked-out production `build_signal_b` and counts its existing rejection reasons and stages when no signal is produced. Injected test builders retain their prior call contract.
- This makes it possible to distinguish trend-alignment, structure, VWAP, volume, RR, and other current filter bottlenecks before proposing any threshold changes. Counts are diagnostic labels, not probabilities; label text can change with builder revisions.
- Added a deterministic test that verifies aggregation of multiple rejection reasons and stages.
- Changes are on `research/prediction-quality-layer-v1`, PR #12 Draft/open/unmerged. Source commit: `836c71a7527ca05c57f4d4ffe428c322640f81ca`; test commit: `640aee21ccaf340fcd55b5ea10617b58571276b7`.
- CI for the resulting branch head must be checked before treating the test as passed. No local test run is claimed.
- The four-hour BingX capture `45573371850` was still in progress at inspection, with the capture step active and no artifacts; no market conclusions are available yet.
- Production `main`, production DB, hosting, credentials, deployment, and exchange orders remain untouched.
- Next: verify CI on the updated PR #12 head; then use rejection-funnel counts alongside multi-day captured data to identify which existing filters merit separate shadow experiments, without relaxing Strategy B in production.

## 2026-11-25 — Strategy B replay now mirrors production signal cooldown
- Audited production `save_signal`: duplicate `setup_key` is checked first, then a 30-minute cooldown is enforced by market, symbol, side, and strategy version. The cooldown boundary is inclusive (a new signal at exactly 30 minutes is still rejected).
- The historical replay previously deduplicated only identical setup keys and could therefore overstate signal frequency versus production.
- Updated `scripts/strategy_b_replay.py` to read `SIGNAL_COOLDOWN_MINUTES` from the checked-out bot module (30 minutes in the current production source), apply it per market/symbol/side/version, and report `cooldown_rejected` plus the applied cooldown. Cooldown-rejected setup keys are not marked as saved, matching the production insert path.
- Added tests for exact-boundary rejection, re-eligibility after 30 minutes, and independent LONG/SHORT cooldowns.
- Commits: replay implementation `aa0a86534baf6021b26744dc771b4b818db00649`; regression tests `18933e649ea35a914bc27ed9f047e8d4bca8cbad`.
- Validation: CI for these new commits is pending; do not treat the new tests as passed until the run for the updated PR #12 head completes.
- Branch relationship: PR #12 head was 5 commits behind its base branch `research/market-discovery-engine-v1`. The missing per-symbol multi-day coverage-gate files are present on the base branch, not yet on this PR #12 head. Do not mistake that branch divergence for a missing test in the replay CI; reconcile the two research branches before interpreting a combined result.
- The separate base-branch validation run `45574114071` completed successfully with 101 deterministic tests passed, including the multi-day coverage branch changes; it is not validation of PR #12's later commits.
- Four-hour capture `45573371850` was still in progress at the latest inspection and had no artifacts. Production `main`, `trading_bot_v9.db`, credentials, deployment, and exchange orders remain untouched; PR #12 remains Draft/open/unmerged.

## 2026-11-25 — execution timestamp hardening commit record
- Timestamp alignment fix in `scripts/execution_cost_backtest.py`: `298f7b59c143a6fe64d4b79bd554828ca3ae9b7e`; methodology wording: `941ec7918b05fc8843362de424e3630b7c96a73c`; strict timestamp validation: `4fd816d371af4929dbd4529a034965b7bf0433f8`.
- Regression tests in `tests/test_execution_cost_backtest.py`: `b62a6de3ebdd56de1d25b2a0df91e510c6d4fe8d` and `e59bb0b23b653fe347d8f5274ad57f793d06551b`.
- Replay candle continuity/alignment guard: `dd46c0739f0a8953ac1eabfa83ba23fd47c1813a`; regression tests: `985d610365f5105c4f8484af7104b79ab93a092a`.
- Roadmap entries: `ba590c488a1d7006782f84a005dbef6edcae98ac`, `527b68fda3d4fff5e0f7866a9d83c5f5a6ec85bf`, and this commit.
- Current PR #12 head before this roadmap-only update: `314cdbca4924ae5af722b7d8d33f220fdd02cf55`. PR remains Draft/open/unmerged; no CI status checks are visible yet.
## 2026-11-25 — strict execution timestamp validation
- Execution evaluator now rejects null, non-numeric, non-finite or non-minute-aligned candle timestamps and rejects non-finite/non-minute-aligned signal close timestamps. This prevents silent truncation or accidental timestamp coercion.
- Added regression tests for a missing exact exit candle and misaligned signal/candle timestamps.
## 2026-11-25 — execution model follow-up
- Corrected the evaluator's methodology text to describe the actual OPEN-time candle lookup precisely: signal candle close is timestamp `signal_ts`; entry is the close of the candle opening at `signal_ts`; exit is the close of the candle opening at `signal_ts + horizon_minutes`.
- The recorded execution times are those candle closes, one minute after each corresponding OPEN timestamp.
- The old evaluator methodology statement was stale after the timestamp fix; corrected to prevent future confusion.
- Current validation status: workflow file includes both affected test modules in its deterministic test command, and the research branch is configured for push validation. The connector's status endpoint currently returns no status checks for the latest head, so tests are not yet claimed as passed.
## 2026-11-25 — Corrected backtest execution timestamp convention
- Audit found a one-minute forward shift in the close-to-close evaluator: signal timestamps represent the signal candle close, but the lookup used candle OPEN keys at signal_ts+1m for the entry close. Because each row's price is that candle's close, this selected the close one full minute too late.
- Corrected lookup: entry price is the close of the candle with open timestamp `signal_ts`; exit price is the close of the candle with open timestamp `signal_ts + horizon_minutes*60_000`. Recorded execution timestamps remain the corresponding candle close times (`signal_ts+1m` and plus horizon).
- Updated exact expected-price/time tests and missing-entry test. Prior backtest reports generated by the old evaluator must be discarded and rerun.
- Hardened Strategy B replay inputs: timeframe opens must align to the interval and all candles in each supplied timeframe must be contiguous. Gaps now fail closed rather than allowing indicator calculations across missing periods.
- Added tests for candle gaps and timeframe misalignment.
- CI for current PR head remains unconfirmed. No production changes or live trading.
## 2026-11-25 — Strategy B replay hardening commit record
- `scripts/strategy_b_replay.py`: validation/provenance commit `e33fc5ca0c021bd14c66e2e14ea9c741cf8c8528`; finite-number validation cleanup `8a1feacecc245b5ddd8389a87df7d98ee9a4220a`.
- `tests/test_strategy_b_replay.py`: regression tests commit `37a1e7d916232ac3f3167f82e78c0a26e664b5e4`.
- Roadmap audit entry commit `15b5245eb7099ba809ef121ea32110d6b66f7ae3`.
- Current PR #12 head after these commits: `8a1feacecc245b5ddd8389a87df7d98ee9a4220a`. PR remains Draft/open/unmerged. CI status for this exact SHA is not yet exposed by the available run/status lookup; do not infer success from the older successful run.
## 2026-11-25 — Strategy B replay adapter hardening
- Audited the replay adapter against both production `main.py` and the research-branch copy. The complete `build_signal_b` function body is byte-for-byte identical in the two checked files; the research branch's full `main.py` differs because of unrelated Spot–Futures/UI and reliability changes. Therefore a full-file hash must not be mislabeled as a production-source hash.
- Renamed replay provenance fields to `checked_out_bot_version` and `checked_out_main_sha256`; added `strategy_builder_sha256` as a separate fingerprint of the exact imported `build_signal_b` source.
- Hardened signal validation: only BUY/SELL are accepted; required numeric fields must be finite and positive; SL/entry/TP ordering must be directionally valid; malformed quote objects and non-mapping builder results are rejected. This prevents unknown sides from silently becoming SHORT trades.
- Added deterministic tests for invalid side, inconsistent price ordering, non-finite output and a valid SHORT signal.
- Verified the execution evaluator uses signal close + 1 minute as entry and exact-horizon close as exit; this matches the replay's decision timestamp convention.
- Existing CI run `45574114071` passed deterministic tests on its own checked-out SHA, but it is not evidence for the current PR #12 head `d952f70a`. New CI for the new commits is required.
- The four-hour public BingX capture `45573371850` remains in progress and has no artifacts yet. No performance conclusions can be drawn.
- Production branch, database, credentials, deployment and exchange orders remain untouched; PR #12 remains Draft and unmerged.
- Next: commit the validation hardening, confirm CI on the resulting PR #12 head, then validate multi-timeframe historical candle coverage before any Strategy B replay.
## 2026-11-25 — per-symbol multi-day holdout coverage gate
- Roadmap audit identified a weakness in the capture workflow: holdout readiness used pooled distinct-minute/date counts across the entire merged database. A well-covered symbol could therefore hide inadequate history for another symbol.
- Added read-only `scripts/multiday_coverage_gate.py`. It checks every observed symbol independently and also checks all explicitly expected symbols from the current capture configuration. Each must meet at least 600 closed one-minute candles and 3 distinct UTC dates before the multi-day holdout pipeline is allowed to run.
- The report records per-symbol minute/day counts, first/last timestamps, missing expected symbols, and not-ready symbols. The gate is a history sufficiency check only; it does not establish data quality or strategy profitability.
- Added deterministic tests for per-symbol thresholds, pooled-coverage false positives, missing expected symbols, exclusion of open candles, empty history, and invalid thresholds.
- Updated `.github/workflows/microstructure-capture.yml` to use the gate and upload its JSON report; updated deterministic CI test list.
- Commits on `research/market-discovery-engine-v1`: implementation `3910facc168f7fa37e7aad2e46f0fa8a52b4f4e2`; tests `0db95edd6115e070868c5f73b3ecf266d55ea949`; workflow `147dd57f83bbfc51f63a1f54273e0869f9323e49`; CI registration `ed63fcde9504f77b1af93ee85dbd50cfaba16fde`; test cleanup / current branch HEAD `77a8413b39e4d94ad25a6e4dd2f8df6d9dd619b6`.
- Validation: push CI run `45574114071` is queued for the final HEAD. Earlier per-file runs were cancelled by the workflow's concurrency policy; only the final run should be used for validation.
- The active four-hour capture `45573371850` checked out its original SHA `5bf6a785` before these changes; its running workflow is unaffected. This gate applies to subsequent captures.
- PR #11 remains open/draft; PR #12 remains open/draft; no merges. Production `main`, bot DB, hosting, credentials, and exchange orders remain untouched.
- Next: confirm CI on `77a8413b`, inspect the four-hour capture artifacts, and only then interpret the multi-day coverage / holdout status.

## 2026-11-25 — rolling transfer tail-coverage diagnostics
- Audited the rolling cross-symbol transfer evaluator after the prior CI success.
- Found that the evaluator intentionally discards a trailing eligible fragment shorter than a complete holdout window, but the report did not quantify that discarded fragment.
- Added `trailing_eligible_timestamps_discarded`, `trailing_fragment_start_utc_ms`, and `trailing_fragment_end_utc_ms` to the rolling transfer configuration. This is reporting-only; fold boundaries, labels, training, holdout samples, and pattern selection are unchanged.
- Added a deterministic test for the two trailing eligible timestamps in the 300-minute synthetic panel.
- Commits: implementation `9ab5cfb486ba20ed523c1b56848dc2c62e082f4f`; test `0f86b7e2eef7adf29af77c5e1688d733698beda8`.
- Validation: CI for the new HEAD is pending; do not treat this addition as validated until its workflow run succeeds.
- The four-hour BingX capture run `45573371850` remains a separate read-only workflow; inspect its final artifacts before interpreting market patterns.
- Production `main`, production database, credentials, deployment, and exchange orders remain untouched. PR #12 remains draft and unmerged.
- Next: confirm CI for this commit, then inspect the four-hour capture report and data-quality audit.
  
# Master Development Roadmap — Trading Bot

> Единая точка отсчёта для продолжения разработки в новых чатах. Обновлять этот файл при каждом согласованном изменении плана, реализации, проверке или переносе задачи.
>
> **Правило безопасности:** этот roadmap и исследовательские изменения не дают разрешения менять production. Любое подключение к live-сигналам, изменение `main`, базы, развёртывание или реальные ордера требуют отдельного явного согласия пользователя.

## 1. Текущая архитектура и неизменяемые ограничения

- Production: ветка `main`, бот `@ZolotoAnalitiiikBot`, хостинг FadeHost.
- Production `main.py`, production DB `trading_bot_v9.db`, секреты, настройки хостинга и рабочий бот не изменять без отдельного разрешения.
- Не выполнять merge PR, deploy, миграции/пересоздание БД или реальные ордера без отдельного явного разрешения.
- Исследования выполнять в отдельных ветках и на отдельных исследовательских данных; по умолчанию только read-only / shadow mode.
- Не выдавать корреляцию, backtest или небольшой holdout за доказанную причинность или гарантию будущей доходности.
- Не подключать исследовательские оценки к live-сигналам, пока не пройдены проверки качества данных, отсутствие look-ahead, хронологическая out-of-sample проверка, учёт издержек и отдельное согласование.
- При изменении кода проверять риск для БД и явно предупреждать о любых миграциях, удалении или пересоздании данных.

## 2. Уже существует в production (не означает, что надо менять)

- Strategy B — действующая базовая стратегия; её поведение и формулы сохранять, пока отдельное исследование не покажет устойчивое преимущество альтернативы и пользователь не согласует замену.
- Мультитаймфреймовый анализ, импульс/структура рынка, VWAP, объём, ATR/ADX, проверки bid/ask, срок действия сигнала и ограничение ухода цены от входа.
- Текущая оценка качества сигнала является формульным score, а не обученной/калиброванной вероятностью.
- Текущий production-бот не следует считать реализовавшим все функции персонального сопровождения сделок, перечисленные ниже.

## 3. Исследовательские ветки и PR

| PR | Направление | Состояние по последней проверке |
|---|---|---|
| [#10](https://github.com/danil0555/gold-bot/pull/10) | Направленная оценка microstructure-событий LONG/SHORT с издержками | Draft, не объединён |
| [#11](https://github.com/danil0555/gold-bot/pull/11) | Market Move Discovery: поиск условий перед сильными движениями, паттерны и хронологический holdout | Draft, не объединён |
| [#12](https://github.com/danil0555/gold-bot/pull/12) | Prediction Quality Layer: рыночный контекст, калибровка, Brier score, ошибки, net return | Draft, не объединён |
| [#3](https://github.com/danil0555/gold-bot/pull/3) | Read-only Spot/perpetual cash-and-carry / basis analysis | Draft, не объединён |
| [#8](https://github.com/danil0555/gold-bot/pull/8) | Изолированная 30-дневная проверка Strategy B | Draft, не объединён |
| [#2](https://github.com/danil0555/gold-bot/pull/2) | Исторический read-only backtest Strategy B | PR; перед использованием результатов перепроверять CI и методологию |

Статусы в этой таблице — контрольная запись на момент последнего обновления roadmap. Перед любым решением проверять фактическое состояние GitHub и SHA веток.

## 4. Уже добавлено в исследовательские ветки (требует проверки CI/данных)

### 4.1 Directional microstructure scoring
- Оценивать событие по его заявленной гипотезе направления LONG или SHORT.
- Для SHORT инвертировать forward return перед вычитанием round-trip costs.
- Показывать gross/net returns и число независимых 24-часовых кластеров.
- Не считать несколько событий внутри одного рыночного эпизода независимыми доказательствами.

### 4.2 Market Move Discovery
- Искать последовательные эпизоды значимого движения на горизонтах 5m/15m/30m.
- Собирать признаки, доступные на старте эпизода, без использования будущего.
- Сравнивать события с нейтральными контрольными интервалами по тому же инструменту и горизонту.
- Искать повторяющиеся сочетания признаков до движения.
- Разделять данные хронологически: обучение, purge gap, затем замороженная проверка на более позднем holdout.
- Объединять независимые сессии сбора данных, сохраняя диагностику качества и устраняя дубликаты рыночных строк.

### 4.3 Prediction Quality Layer (PR #12)
- Причинные returns 1m/5m/15m/60m, realized volatility, trend efficiency и описательные market-regime labels.
- Контекст BTC и относительная доходность инструмента.
- Backward-only присоединение funding/OI snapshots с проверкой свежести.
- OI/price positioning quadrants — только косвенные proxy, не реальные liquidation prints.
- Reliability-bin probability calibration — только обучение на train; применение только замороженного калибратора.
- Brier score и observed-vs-predicted probability на отдельном holdout.
- Разбор ошибок по режиму и направлению.
- Net return с учётом комиссий, проскальзывания и funding.
- Всё остаётся shadow-only; наличие кода не означает, что калибратор обучен или что regime labels предсказывают доходность.

## 5. Главная исследовательская задача: собственные стратегии из повторяющихся движений цены

Это отдельный приоритетный исследовательский процесс, который пользователь описал как итеративный поиск закономерностей:

1. Найти заметное движение цены вверх или вниз.
2. Зафиксировать точное время начала, направление, амплитуду и горизонт движения.
3. Восстановить только доступные ДО движения данные: свечи, объём, волатильность, структура/уровни, BTC-контекст, глубина/сделки, OI/funding и другие доступные признаки.
4. Сформировать несколько проверяемых объясняющих гипотез, не объявляя ни одну из них причиной заранее.
5. Найти похожие исторические ситуации и сравнить, что происходило после них.
6. Сравнить с контрольными интервалами, где похожие условия не привели к такому движению.
7. Проверить повторяемость на других датах, инструментах и рыночных режимах.
8. Зафиксировать правила кандидата стратегии только на обучающей части.
9. Заморозить правила и проверить на более позднем независимом holdout.
10. Учесть комиссии, спред, проскальзывание, funding, задержку и реализуемость входа/выхода.
11. Вести журнал всех проверенных гипотез, включая отрицательные результаты, чтобы не повторять неудачные поиски и не выбирать только удачные примеры.
12. Только после устойчивой независимой проверки рассматривать paper/shadow сигнал; live-интеграция — отдельное решение пользователя.

Обязательные защиты от самообмана:
- Никаких признаков, вычисленных после момента входа, в pre-move features.
- Не выбирать события по величине их будущего движения, а затем оценивать на той же выборке.
- Не подбирать пороги на holdout.
- Не делать случайное перемешивание временных рядов; использовать хронологические split и purge gap.
- Учитывать множественный перебор паттернов и малое число независимых эпизодов.
- Отдельно сообщать coverage, пропуски и качество данных.
- Положительный результат одной короткой сессии считать только наблюдением, а не стратегией.

## 6. Запланированные исследовательские улучшения

### Приоритет A — качество доказательств
- Проверить PR #10/#11/#12 и все новые изменения на CI; не считать тесты пройденными до фактического успешного запуска на нужном SHA.
- Проверить отсутствие look-ahead в каждом признаке, join, label, split и отчёте.
- Исправить/проверить граничные случаи OI proxy: нулевое изменение OI не должно автоматически классифицироваться как рост/снижение.
- Добавить тесты на пропущенные минуты, нерегулярные timestamps, stale derivatives, отсутствие BTC benchmark и строгую причинность joins.
- Проверить, что калибровка обучается только на train, а метрики считаются только на независимом holdout.
- Проверить, что издержки и funding не вычитаются дважды и что знак SHORT обработан правильно.
- Повысить качество контроля: сопоставление по режиму волатильности/времени суток, достаточное число независимых дат/кластеров, доверительные интервалы и поправка/контроль множественного тестирования.
- Указывать минимальный объём данных и автоматически блокировать вывод «кандидат стратегии», если выборка недостаточна.

### Приоритет B — накопление и анализ данных
- Продолжать read-only BingX capture для свечей, публичных сделок, order book/depth и derivatives snapshots.
- Накопить несколько независимых UTC-дней; короткая сессия не заменяет многодневную проверку.
- Сохранять сырые артефакты, метаданные запуска, список инструментов, интервалы, таймзону и диагностические показатели.
- После каждой сессии обновлять общий датасет и отчёт покрытия; не смешивать разные схемы без явной версии.
- Добавить анализ устойчивости закономерностей по символам, датам, волатильности и направлениям.

### Приоритет C — сравнение и отбор стратегий
- Использовать текущую Strategy B как неизменяемую контрольную базу.
- Сравнивать кандидатов с B на одинаковых инструментах, периодах, правилах исполнения и издержках.
- Проверять не только win rate, но и net expectancy, profit factor, drawdown, R distribution, число сделок, стабильность по датам/символам и чувствительность к параметрам.
- Разделять качество прогноза направления и реальную прибыльность исполнимой сделки.
- Не заменять B из-за одной метрики или одного удачного периода.
- Разрабатывать несколько независимых кандидатов и затем проверять, даёт ли их комбинация пользу, не создавая чрезмерную сложность.

### Приоритет D — режимы рынка и вероятности
- Проверить, несут ли market-regime features дополнительную out-of-sample ценность.
- Проверить BTC benchmark и relative strength как контекст, а не как автоматическое правило.
- Оценить OI/funding/price combinations; не называть их ликвидациями без реальных liquidation records.
- Обучить калибратор только при достаточном количестве независимых наблюдений; сравнить Brier score/log loss и reliability до/после.
- Сохранять исходный score и калиброванную вероятность отдельно; не показывать псевдоточную вероятность без доказанной калибровки.

### Приоритет E — Spot и Futures
- Исследовать Spot и USDT-M Futures раздельно, учитывая разные механики исполнения и издержки.
- Продолжить read-only анализ basis/funding/cash-and-carry как отдельную возможность, не смешивая его с directional strategy.
- Сначала только публичные данные и ручной анализ; никаких API-ордеров или автоматического исполнения.

## 7. Будущие функции Telegram и сопровождения сделок (ещё не реализованы, если не подтверждено кодом)

- Под прогнозом оставить основную кнопку «Я вошёл в сделку»; не добавлять кнопку «Просто наблюдаю».
- До подтверждения входа прогноз не становится персонально сопровождаемой сделкой.
- После подтверждения пользователь вручную указывает фактическую цену входа.
- Отдельный раздел «Мои сделки» показывает только подтверждённые пользователем сделки; лимит — не более 5 одновременно сопровождаемых сделок.
- Для каждой сделки показывать состояние, фактический вход, текущую цену, SL, TP1/TP2, последнюю рекомендацию и остаток позиции.
- Действия: обновить данные; исправить фактическую цену входа; обновить вручную изменённый SL; закрыть/остановить сопровождение с подтверждением.
- Отдельная история сделок с результатом, R и USDT.
- Уведомлять только о важных изменениях, не отправлять лишние сообщения.
- На первом этапе только подсказки и ручной перенос параметров в BingX; не размещать реальные ордера через API.
- До реализации сверить требования с текущей схемой БД и сохранить существующие данные.

## 8. Баланс частоты и качества сигналов (целевое направление, не повод менять production без проверки)

- Цель — баланс количества и качества подтверждённых сигналов, а не максимальное число сигналов.
- Постоянно отслеживать рынок, но отправлять сигнал только при подтверждённом сетапе.
- Исследовать Spot + USDT-M Futures.
- Сохранять текущую стратегию как baseline; новые фильтры/кандидаты сравнивать отдельно.
- Любое изменение score/threshold сначала тестировать в shadow mode и на историческом holdout.

## 9. Отложено / отсутствует / требует отдельного источника данных

- Прямой анализ liquidation prints: текущий collector не предоставляет прямые данные о ликвидациях; OI/price proxy не заменяет их.
- Достоверная probability calibration: код утилит есть, но калибратор нельзя считать обученным до корректного train/holdout.
- Реальная оценка исполнения в live: историческая модель издержек остаётся приближением.
- Telegram trade-monitoring: требования записаны, реализацию надо проверить отдельно.
- Spot + Futures полноценный анализ/сигналы: не считать готовыми только потому, что есть отдельный basis research PR.
- Автоматическая торговля/API ордера: не входят в текущий разрешённый объём.

## 10. Журнал решений и прогресса

Каждое обновление добавлять сверху с датой UTC:
- Что пользователь согласовал.
- Что изменено (ветка, PR, commit SHA, файлы).
- Что проверено (точный CI run и SHA; отдельно локальные проверки).
- Что не проверено и почему.
- Результат исследования, включая отрицательный/неопределённый.
- Следующий конкретный шаг.
- Любые изменения статуса задач из разделов выше.

### Обновление аудита (2026-11-24, поздний вечер)

- В `scripts/prediction_quality_layer.py` исправлено: 1m return теперь использует точный timestamp lag; 30m/60m volatility, 60m path и 6h volatility reference используют time-based rolling windows, чтобы пропуски не считались последовательными минутами.
- Исправлена обработка нейтральных значений: нулевое изменение OI или цены оставляет `positioning_proxy=unknown`, а не принудительно относит строку к одной из четырёх сторонних категорий.
- Уточнён OI delta: сохраняется фактический интервал между двумя реально сопоставленными derivatives snapshots; изменение OI принимается только при интервале 4–6 минут. Нерегулярные пары snapshots остаются unknown, чтобы метрика `oi_change_5m_pct` не выдавала, например, 7-минутное изменение за 5-минутное.
- Добавлен тест на snapshots с интервалом 7 минут, которые оба проходят freshness, но не должны давать валидный 5m OI delta.
- Нулевые/отрицательные значения OI больше не используются для расчёта изменения; shadow report отдельно показывает покрытие валидного OI delta, а не только наличие свежих derivatives snapshots.
- Добавлен тест на non-positive OI и отдельное поле coverage в shadow report.


- Исправлено отсутствие BTC self-relative context: ноль выставляется только когда соответствующий BTC return уже существует; ранние строки остаются NaN.
- Добавлена валидация конечности и положительности close.
- Добавлены тесты на gap после пропущенных минут, time-window coverage, ранний BTC return и нулевой OI change.
- Коммиты аудита: `cb25b4047887be96c86254c656cf5e17753ea99f` (gap-aware features), `20c8f099f5818cbd480f76101df0c4cf9e473a13` (тесты на gaps/neutral OI), `70d3edf314273aefcbe96212e96e2873fc3dee92` (проверка фактического OI snapshot interval), `b6d3b1d0d0b30afdff97983d628e0013bc41a59e` (тест нерегулярного OI интервала), `2a985137a60bd0c8aa93f3069bc92aa15e54e3f5` (валидация положительного OI и coverage), `95630c277308555581450bcec799926627000745` (тесты OI validity/report coverage).
- Эти изменения находятся в ветке `research/prediction-quality-layer-v1`, PR #12 остаётся draft и не объединён. Production не затронут.
- GitHub Actions run `45472366609` для commit `06c8f48fa359ed52a22a0d6a3e0020f8348b528e` прошёл шаги compileall, runtime import/safe defaults и полный список deterministic strategy/research tests (включая `tests/test_prediction_quality_layer.py`) со статусом `success`. Это подтверждает актуальные Python-код и тесты в указанном SHA. Предыдущий run `45472087813` также прошёл на более раннем SHA.
- BingX capture run #6 (ID `45467461076`) при последней проверке всё ещё был `in_progress`, активен сбор публичных trades/depth/candles/derivatives; аналитические шаги ещё pending.

### Последняя зафиксированная точка (2026-11-24)

- Создана исследовательская ветка `research/prediction-quality-layer-v1` и draft PR #12 от ветки `research/market-discovery-engine-v1`.
- PR #12 включает shadow prediction quality utilities, тесты, документацию и добавление ветки/теста в Python validation workflow.
- Последний известный SHA PR #12 на момент первоначального создания: `5a8facf8449959a09e9deb748559dfe957f2b9e2`; актуальный SHA нужно брать из PR #12, так как ветка продолжает обновляться.
- На момент записи CI для этого SHA ещё не был подтверждён; не утверждать, что тесты прошли.
- BingX capture run #6 (ID `45467461076`) на ветке `research/market-discovery-engine-v1`, SHA `5bf6a78598c2e06f80a1a3d21b6f81b747c06203`, был `in_progress` на последней проверке.
- Production `main`, production database, хостинг и реальные ордера не менялись.
- Следующий шаг: проверить PR #12/CI и продолжить аудит качества/causality; затем обновить этот roadmap фактическим статусом.


## 2026-11-24 — OI snapshot interval correction (in progress)
- Audit confirmed that the Market Move Discovery loader previously calculated `open_interest_change_pct` with a candle-row `groupby().pct_change()` after an as-of join. That can compare repeated carried-forward values or lose the true derivative snapshot interval.
- Corrected the calculation to derive OI changes directly between consecutive raw derivative snapshots, then attach the result with the existing backward-only 120-second freshness join.
- Added an explicit `open_interest_change_interval_ms` feature for auditability.
- OI change is suppressed when either OI value is non-positive, timestamps are non-increasing, or the actual snapshot interval exceeds 10 minutes.
- Added deterministic tests for accepted 5m/10m snapshot intervals, rejected longer gaps, and non-positive OI.
- Scope remains research-only. No live signals, production branch, production database, credentials, deployment, or exchange orders are touched.
- CI validation for this change is pending; do not treat the correction as validated until a successful run on the resulting code SHA.


## 2026-11-24 — CI failure analysis and compatibility fix
- CI run `45475218598` for commit `bc8be29940ea0ad225e9099595122461ca9dac63` completed with failure: 105 tests passed and 4 Market Move Discovery tests failed.
- Root cause: the discovery summary iterated the expanded optional feature list and indexed `open_interest_change_interval_ms` on older/synthetic panels that did not contain this new optional column. This was a schema-compatibility issue, not a failure of the raw-snapshot interval calculation.
- Added defensive NaN columns for absent optional discovery features inside `discover_episodes`, preserving “unknown/missing” rather than inventing zero values. Updated the synthetic fixture to include the new interval feature explicitly.
- Fix commits: script `0174f817439351f45068ffa3664c24b29f665758`; test fixture `947675cfd8ee90e1c6e9d0a9b3db272807256379`. CI for the resulting combined branch HEAD is required before considering this fix validated.
- Production `main`, live bot, database, credentials, deployment, and exchange orders remain untouched. PR #12 remains draft and unmerged.
- Next: inspect the new CI run; if successful, proceed to matched-control comparisons stratified by volatility regime and UTC time-of-day.


## 2026-11-24 — volatility/time matched controls for pre-move mining
- Confirmed CI run `45475613310` completed successfully on branch SHA `4e207f6b94cac3e31e8f72a895905c3700937de8`. The syntax job compiled Python, imported runtime/safe defaults, and ran deterministic strategy/research tests successfully. Optional live/public scans and historical backtests were skipped by workflow conditions; this was code/test validation, not a fresh market-data experiment.
- Audited `scripts/market_pattern_mining.py`: previous neutral controls were same-symbol/same-horizon but not matched by UTC hour or volatility regime.
- Added per-symbol volatility terciles and UTC-hour labels to controls and events. Pattern comparison now retains shared strata only and standardizes control hit rates to the event stratum mix. Reports include `matched_strata_count` and an explicit control-method description.
- Added a regression assertion for matched-strata reporting in `tests/test_market_pattern_mining.py`.
- This reduces confounding from broad time-of-day and volatility differences, but is not exact matching and does not control liquidity, BTC-wide market state, funding regime, or multiple testing. Results remain exploratory; no strategy approval.
- Changes: `scripts/market_pattern_mining.py` commit `30b81a957421e4eeead851cbef3e6941dae25bb6`; `tests/test_market_pattern_mining.py` commit `f85d15035ce086f5215b8b2c3d5306de663fcbd2`.
- New CI run is required for this matched-control change; do not claim it validated until a run on the resulting code/test SHA succeeds.
- Production `main`, live bot, database, credentials, deployment, and exchange orders remain untouched. PR #12 remains draft and unmerged.
- Next: inspect CI, then add coverage diagnostics for how many events survive matching and assess sensitivity to alternative volatility bucket definitions without tuning on holdout.


## 2026-11-24 — matched controls also applied to frozen holdout
- Extended the UTC-hour/volatility-tercile control comparison into `evaluate_frozen_patterns`; training discovery and later holdout evaluation now use the same matched-strata principle.
- Holdout event volatility uses the episode's pre-move RV feature; if absent in a compatibility fixture, it falls back to the exact event timestamp's panel RV, never a future row. Event/control rates are compared only in shared strata, with controls weighted to the holdout event stratum mix.
- Added `holdout_matched_strata_count` and a regression assertion.
- Commits: `scripts/market_pattern_holdout.py` `9d3be91500ec619a1843693e11efb7cc553f0317`; `tests/test_market_pattern_holdout.py` `1abeb595bfb56023115a9c9597604b65a84754a8`.
- CI for the complete matched-control implementation is pending. Do not claim it passed until a successful workflow run covers the final code/test SHA.
- Method remains exploratory: volatility terciles and UTC hour are coarse strata; they do not match exact volatility, liquidity, BTC market state, funding or event clustering, and do not correct for multiple comparisons.


## 2026-11-24 — matched-sample coverage and missing-feature hardening
- Audited the matched-control implementation and found two compatibility defects: event records may omit precomputed rv_60m/rv_30m, and the holdout event frame did not always carry its exact event timestamp. Both could raise a KeyError or prevent exact-timestamp volatility fallback.
- Fixed event handling to preserve absent volatility as NaN/unknown and, where possible, read RV only from the exact event timestamp in the corresponding panel. No forward row is used.
- Added regression tests for event records without volatility features in both training pattern mining and frozen holdout evaluation.
- Added event_match_coverage_pct / control_match_coverage_pct to training mining and corresponding holdout coverage fields. These show how much of each original sample remains after retaining only common UTC-hour/volatility strata; a high lift with very low retained coverage must be treated cautiously.
- Corrected the pattern-mining warning to accurately describe the new coarse matching: UTC hour and per-symbol volatility tercile are matched, but exact volatility, liquidity, BTC-wide market state, funding regime, and multiple comparisons remain unresolved.
- Code commits: mining missing-feature fix 9b53be79f9de90de27a749bb7d2a8c3ba48c590c; holdout timestamp/RV fix 2204a979d5ced1bc9e18ce94e5f67a09242e08bd; training tests 44e61384aae344f4804015192cfedcf661d0ddde; holdout tests 4377e94a5fa3a86acfb3436f8784e7f47a9fbfcf; coverage diagnostics a45457b2db4c824ebf9bed173573f4896a8e9c66, 59dbeeca57db8d9803636ca2cdc34086eabde56d; coverage tests c75f2ee92b6edc5bbfb5421d735e131f9ea4ed80, 62de7a3aedd2ccc1469a11cf2e5c46d865f70600.
- Validation status: these latest commits have not yet been confirmed by a successful GitHub Actions run. The available workflow-run lookup only returns pull-request-triggered runs, while this branch workflow is configured to run on branch pushes; an empty lookup is not evidence of failure or success. Do not claim CI passed until the Actions run is directly inspected.
- Next research priorities: (1) validate coverage and matched sample sizes on multi-day captured data; (2) test sensitivity to volatility bucket definitions using training data only and freeze the choice before holdout; (3) add BTC-wide market-state and liquidity matching where data coverage permits; (4) add repeated chronological walk-forward windows and report negative/unstable patterns as prominently as positive ones.
- Production main, live bot, production database, credentials, deployment and exchange orders remain untouched. PR #12 remains draft and unmerged.


## 2026-11-24 — freeze volatility strata before holdout
- Methodology audit found that training and holdout each recalculated per-symbol volatility-tercile boundaries from their own panel. This did not use future returns to fit pattern thresholds, but it changed the control-matching definition between samples and allowed holdout distribution to determine its own matching strata.
- Added a single helper to fit per-symbol RV tercile cut points. Pattern mining now records those cut points in its report and uses them for event/control matching.
- Frozen-pattern holdout now accepts and applies the training cut points to both holdout events and holdout controls. The chronological walk-forward pipeline passes the training report's cut points unchanged; it does not recompute them on holdout.
- Holdout output now records whether volatility cuts were supplied frozen from training or derived by the compatibility fallback for direct legacy callers. The production research pipeline must report training_frozen.
- Added tests that custom training cut points are actually used for controls and that holdout reports the frozen-cut provenance.
- Code commits: mining cut-point extraction and frozen control matching 62cf6bbf37742e36e7d73e3cbaf3247bc92b7987; holdout frozen-cut application 0d0ff27e05450ed3ca69b6581203402131827175; remove holdout sample-size re-estimation guard b51b9af9ca4cde3e71b806754a80adf0d5c3a223; holdout provenance reporting 782ed7060d169743234e433a7b13770d923f7eec; mining regression test 36ddffd23e2e9e174341bff59ee6218ba0d68623; holdout regression test 4cec4cbd1def0da59aebb81cd646f3b4c69d6d60; deterministic cut test adjustment ca96fef62f3e5e32492d7f047025f23e19a2f8eb.
- Validation status: no successful Actions result has yet been observed for the current branch HEAD. The GitHub connector's workflow-run helper filters to pull-request-triggered runs, so an empty result is inconclusive for push-triggered runs. Code and tests are committed; do not claim they passed until the actual Actions run is inspected.
- Interpretation: this makes holdout matching more consistent and reduces one source of distribution drift, but does not solve sparse sample sizes, exact volatility matching, liquidity/BTC-regime confounding, multiple testing, or execution realism.
- Production main, live bot, production database, credentials, deployment and exchange orders remain untouched. PR #12 remains draft and unmerged.


## 2026-11-24 — enable CI for the research PR base branch
- Found why the GitHub connector could not show the validation run for PR #12: the workflow's pull_request trigger targeted only base branch main, while PR #12 targets research/market-discovery-engine-v1. Push validation exists, but the connector's run lookup only exposes pull-request-triggered runs.
- Expanded the pull_request target list to include research/market-discovery-engine-v1 so PR #12 receives the deterministic compile/runtime/research test job and its result can be inspected through the available connector.
- Workflow change commit: cbd3527256fec74a3f4f37d201db7a173bf17b88.
- At the time of this note, no PR-triggered run was yet visible for that commit; validation is pending, not passed.
- This only changes research CI triggers. It does not change the production workflow behavior, bot runtime, database, credentials, deployment, or exchange orders. PR #12 remains draft and unmerged.


## 2026-11-24 — search trial registry for pattern mining
- Added an explicit trial registry to the recurring-pattern mining report: number of usable features, feature pairs, state combinations per pair, matched symbol/horizon/direction groups evaluated, total candidate hypotheses evaluated, and number of patterns emitted.
- The registry is present for normal, empty-event, and insufficient-feature runs, so a report cannot hide that a zero-event run performed no hypothesis tests.
- Added deterministic assertions that the hypothesis count equals evaluated groups × feature pairs × four high/low state combinations, and that empty input records zero tests.
- This is an audit trail for search multiplicity, not a multiple-testing correction and not a p-value. The number of tried combinations must be considered before interpreting the best-looking pattern.
- Code commits: hypothesis registry efb7228421a179181890983c40f2be9697a33d82; registry tests 54c5e0f164cac6432bc4b73c970ccf660e44e67c; stable empty-run registry 19fc1a85525eb37373b19ad76b56ea5619ceff1f; empty-run test 46f4ad4e2cb29ac2d647ef46b80e7c7b39da9be7.
- Methodological basis: research on backtest overfitting emphasizes that trying many candidate strategies increases false-discovery risk, and that a single walk-forward path can still be unstable. Our next step is repeated chronological test windows and, only with enough data, a more formal multiple-testing / backtest-overfitting assessment. See the 2024 comparison of out-of-sample methods (DOI 10.1016/j.knosys.2024.112477) and the discussion of selection under multiplicity.
- Validation remains pending: the GitHub connector has not exposed a PR-triggered Actions run for the current branch commits yet. Workflow now includes PR #12's base branch in pull_request triggers; inspect the resulting run before calling tests passed.
- Production main, live bot, production database, credentials, deployment and exchange orders remain untouched. PR #12 remains draft and unmerged.


## 2026-11-24 — empty-panel compatibility guard
- Final review of the new volatility-cut helper found a compatibility edge case: the existing no-episodes unit fixture intentionally has feature columns but no symbol column. The helper now returns an empty cut map for panels without symbol data rather than raising KeyError.
- The existing no-episodes regression test exercises this path and now also verifies the trial registry reports zero hypotheses.
- Fix commit: 9c6f46675a959ab2543542deeb958e7a239ac9bd.
- This is a source-level review and regression-test addition; CI remains unconfirmed.


## 2026-11-24 — repeated chronological pattern validation
- Added `scripts/market_pattern_rolling.py` with expanding training windows and multiple later, non-overlapping holdout windows. Each fold rediscovers episodes and mines patterns only on its training segment; feature thresholds and per-symbol volatility cut points are frozen and reused unchanged in that fold's holdout.
- Each fold uses a time-based purge gap of at least 30 minutes, matching the maximum 30-minute event label horizon. Training episodes are discovered only inside the training panel, so their forward labels cannot cross into holdout.
- Added fold-level time bounds, row/event counts, trial-registry counts, holdout metrics, and pattern recurrence summaries. Recurrence identity uses symbol, horizon, direction and feature/state combination; threshold values may differ between folds and are not silently treated as identical.
- Holdout windows are required not to overlap. Expanding training sets overlap by design, so fold results are dependent; the report states this and does not treat repeated folds as independent statistical replications.
- Insufficient data returns an explicit status instead of manufacturing validation metrics. This is still descriptive research, not a strategy approval or a multiple-testing correction.
- Added deterministic tests for chronological boundaries, frozen volatility cuts, recurrence aggregation, trial counts, overlapping-window rejection and insufficient-fold reporting. Added the new test file to the GitHub Actions deterministic test command.
- Initial implementation commits: rolling evaluator `2c4f6bc430373e06876d63b35deee462fab7c449`; tests `9656e124afa5ac2ff079bbe1cf387dfec12d4a7f`. CI trigger/test-list update is committed separately.
- Validation note: workflow run `45484245735` completed compilation, runtime safe-default checks and the existing deterministic test step successfully before the rolling evaluator was added. A later CI run must cover the new rolling test before it is considered validated. The 180-day spot/perpetual backtest in that earlier run was still in progress when inspected.
- Production `main`, live bot, production database, credentials, deployment and exchange orders remain untouched. PR #12 remains draft and unmerged.


## 2026-11-24 — rolling evaluator audit and validation boundary
- Source review caught and fixed an initialization typo in the rolling pattern observation registry: it must be a dictionary because fold observations are grouped with `setdefault`. Fix commit: `c4bde6715545fc2ed4fa0422443697d77fab618a`.
- The deterministic CI command now explicitly includes `tests/test_market_pattern_rolling.py`; workflow-list commit: `ce29b68e10b773594195bd4714eac0d210e00770`.
- Run `45484926957` showed the deterministic test step completed successfully, but the overall run was cancelled during the subsequent public-data smoke scan. That run began before the workflow test-list update, so it is not accepted as proof that the new rolling test was executed. A fresh run covering the updated workflow and final branch head is still required.
- Run `45484245735` completed successfully, including the then-current deterministic test suite and the 180-day public spot/perpetual backtest; it predates the rolling evaluator and does not validate it.
- Do not describe the rolling evaluator as CI-validated until a successful run confirms the updated deterministic test command on the current branch head.


## 2026-11-24 — complete forward labels at walk-forward holdout edges
- Audit found that `discover_episodes(holdout)` discarded the final 5/15/30 minutes of each holdout because the forward return could not be computed without candles after the holdout boundary. This biased event counts and coverage downward near every test-window edge.
- Added optional inclusive `start_time_bounds` to episode discovery. A caller may now provide a label-only future tail while restricting all event starts to the actual evaluation interval.
- The rolling evaluator now gives holdout discovery up to the next 30 minutes of candles solely to complete forward labels. Frozen-pattern evaluation and neutral controls still receive only the original holdout rows; future-tail rows cannot become evaluation observations or controls.
- Each fold records requested/available label-tail minutes, whether the maximum-horizon tail is complete, and the number of additional rows used only for labels. The final dataset edge is explicitly marked incomplete when 30 future minutes do not exist.
- Regression coverage verifies that event-start bounds remain inside the holdout, the discovery frame can extend into the label-only tail, evaluation boundaries remain unchanged, and the final incomplete tail is surfaced.
- Commits: bounded event-start support `2fecd96e517d1e87e45a1d0b06d71e9fcf724b46`; rolling evaluator tail handling `4f7c09fd48c9763726bed3a62d0918ed32902994`; regression tests `1ca165a63ebd37d84b3a3f808adeda59a973a8d5`.
- The first CI run after the implementation failed in one test double: the insufficient-fold test still mocked `discover_episodes(frame)` without accepting the new optional keyword argument. The production function compiled and the other 115 tests passed. Updated the mock to accept keyword arguments; fix commit: `b9faae3e51419412578eb79a7ca78cbed12c5398`. A fresh latest-head CI run is required.
- This corrects event-label coverage, not the statistical limitations of descriptive pattern mining. It does not introduce trading signals, execution costs, strategy approval, or live behavior.
- Production `main`, `trading_bot_v9.db`, credentials, hosting/deployment and exchange orders remain untouched. PR #12 remains draft and unmerged.


- Additional direct regression test: `tests/test_market_move_discovery.py::test_bounded_event_starts_can_use_future_label_tail_without_extending_window` proves that a move beginning at the final allowed holdout timestamp is censored without the tail, detected with the tail, and still cannot start outside the bound. Test commit: `58ae47edcc915ea0a51cd43fcbc023445f980277`. A fresh CI run for this newest commit is required; an earlier run for the previous branch head is not sufficient.


## 2026-11-25 — per-symbol continuity for holdout label tails
- A further audit found that the holdout tail completeness indicator used the maximum timestamp across the entire panel. In multi-symbol research, one symbol with later candles could therefore make the report imply that every symbol had a complete future-label window, even when another symbol had stopped updating or had missing minutes.
- Replaced the global timestamp estimate with per-symbol contiguous-minute checks starting from each symbol's final holdout candle. A later timestamp after a missing minute does not count as continuous coverage.
- Fold metadata now reports the conservative minimum contiguous tail, number of symbols with a complete requested tail, number checked, and each symbol's own coverage. The flag is true only when every symbol represented in the holdout has a complete tail.
- Added a regression test with two symbols: one has a complete tail and the other has a gap; the combined fold must be marked incomplete.
- Commits: implementation `d3c1aa6678611c816b45ceba0a46f507e64e7d14`; test `af8958dfa21b1216ac5b787d3fd700edbf01dedb`.
- Latest-head CI is required before this change is treated as validated. This is a reporting/data-quality correction; it does not create a trading strategy or alter production behavior.



## 2026-11-25 — cross-symbol pattern transfer evaluator (CI pending)
- Added `scripts/market_pattern_transfer.py` to test whether pre-move feature/state combinations discovered on one source symbol also appear on other target symbols in a later chronological holdout.
- Source patterns are mined from the training panel only. For each target symbol, the numeric high/low cutoffs are recalculated from that target symbol's training history (20th/80th percentiles); raw source-symbol thresholds are deliberately not copied across coins because feature scales can differ.
- Transfer evaluation excludes the source symbol itself, preserves horizon and direction, compares target holdout events with same-target neutral controls, and retains only shared UTC-hour / training-fitted per-symbol volatility-tercile strata. Target cutoffs and volatility cuts are not fitted on holdout.
- Results include source→target pairs, target support, event/control rates, descriptive lift, target-fitted cutoffs, and explicit insufficient-coverage/support statuses. The report explicitly warns that this is not causal proof, profitability evidence, or a multiple-testing correction.
- Added deterministic tests for target-only cutoff fitting, state transfer without copying source raw thresholds, and excluding self-transfer. Added the test file to the deterministic CI command.
- Commits: evaluator `be96238ec11108ad70221b7941851d65470acdaf`; tests `d9aad61a3ea3b785336246f57e2472222dd46b1f`; CI test-list update `d2631c7ae705615c0aa3c42303d33c2070ee74e8`.
- Validation: a fresh CI run on the final branch HEAD is required. The previously successful run `45487630824` predates this feature and does not validate it.
- Known scope limits: this first transfer evaluator is a single chronological train/holdout experiment, has no fees/slippage/funding model or confidence intervals, and holdout event discovery does not yet add the rolling evaluator's label-only tail. It is exploratory and must not be used as a live strategy.
- Production `main`, `trading_bot_v9.db`, credentials, hosting/deployment and exchange orders remain untouched. PR #12 remains draft and unmerged.


## 2026-11-25 — isolate label-only tails in cross-symbol transfer (CI pending)
- Audited the first cross-symbol transfer evaluator and found that its single holdout extended to the end of the captured panel. Consequently, forward event labels near the holdout edge were censored even though the shared discovery engine supports a label-only tail.
- Reserved the final 30 minutes of the panel as label-only data. Holdout event starts are bounded to the preceding evaluation interval; tail rows are passed only to event discovery. The neutral-control builder still receives the original holdout rows, so label-only candles cannot become controls or evaluation observations.
- Applied the same rule to training: purge-gap candles may complete labels for training events, but event starts remain inside the training interval and pattern mining receives only the original training panel.
- Added per-symbol contiguous-tail coverage reporting. A later candle after a missing minute does not count as continuous coverage; completeness is true only when every symbol represented in the segment has the full requested tail.
- Added tests for per-symbol tail assembly, missing-minute continuity, reserved holdout boundary, training purge-tail isolation, and event-start bounds.
- Commits: holdout-tail implementation `720d38a126389827a4ef96a97660325db8497b80`; initial tail tests `a7b92c9cec5cb8710127847ede03c6e7945f1d53`; reserved holdout tail `0a810db0bdbd46697dc5e1d66befc23e6ecbe932`; holdout boundary tests `745cc5d69b8f29736f5883db0c867c361fef115b`; training purge-tail implementation `9a13bcf5dbb6210b42a52fa7c232f1c0dd02cdb2`; final boundary assertions `d8d687aeb970acfcd6482eacf2e3b36ac32bd736`.
- Validation: latest-head CI is pending. Earlier successful run `45488394679` covered the initial transfer evaluator and tests, but predates this label-tail correction and does not validate these new commits.
- Known limitations remain: single chronological transfer split; no fees, slippage, funding, confidence intervals, liquidity matching, or formal multiple-testing correction. This is exploratory research, not a live strategy.
- Production `main`, `trading_bot_v9.db`, credentials, hosting/deployment and exchange orders remain untouched. PR #12 remains draft and unmerged.

- Validation update: run `45490554688` on commit `783605940877ec9a946f872ad1f5dd07c52473d2` completed successfully. Compile, runtime safe-default checks, and all deterministic tests passed; the suite reported **123 passed** including the new transfer-tail tests. Public BingX smoke and 180-day Spot–Perpetual backtest steps were skipped in this push-triggered run. The earlier PR-triggered run `45488394679` on the initial transfer implementation completed both those read-only checks successfully, but it predates the tail-isolation correction.


## 2026-11-25 — cross-symbol matched-sample coverage and minimum support (CI pending)
- Tightened transfer result eligibility: a source→target transfer is counted as evaluated only when both matched target events and matched target controls meet the configured minimum support.
- Added pre-match and post-match event/control sample counts and match-coverage percentages to every fully processed transfer result. This makes it harder to mistake a high lift on a tiny matched subset for robust evidence.
- Added a deterministic regression fixture verifying event/control coverage and support reporting.
- Commits: result and support logic `6923640b53310ebcebd57790a0bbc45fa709d305`; regression test `d1fbe20caff4101258567b528b5d752a66d65154`.
- Validation: a previous revision passed 123 deterministic tests on commit `783605940877ec9a946f872ad1f5dd07c52473d2`, but that run predates this support/coverage change. Latest-head CI is required.
- Production remains untouched; PR #12 remains draft and unmerged.


## 2026-11-25 — expose cross-symbol search multiplicity and result coverage (CI pending)
- The cross-symbol transfer report previously emitted selected source patterns and transfer outcomes but did not carry forward the mining trial registry. This made it too easy to read the strongest source→target result without seeing how many hypotheses had been searched to select the source pattern.
- Transfer reports now include the source mining status, full training `trial_registry`, total transfer result rows, and counts by result status (evaluated, insufficient data, below minimum support, etc.).
- Added regression assertions that the report preserves candidate hypothesis counts and status diagnostics.
- Commits: report diagnostics `3af5eb2b995b6c5cc3be69ca78622810a2dbd5cd`; test `27fb8f72af15abc386a154e26ea998dcc3556d25`.
- Validation: run `45490941559` on the previous head `afccf203` had passed compile, safe-default checks, deterministic tests and read-only BingX smoke scan; its 180-day backtest was cancelled when newer commits arrived. The new diagnostics and test are not covered by that run. Latest-head CI is required.
- Next methodological step remains repeated chronological cross-symbol holdouts; until then this is a single split exploratory transfer test, not evidence of durable alpha.
- Production `main`, database, credentials, deployment and exchange orders remain untouched. PR #12 remains draft and unmerged.


## 2026-11-25 — cross-symbol hit-rate uncertainty intervals (CI pending)
- Added Wilson score 95% intervals for raw target-event and raw target-control hit rates. These intervals are more informative than point percentages alone, especially when sample sizes are small or hit rates are near 0%/100%.
- Kept the event-stratum-weighted control rate and descriptive lift separate. No naive Wilson interval is attached to the weighted rate because its effective sample-size assumptions are not binomial; this avoids presenting an invalid confidence interval as rigorous evidence.
- Added explicit empty-sample behavior and tests for zero/all-hit/small/balanced samples, plus report-level assertions.
- Commits: implementation `88e45f6fd5f57ec95001f11678230854625c52e9`; tests `eb16ce7a669df44ce4cb53652b5d39cf9be9d6bd`.
- Validation: previous commit `76400f1b4483e4345a6aade63ed2369f62c239f3` passed compile, safe-default checks, and deterministic tests. The current interval implementation and tests require a fresh CI run.
- Limitation: Wilson intervals assume independent Bernoulli observations; overlapping market episodes may violate independence. Intervals are descriptive uncertainty indicators, not formal inference or multiple-testing correction.
- Production `main`, `trading_bot_v9.db`, credentials, deployment and exchange orders remain untouched. PR #12 remains draft and unmerged.


## 2026-11-25 — repeated chronological cross-symbol transfer (CI pending)
- Added `build_rolling_cross_symbol_transfer_report` with expanding training windows and later, non-overlapping holdout windows. The step must be at least as large as the holdout fraction; configurations that would reuse holdout timestamps are rejected.
- Every fold independently mines source patterns on its training segment, fits target-symbol feature-state cutoffs from that target's training history, and evaluates only different target symbols. A minimum 30-minute purge separates train and holdout event starts.
- The final 30 minutes of the captured panel are reserved from evaluation and remain label-only, preventing the last fold from silently losing forward labels. Per-symbol label-tail coverage is reported for every fold; tail rows are never passed as holdout controls.
- Added recurrence summaries keyed by source symbol, target symbol, horizon, direction and feature/state conditions. Fold-specific numeric thresholds are deliberately not part of the key. Reports include number of evaluated folds, positive-lift folds, median fold lift and support totals.
- Trial counts are aggregated across folds with an explicit warning: expanding training windows overlap, so folds are dependent and these counts are not a multiple-testing correction. The output remains exploratory, not a strategy approval.
- Added deterministic tests for expanding training windows, non-overlapping holdouts, purge separation, recurrence aggregation, trial counts and rejection of overlapping-fold configurations.
- Validation: commit `4ee76517eb102c6d9df37d55b221d534318f8e43` passed Python compilation, runtime safe-default checks and all 126 deterministic strategy/research tests in both push and PR CI runs. Extended read-only exchange scans/backtests were skipped in these runs, so they are not claimed as validated here. No research performance or profitability claim is made.
- Follow-up hardening: rolling evaluation now discards a trailing fragment shorter than one full holdout window rather than reporting a tiny partial fold. The collector kline test now uses one fixed timestamp safely in the past, avoiding a wall-clock boundary race in its closed-candle assertion.
- Production `main`, `trading_bot_v9.db`, credentials, deployment and exchange orders remain untouched. PR #12 remains draft and unmerged.


## 2026-11-25 — conservative execution-cost backtest foundation (CI pending)
- Added a separate research-only close-to-close evaluator. A signal is considered known only after its candle closes; entry uses the next exact minute close and exit uses the exact configured horizon. Missing timestamps are rejected rather than bridged.
- Added LONG/SHORT return handling, configurable per-side fees and slippage, one open position per symbol, exact settlement-time funding cashflows, and rejection of trades when a supplied funding schedule has missing settlement records.
- Reports include gross/net expectancy, win rate, compounded return, profit factor, drawdown, holding time, funding counts and rejection reasons.
- This is deliberately not described as a realistic fill engine: no intrabar stops/targets, order-book execution, dynamic spread, latency, market impact, liquidation or capital/margin allocation. Funding correctness depends on a complete settlement schedule and exact event records.
- Added deterministic tests for direction, cost arithmetic, missing candles, overlap prevention, funding sign/coverage and invalid inputs. Added the test module to Python validation CI.
- Commits: evaluator `e527e605c9d69b57f85b9149bdc2bd8896ac2687`; tests and corrected expectations `32b21bdaa77a59ed12ec3157d61831528437ff61`; CI/roadmap update follows.
- Validation is pending on the latest branch head. No performance result has been generated because no verified historical execution dataset has been supplied to this evaluator.
- Production `main`, `trading_bot_v9.db`, credentials, deployment and exchange orders remain untouched. PR #12 remains draft and unmerged.


## 2026-11-25 — fair candidate-versus-Strategy-B comparison foundation
- Added research-only `scripts/strategy_candidate_comparison.py`, a common comparison wrapper around the conservative execution-cost backtest. Strategy B is the required baseline; named candidate signal streams are evaluated on the same candle panel, horizon, fee/slippage assumptions and exact funding inputs.
- Added low/base/stress default cost scenarios and explicit candidate-minus-baseline deltas for trade count, net expectancy, net total return and maximum drawdown. Each result also includes per-symbol and UTC-entry-date breakdowns to expose concentration and temporal instability.
- Parameter variants can be passed as separately named candidate streams, allowing later robustness sweeps without changing the evaluator. The report clearly warns that differing trade counts/exposures are not normalized, and that this is not a capital/margin model, formal inference, multiple-testing correction or independent holdout.
- Added deterministic tests for common cost application, baseline deltas, duplicate signals/scenarios, and no-valid-trade handling; registered them in Python validation CI.
- This commit adds the comparison framework only. It does not generate strategy signals, claim a performance winner, or approve any candidate. A verified historical dataset and leakage-safe signal adapters are still required before a meaningful comparison can be run.
- Production `main`, `trading_bot_v9.db`, credentials, deployment and exchange orders remain untouched. PR #12 remains draft and unmerged.


## 2026-11-25 — leakage-safe historical replay adapter for production Strategy B
- Added `scripts/strategy_b_replay.py`. It lazily loads the unchanged `build_signal_b` function from the checked-out `main.py`; it does not start the bot, initialize its database, or place orders. The replay report records the production strategy version and SHA-256 of the loaded source file so results can be traced to the exact logic revision.
- Input candles use explicit OPEN timestamps in milliseconds. At each 1m decision close, every timeframe is truncated to candles whose full interval has closed by that timestamp. Incomplete/future 5m/15m/1h/4h candles cannot enter the feature calculation. Each Strategy B `setup_key` is emitted only once.
- Replay output is already shaped for the shared candidate comparator: symbol, decision timestamp, LONG/SHORT direction, and traceable setup metadata. Historical quotes can be supplied; otherwise Strategy B's configured synthetic-spread fallback is used and is disclosed.
- Added deterministic tests for timeframe close-boundaries, setup deduplication, invalid/missing data, and loading the actual production builder without starting the bot. Registered tests in validation CI.
- This is an adapter foundation, not a completed backtest. It still requires verified historical OHLCV for all five timeframes, correct exchange candle semantics, and a comparison run on the same 1m execution candles. The replay uses the `main.py` present in the checked-out branch; compare its recorded SHA/version with production before interpreting results.
- Production `main`, database, credentials, hosting, live signals and exchange orders remain untouched. PR #12 remains draft and unmerged.


## 2026-11-25 — fail-closed execution-price and funding validation
- Corrected invalid-price accounting: non-positive close prices remain in the exact timestamp lookup so the evaluator classifies them as `invalid_price`, rather than misreporting them as missing candles.
- Funding input now fails closed for non-numeric/non-finite rates, non-integer or non-finite settlement timestamps, duplicate symbol/settlement records, and duplicate/invalid expected schedule timestamps. Previously invalid rates could be silently skipped and duplicate records could overwrite one another.
- Added deterministic tests for non-positive entry prices, invalid funding rates, duplicate funding settlements, and fractional expected settlement timestamps.
- Commits: evaluator hardening `480688fb8f1ee8b4e37ba7c542b2a981a0ce7b76`; regression tests `8ecc1e213abec22066624b5122dc6a3bbb7178a2`.
- Validation: not yet run on this head; CI must confirm the changes. No historical performance claims are made.
- Production `main`, `trading_bot_v9.db`, credentials, deployment and exchange orders remain untouched. PR #12 remains draft and unmerged.


## 2026-11-25 — ancestry and multi-day coverage gate audit
- Rechecked PR #12: it remains open, Draft, unmerged, with `mergeable=false`; its head before this audit was `0e8b11ab925fc925ad26fe80e29e51f31a5f3bcc`. No merge, rebase, retarget, production update or workflow dispatch was performed.
- The exact-head workflow lookup returned no PR-triggered run for `0e8b11a`. The previously successful run `45612012450` validated `47a4fcda`, not this documentation-only head. Do not describe `0e8b11a` as CI-validated.
- An ancestry audit found that `research/market-walkforward-regimes`, inherited by the PR #10 → #11 → #12 branch chain, differs from production `main.py` by 91 added lines. These include a Spot–Futures Telegram menu entry/handler and an import of the arbitrage scanner. Production `main` itself remains unchanged, but the research branch ancestry is therefore not a pure research-only diff relative to production. Before any future promotion of this branch chain to production, this UI/handler change must be separately reviewed and explicitly approved or removed. PR #12 is based on PR #11's branch, so merging PR #12 into that research base does not itself merge those inherited changes into production.
- Audited `scripts/multiday_coverage_gate.py` and its tests on `research/market-discovery-engine-v1`: the gate checks total distinct closed minutes and distinct UTC dates per symbol, and verifies expected symbols. It does **not** enforce a minimum number of minutes on each UTC date, contiguous coverage, maximum gaps, minute alignment, or data-quality completeness. Current passing tests cover total counts, per-symbol minimums, missing symbols, open candles and invalid thresholds only.
- Consequently, `holdout_ready=true` currently means history-count sufficiency only; it must not be interpreted as continuous/full coverage. The gate's own interpretation string already disclaims data quality and predictive validity. The earlier 4-hour capture still fails the multi-day research requirement; no threshold has been weakened.
- Next engineering step: strengthen coverage reporting with per-UTC-day minimums and explicit gap/coverage diagnostics, while preserving intentional capture-session breaks as separate segments. Add tests for sparse-day history and large within-session gaps before using the gate to authorize a new holdout run. Keep this work on a research branch and do not alter production `main.py`.
- Production `main`, `trading_bot_v9.db`, credentials, deployment and exchange orders remain untouched.


## 2026-11-25 — production diagnostics HTML safety (separate review PR)
- Audited the existing allowlisted `/diag_db` handler in production `main.py`. Dynamic SQLite diagnostic values were interpolated directly into Telegram HTML, and raw exception text was echoed to the user. A table name containing HTML-significant characters could make Telegram reject the diagnostic message; exception text could also leak implementation/database details or break parsing.
- Prepared a minimal presentation/security hardening change on isolated branch `fix/diag-db-html-safety`, Draft PR #15: https://github.com/danil0555/gold-bot/pull/15. HTML-escape every dynamic value and return a generic failure message while retaining full details in server logs. CI has not yet reported a run for this exact head.
- No SQL, DB access mode, schema, query, allowlist, strategy, risk, signal generation, credentials, deployment or order behavior is changed by this patch.
- Keep the change isolated for review; do not merge or deploy until the complete CI result and code review are satisfactory.


## 2026-11-25 — signal header and displayed risk configuration (separate review PR)
- Prepared isolated branch `fix/signal-risk-header-10pct`, Draft PR #16: https://github.com/danil0555/gold-bot/pull/16, based on production `main`.
- Changed the configured/displayed `RISK_PERCENT` from 2% to 10% and the default for newly created `user_settings` tables to 10%. Existing SQLite rows are deliberately not rewritten or migrated.
- Replaced the generic signal title with a direction-first header containing the matching indicator and symbol, e.g. `🟢 LONG — BTC-USDT` or `🔴 SHORT — BTC-USDT`.
- Important limitation found during source audit: production `main.py` currently does not calculate position quantity/notional from `RISK_PERCENT`. The value is a configuration/display default only; this PR does not implement automatic position sizing. Do not describe 10% as an enforced per-trade loss limit until a separately reviewed sizing calculation and its tests exist.
- Leverage remains 5x; TP1/TP2 remain 1.8R/3R. Real exchange orders remain disabled. No DB, credentials, deployment or live bot process was touched.
- PR #16 is Draft/open/unmerged. Its first CI run `45632193537` failed only because the workflow still asserted the old `RISK_PERCENT == 2.0`; Python compilation passed. Updated the CI assertion to 10.0 in commit `4a89cd1199b1fe5001e76fcedf8af1c6a627e892`. Replacement run `45632351705` passed: compile, runtime import/safe-default assertions, and deterministic add-on tests succeeded; the optional 7-day historical backtest was skipped. Added focused regression tests for LONG/SHORT header rendering and the 10%/5x defaults in `tests/test_signal_header.py`, and registered them in CI. The latest exact-head run `45632672926` completed successfully: compile, runtime import/safe-default checks, existing deterministic tests, and the new signal-header tests all passed. The optional 7-day historical backtest step was skipped. PR #15's diagnostics HTML-safety CI run `45630974943` completed successfully (syntax, runtime import/safe defaults, and deterministic add-on tests); the optional 7-day historical backtest step was skipped.


## 2026-11-25 — manual 10% risk-based position sizing (PR #16 extension)
- Extended Draft PR #16 on `fix/signal-risk-header-10pct`: https://github.com/danil0555/gold-bot/pull/16.
- Added pure helper `scripts/position_sizing.py` for linear USDT-M estimates: risk budget = saved trading balance × 10%; quantity = risk budget / absolute(entry − SL); notional = quantity × entry; initial margin estimate = notional / 5.
- Added per-user Telegram commands `/setbalance <USDT>` and `/balance`; each user's manually entered trading allocation is stored separately and used for their own signal calculations. No BingX balance/API data is fetched.
- Signal messages now show risk USDT, estimated base quantity, notional and initial margin; they warn when required margin exceeds the saved balance or consumes almost all of it.
- Added additive table `user_risk_profiles` only. Existing tables/rows are not altered. Existing startup logic attempts a verified SQLite backup before schema initialization when the DB exists. This migration has not been applied to any live database; require explicit user approval before merge/deployment.
- Limitations: no exchange contract metadata, contract multiplier, min-notional or quantity-step validation; quantity is not validated against the exchange lot rules. Fees, funding and slippage are excluded, so realized loss can exceed the nominal 10%. Entry-zone fills may differ from the reference entry. Risk is 10% per signal, with no portfolio-level exposure cap; simultaneous positions add risk.
- No live orders, credentials, production DB, or deployment touched. PR remains Draft/unmerged. Exact-head CI run `45647908073` passed (21 existing add-on/reliability/API tests and 11 signal/position-sizing tests). After adding an additional migration regression test, exact-head CI run `45648248721` also passed: compile, runtime safe-default checks, 21 existing tests, and 12 signal/balance/position-sizing tests. The additional test starts from an existing SQLite file and verifies that existing user/settings rows remain unchanged, the new profile table is created, foreign keys remain valid, and a pre-migration backup is made. The optional 7-day historical backtest step was skipped. No production DB was accessed; the SQLite table creation is code-only until a separately approved deployment.


## 2026-11-25 — database migration fail-closed hardening (PR #16)
- Read-only review found that `init_db()` logged a backup failure but continued schema initialization. That contradicted the stated requirement to have a verified pre-migration backup.
- Updated PR #16 so an existing database is not initialized/migrated if `backup_and_verify_db()` raises or returns `None`. Startup now raises a clear RuntimeError instead of continuing.
- Added regression test `test_init_db_refuses_migration_when_backup_returns_none`: an existing SQLite file with a legacy marker is kept unchanged and `user_risk_profiles` is not created when backup is unavailable.
- Code commit: `54ab705088f8767d4505bdc89b130d188d425314`; CI run `45649107969` passed compile, runtime safe-default, and existing tests before the new regression test was committed.
- Regression test commit `509f37a1b1079440dee40478f5053642e7dfeb71` initially failed because `pytest` was not imported in `tests/test_signal_header.py`; fixed in `ebfef09d9f7d4f6130f0908484e005ef565e3600`. Exact-head CI run `45649343335` completed successfully: compile, runtime safe-default checks, existing deterministic tests, and signal/balance/position-sizing tests all passed, including the backup-failure regression. Optional 7-day historical backtest was skipped.
- BingX contract metadata research: public contract info documents `quantityPrecision`, `tradeMinQuantity`, `tradeMinUSDT`, status and API-open state. The published response does not document an explicit quantity step field; support documentation explains that step size can be independent of minimum quantity. Do not infer the step from precision or contract size. Until a trustworthy per-symbol step source is available, mark step validation unknown and never describe a rounded quantity as order-ready.
- No live database, production branch, credentials, or orders touched.


## 2026-11-25 — BingX contract-aware position sizing (PR #16)
- Corrected the earlier assumption about a separate explicit quantity-step field: the public contract-info response exposes `quantityPrecision`, which defines decimal places. The helper rounds quantity DOWN to that precision; it does not invent a separate lot step or multiply base-asset quantity by contract `size`.
- Startup now fetches `/openApi/swap/v2/quote/contracts` through the existing read-only public market-data client. A background refresh is scheduled; cache age is limited to 6 hours. If no fresh contract data is available, signal text explicitly marks the size as unverified. Failed/unknown metadata never becomes an order-ready quantity.
- Per-symbol checks include quantity precision, `tradeMinQuantity`, `tradeMinUSDT`, active `status`, `apiStateOpen`, and the 5x configured leverage against `maxLongLeverage` / `maxShortLeverage`.
- If checks pass, quantity is rounded down and notional, estimated margin and stop-loss risk are recalculated from the rounded quantity. If any required check fails or is unknown, the theoretical quantity remains visibly unverified and the message says not to use it as a ready order.
- Public endpoint schema confirmed in BingX API reference: `/openApi/swap/v2/quote/contracts`; the same reference documents `quantityPrecision`, `tradeMinQuantity`, `tradeMinUSDT`, `status`, `apiStateOpen`, and max leverage. `/openApi/swap/v1/tradingRules` additionally documents min-size and price-protection fields, but it does not expose a distinct quantity-step property.
- Exact-head CI for commit `2fe7d05fe4a9e213117f5ef388b93dce85f975ed`, run `45650662441`, passed compile, runtime safe-default checks, 21 deterministic tests and 17 signal/balance/position-sizing tests. Optional 7-day backtest was skipped. A subsequent small retry-loop adjustment was committed as `b8d51e1c9da1f36ac74adaf5ddda02750207c8c8`; exact-head CI for that final adjustment has not yet been confirmed.
- No credentials, account balances, private account endpoints, live DB, real orders, or production `main` were touched. PR #16 remains Draft/unmerged.


## 2026-11-25 — PR #16 BingX validated-signal rendering regression fix
- A code-path audit found that `position_size_text()` read `checked["quantity_precision"]` after a successful contract validation, but `apply_bingx_contract_constraints()` did not return that field. Consequently, a valid BingX contract could trigger a KeyError while formatting the Telegram signal.
- Fixed the helper to include the validated integer precision in its result. Added a unit assertion for the returned precision and an integration regression test that renders a LONG signal with fresh, valid BTC-USDT contract metadata and checks the verified-status text.
- Commits: helper fix `f92cf8e3148662ab7852c5d1ef45ec8e831d0760`; helper test `b3c07e1726eee9aa18b7d4d01ab3749967b2b81e`; signal-rendering integration test `12a0c541e4b84d0329e3e3e6acbb905c82dc4379`.
- Exact-head CI for `12a0c541e4b84d0329e3e3e6acbb905c82dc4379` has not yet been reported. Earlier CI run `45650662441` validated only the older commit `2fe7d05fe4a9e213117f5ef388b93dce85f975ed`; it does not validate this regression fix.
- PR #16 remains Draft/open/unmerged. Production `main`, live database, credentials, deployment and exchange orders remain untouched. No real exchange order placement is enabled.


## 2026-11-25 — PR #16 exact-head regression CI and risk-rounding stress tests
- Exact-head GitHub Actions run `45675272507` completed successfully for PR #16 commit `12a0c541e4b84d0329e3e3e6acbb905c82dc4379`. All nine job steps completed successfully except the optional 7-day read-only historical backtest, which was skipped. In particular, compilation, runtime safe-default import, deterministic add-on tests and signal/balance/position-sizing tests passed.
- Added a further parameterized stress-test commit `08637f82a11f2a9f16e0fba543ca07a30ee531b3`: six balance/entry/stop-distance/precision combinations assert that exchange rounding DOWN never raises the individual trade's calculated stop-loss risk above the 10% budget. Added an explicit test documenting that three separately valid 10%-risk signals can stack to 30% aggregate nominal risk; this is a known absence of portfolio-level exposure control, not a feature claim.
- CI for the new stress-test head `08637f8` has not yet been reported. Do not treat run `45675272507` as validation of the newer commit.
- PR #16 remains Draft/open/unmerged. GitHub currently reports `mergeable=false`; no rebase, retarget, merge, or force-push was attempted. Production `main`, live database, credentials, deployment and exchange orders remain untouched.


## 2026-11-25 — PR #16 risk-rounding stress tests passed exact-head CI
- GitHub Actions run `45681145259` (run 586) completed successfully on exact latest PR #16 head `08637f82a11f2a9f16e0fba543ca07a30ee531b3`.
- Job log confirms: Python compilation passed; runtime import/safe defaults passed with real trading and add-on strategies disabled; deterministic reliability/add-on/API suite **21 passed**; signal, balance persistence and position-sizing suite **25 passed**. Optional read-only 7-day BingX historical backtest was skipped.
- The 25-test signal/sizing group includes the new six-case balance/stop-distance/precision stress parameterization and the explicit demonstration that three independent 10% positions can sum to 30% nominal aggregate risk. Rounded-down size remained at or below the per-trade 10% budget in all six cases.
- PR #16 is still Draft/open/unmerged. Latest GitHub snapshot reports `mergeable=true`; no merge, deployment, database migration or real orders were performed. Production `main` remains untouched.


## 2026-11-25 — PR #14 collector shutdown safety and CI activation
- Read-only audit found `MarketMinuteAggregator.close()` closed the SQLite connection only after `flush_all()` succeeded. A final flush exception could therefore leak the DB handle while leaving the session open.
- Hardened `close()` with `finally: self.db.close()`. If flush fails, the session's `ended_at_ms` is intentionally left NULL and no clean stop event is written; this preserves fail-closed coverage semantics while releasing the file handle.
- Added regression test `test_close_releases_database_when_final_flush_fails` to verify the handle is closed and the capture remains marked open after a simulated flush error.
- Enabled pull-request validation for the research base `research/multiday-coverage-gate-v2` and restricted unrelated spot/perpetual live smoke and 180-day backtest steps to PRs targeting production `main` only.
- The first validation run `45681463142` was superseded/cancelled during the workflow update before deterministic tests completed. Replacement run `45681566207` is in progress on the updated PR #14 head; no CI pass is claimed yet.
- PR #14 remains Draft/open/unmerged. Production `main`, production DB, credentials, deployment and exchange orders remain untouched.


## 2026-11-25 — PR #14 collector provenance validation passed
- Replacement workflow run `45681566207` completed successfully on exact PR #14 head `296bb219ed472fca56a4fc4bcaca82a2ee37f115`.
- Log confirms Python compilation and runtime safe-default import passed; the full deterministic research/strategy suite completed with **115 passed in 2.00s**. The public spot/perpetual smoke and 180-day backtest steps were skipped as intended for a PR targeting the research base.
- The tested head includes the collector `close()` finally-cleanup change and regression test for a simulated final-flush exception. A failed flush leaves `ended_at_ms` NULL and closes the SQLite handle.
- PR #14 remains Draft/open/unmerged and GitHub reports mergeable=true. No production changes, live DB access, deployment or exchange orders.


## 2026-11-25 — Portfolio risk helper groundwork (PR #16)
- Added `scripts/portfolio_risk.py`, a pure Decimal-based helper that checks open-position remaining stop-risk plus a proposed trade against an explicit portfolio risk cap.
- It counts only records explicitly marked `confirmed=True` and `status='OPEN'`; requires `remaining_risk_usdt` for every counted position; rejects negative, NaN, infinite or malformed risk inputs; and fails closed if a confirmed open record lacks risk data.
- No default portfolio cap is selected. The helper is not integrated into signal delivery because current bot code does not maintain a reliable registry of user-confirmed positions, actual entries, remaining quantities, partial exits and current stops. It is groundwork only and does not block or alter signals.
- Added `tests/test_portfolio_risk.py` and included it in the PR #16 CI command. Exact-head CI run `45682161967` for commit `7b89900a38ebf096e57b841a31589b0848a564a2` is running; compilation has passed and dependency installation was still in progress at the last check. No test pass is claimed yet.
- PR #16 remains Draft/open/unmerged. GitHub currently reports mergeable=true. Production `main`, live database, deployment, credentials and exchange orders remain untouched.


## 2026-11-25 — Portfolio risk helper CI passed (PR #16)
- Exact-head GitHub Actions run `45682161967` passed on commit `7b89900a38ebf096e57b841a31589b0848a564a2`.
- Log confirms compilation and runtime safe-default import passed; deterministic add-on/reliability/API suite: **21 passed**; signal, balance, position-sizing and new portfolio-risk suite: **42 passed**. The optional read-only 7-day backtest was skipped.
- This validates the pure helper only. It is not connected to the bot's signal delivery or a persistent open-position registry; simultaneous signals can still stack risk until confirmed-position tracking and an explicit portfolio cap policy are implemented.
- PR #16 remains Draft/open/unmerged. Production `main`, live DB, credentials, deployment and exchange orders remain untouched.


## 2026-11-25 — BingX sizing fail-closed hardening and trade lifecycle design (PR #16)
- Audit found a fail-open classification bug in `apply_bingx_contract_constraints`: any unknown side string selected the SHORT leverage limit. Changed it to reject anything except LONG/BUY/SHORT/SELL. Also reject invalid/non-positive/non-finite leverage, invalid precision (must be integer 0..18), and malformed/non-finite quantity, entry, stop distance or contract limits.
- Added regression tests for invalid side, leverage, precision and non-finite sizing values. Exact-head CI run `45683112947` passed on code commit `866e8fc298c90cdce4583af99824f80cbd92c526`? The exact full SHA is `866e8fc298c90cdce4583af99824f80cbd92c526`; compilation/runtime safe defaults passed, 21 add-on tests and 59 signal/position-sizing/portfolio-risk tests passed. Optional 7-day backtest skipped.
- Added `research/TRADE_LIFECYCLE_DESIGN.md`: proposed user-confirmed trade lifecycle, max-five active positions enforced inside a write transaction, per-user ownership checks, actual entry correction, remaining-quantity and partial-close accounting, stop updates, append-only event audit, notification deduplication, restart recovery, and additive migration gates.
- Design only: no trade table, Telegram trade handler, live database migration, production deployment, or exchange order integration was added. Current latest PR head `7a7d9d34ee792e7fe5d127c193ae7fbfc3da61ab` adds documentation only after the validated code head. PR remains Draft/unmerged; GitHub currently reports mergeable=false, so no rebase/merge was attempted.


## 2026-11-25 — capture-scoped storage v2 audit and regression fixes (PR #17)
- Audited PR #14 provenance behavior and found a substantive limitation: capture_observations linked capture IDs to market timestamps, but canonical minute tables remained keyed only by symbol/time. Concurrent/overlapping captures could overwrite canonical minute aggregates while both provenance links survived. Provenance alone did not preserve each capture's exact feature payload.
- Opened Draft research PR #17, research/capture-scoped-storage-v2, stacked on PR #14: https://github.com/danil0555/gold-bot/pull/17.
- Added capture-keyed v2 tables for minute feature aggregates, closed candle payloads, and derivatives snapshots, plus a schema-version marker. Existing canonical cross-capture tables remain unchanged for compatibility. The collector persists each session's aggregate, not raw tick-by-tick trades/depth events.
- Updated merger to preserve v2 tables and coverage gate to prefer v2 capture-scoped candles, then v1 observation links, then explicitly labeled legacy time-window estimates.
- Initial CI runs on commit d63a0ed471f5a60e75d908885224d0a5ddb09188 failed in collector tests. Log review showed stale time fixtures: a test used a future fixed timestamp, and a provenance test used a candle less than two seconds old, so the production guard correctly declined to persist it. Replaced fixtures with dynamically derived, fully closed historical candle timestamps.
- Additional merger hardening treats capture session rows and provenance rows as keyed metadata during merge, avoiding duplicate-key failures if the same capture metadata appears in more than one input.
- Latest PR #17 head is c389376dcc416459f7337f1d455c1a3c4d106f97. GitHub Actions has queued checks for this exact SHA; no pass is claimed yet. Earlier runs 45687174224 and 45687182355 failed (117/2 and 118/1 respectively) on the older head and must not be described as validation.
- Statistical guardrail: two captures observing the same market minute remain one market-time outcome, not independent samples. V2 capture isolation does not create additional independent days or prove predictive edge.
- Research only. No production main, production DB, credentials, deployment, strategy, risk, or exchange order behavior changed. Keep PR Draft and do not merge without explicit approval.


## 2026-11-25 — capture-scoped storage v2 exact-head CI passed (PR #17)
- GitHub Actions run 45687562480 completed successfully on exact PR #17 head c389376dcc416459f7337f1d455c1a3c4d106f97. Log confirms Python compilation, runtime safe-default import, and deterministic strategy/research suite: 119 passed in 2.82s. Public scan and optional historical research/backtest steps were skipped as intended for a research-base PR.
- The passing head includes dynamic historical candle fixtures, v2 capture-scoped rows, merger compatibility for legacy captures, and keyed deduplication for capture session/provenance metadata.
- A duplicate workflow run 45687552952 was still in progress at the time of this entry; run 45687562480 alone is sufficient exact-head passing evidence.
- PR #17 remains Draft/open/unmerged; GitHub reports mergeable=true. No rebase or merge performed.
- Validation covers deterministic code paths only. It does not yet validate a live BingX capture, a merged real-data DB, or predictive profitability. Next: conduct a small read-only capture using the v2 collector, inspect row counts and per-capture isolation in the resulting DB, then run the multi-day coverage gate before any long collection.
- Production main, production DB, credentials, deployment, strategy/risk settings, and exchange orders remain untouched.


## 2026-11-25 — mixed legacy/v2 merge-order regression added (PR #17)
- Added a regression test with a legacy capture as the first merge input and a v2 capture as the second. This specifically validates that the merger creates additive v2 destination tables from a legacy first input and then copies the second capture's version marker and per-capture minute aggregate without dropping it.
- Test commit: 5fbc57d99c420411916dba27b76ce3d2d805e8da. Exact-head CI is queued/in progress; prior 119-pass result applies to the preceding head only, not this new test commit.
- PR #17 remains Draft/open/unmerged. No production changes or data collection have been started by this test-only addition.


## 2026-11-25 — mixed-version merge fixture corrected (PR #17)
- The added legacy-first merge test exposed a fixture mismatch: its synthetic legacy schema used a reduced minute_features table, while a real older collector DB has the full canonical table schema. The merger correctly rejected the incompatible input; this was a test-fixture problem, not evidence that real collector versions differ.
- Reworked the test to create a v1-shaped DB using MarketMinuteAggregator's real schema, then drop only the four v2 tables/marker before merging it ahead of a v2 capture. This preserves realistic core-table column parity while exercising legacy-first ordering.
- Fix commit: 7311436283ec53e92e2d7745cc5d49f420d2f9bc. New exact-head CI is required; the earlier 119-pass run validated the prior head only. The intermediate added test correctly failed (119 passed, 1 failed) because of the fixture schema mismatch.
- No production code, DB, credentials, deployment, or exchange order behavior changed.


## 2026-11-25 — PR #17 final exact-head deterministic CI
- Latest PR #17 head `7311436283ec53e92e2d7745cc5d49f420d2f9bc` has two successful GitHub Actions runs: `45687982118` and `45688002614`.
- Both logs confirm compilation, runtime import/safe-default checks, and **120 deterministic tests passed** (3.47s and 3.34s). No failures.
- Both runs intentionally skipped public market scans, historical backtests and the 3-minute live microstructure smoke collection because this PR targets a research branch, not production `main`. Therefore this is code/fixture validation, not live-data validation.
- PR #17 remains Draft/open/unmerged. GitHub reports mergeable=true. No rebase or merge performed.
- Next validation should use a separate manually dispatched research-only workflow or approved local read-only smoke run, creating a new disposable SQLite DB. Verify capture-scoped feature/candle/derivatives counts, `PRAGMA integrity_check`, foreign-key check, merge row counts, and coverage-gate attribution before any multi-day collection.
- Production main, production DB, credentials, deployment, strategy/risk settings, and exchange order behavior remain untouched.
