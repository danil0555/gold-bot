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
