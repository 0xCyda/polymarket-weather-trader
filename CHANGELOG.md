# Changelog

All notable changes to the Polymarket Weather Trader. Newest first.

## 2026-05-09

### Added
- CORE positions now take 75% profit at 1.9x entry, then keep the remaining 25% alive as a runner with a peak-based trailing stop instead of dumping the whole position in one shot. (`position_manager.py`, `tests/test_position_manager.py`)
- Paper-journal rows now persist partial take-profit history plus `realized_pnl`, so runner exits and later settlement logic can account for already-booked gains without making the ledger lie. (`paper_journal.py`, `position_manager.py`, `tests/test_paper_journal.py`, `tests/test_position_manager.py`)
- Added fresh audit artifacts for the exit-review work, including the 2x audit, 1.8x-2.5x band sweep, top-swing recheck, and cleaned 1.9x timing analysis. (`reports/audits/*`)

### Changed
- Dashboard portfolio stats now include realized gains from still-open runner positions, so balance and realized P&L stop understating partial exits. (`dashboard.py`, `tests/test_dashboard_marks.py`)
- Position-manager operations were tightened from the old 30-minute cadence to an adaptive 10-minute schedule so take-profit and runner-stop checks stop arriving half an hour late to the crime scene. (runtime cron schedule)
- Weather-station bias calibration was rerun across 618 resolved-date forecasts, refreshing `LOCATION_BIAS_C` and fixing the audit script to read current live bias settings instead of a stale hardcoded map. (`weather_trader.py`, `bias_audit.py`)

## 2026-05-07

### Added
- Added a config-gated CORE carve-out for strong exact buckets in the 30-39¢ band when edge is below the normal floor but still clears a small minimum edge. The trade reasoning and signal payload now tag these entries for later audit. (`weather_trader.py`, `tests/test_exact_core_rules.py`)

### Changed
- CORE skip logging now distinguishes near-miss exact-bucket carve-out candidates from generic low-edge skips, so the 30-39¢ exact pocket can be audited without mixing it into the rest of the sludge. (`weather_trader.py`)
- The dashboard now gives carve-out trades their own `CARVEOUT` tag next to `CORE`, and the paper journal persists that flag on new entries instead of making us guess later. (`dashboard.py`, `paper_journal.py`, `weather_trader.py`)

## 2026-05-05

### Changed
- Pre-peak breakout exits now default to a 1.5°C overshoot instead of 0.5°C, so PM stops killing same-day winners on shallow 1-degree breakouts that whole-degree markets can still recover from. (`position_manager.py`, `weather_trader.py`)
- Projected-outside-bucket exits now require the projected EOD temperature to sit a full 1.0°C outside the held bucket before force-closing, instead of firing on tiny 0.5°C misses. Exit reasons now log the active projected buffer for easier audit reads. (`position_manager.py`, `weather_trader.py`)

### Added
- Added `position_projected_exit_buffer_c` / `SIMMER_WEATHER_POSITION_PROJECTED_EXIT_BUFFER_C` so projected-exit strictness is configurable instead of hard-coded into PM logic. (`position_manager.py`, `weather_trader.py`)

## 2026-05-01

### Changed
- Dashboard summary cards were trimmed so the old Total P&L tile and its redundant realized-P&L subtext are gone, leaving the balance card to carry the headline account state. (`dashboard.py`)
- Post-peak position exits now require a full configurable degree outside the held bucket before force-closing, which stops noisy boundary misses from getting killed too early. (`position_manager.py`, `weather_trader.py`)

### Fixed
- The dashboard's “Today P&L” / “Resolved Today” stats now convert `resolved_at` into AWST before bucketing trades by day, so overnight UTC resolutions stop landing on the wrong date. (`dashboard.py`)
- AIFS GRIB cache retention now prunes old run-keyed files after refresh/prewarm and keeps only the current run, the previous run, and the `latest_*` aliases instead of silently hoarding old downloads. (`aifs_forecast.py`)

## 2026-04-30

### Changed
- Interactive AIFS reads are now cache-only whenever any readable cached run exists, so scan/dashboard lookups stop triggering surprise live downloads and just surface the freshest local run with a stale marker when needed. (`aifs_forecast.py`, `tests/test_aifs_stale_fallback.py`)

### Fixed
- AIFS latest-cache handling now deduplicates the run-keyed GRIBs behind the `latest_cf.grib2` / `latest_pf.grib2` aliases instead of copying duplicate blobs around disk. (`aifs_forecast.py`)
- AIFS index fetch retries are now capped, with regression coverage, so bad remote responses stop spiraling into retry storms. (`aifs_forecast.py`, `tests/test_aifs_stale_fallback.py`)

## 2026-04-29

### Fixed
- Early PM TP/SL exits no longer populate `actual_temp` before the market is truly resolved. Dashboard rows now stay blank on same-day exits, and the backfill logic only restores actuals after the target date is past in the city’s local timezone. (`position_manager.py`, `paper_journal.py`, `dashboard.py`)
- Existing early-exit journal rows were cleaned up so stale same-day actuals do not leak into the dashboard API. (`data/paper_trades.jsonl`)
- D+0 CORE skip logging no longer pollutes the skip journal with fake `no_bucket_parseable` / `no_bucket_low_edge` reasons caused by intentionally suppressing CORE entries on day-of markets. `d0_core_skip` rows now carry best-bucket audit metadata instead. (`weather_trader.py`)
- Position manager now has a same-day corpse-price guard plus an earlier easy-city CORE repricing window, so dead same-day easy setups get cut sooner instead of waiting for the generic 2 PM / 5¢ logic. (`position_manager.py`, `weather_trader.py`, `tests/test_position_manager.py`)
- PUNT now blocks sub-tick/extreme-price entries and refuses duplicate same-market journaling, which closes the hole that let the bogus Dallas 0.0005 paper punt show up as fake exposure. (`weather_trader.py`, `paper_journal.py`, `tests/test_paper_journal.py`, `tests/test_punt_rules.py`, `data/paper_trades.jsonl`)

### Changed
- Repo-local operator docs were pruned and rewritten to match the current system. `SKILL.md` and `LESSONS.md` now reflect the live stack, and stale `CLAUDE.md` guidance was removed. (`SKILL.md`, `LESSONS.md`, `CLAUDE.md`)

## 2026-04-28

### Added
- Position manager gained repricing-guard logic plus adaptive check cadence, giving PM a proper late-day repricing response instead of just static threshold checks. (`position_manager.py`, `weather_trader.py`)
- Dashboard now shows staged scan progress instead of feeling frozen during long scans. (`dashboard.py`)
- Resolved-trade actual temperatures are backfilled on resolved rows so historical rows can show the real observed temp once settlement data exists. (`position_manager.py`, `weather_trader.py`, `data/paper_trades.jsonl`)

### Fixed
- Late trader entry window was tightened to a real 15-minute local-time window. (`late_trader.py`, `weather_trader.py`)
- Dashboard resolved-history UX was cleaned up: resolved rows sort by resolution date, reset to the latest page, keep the resolved column at the end again, and fix punt display plus scan city count. (`dashboard.py`)
- Sub-floor late adds are blocked, the bad Seoul late trade was closed out, and the invalid Seoul late trade was removed from the journal/loss logs. (`position_manager.py`, `data/paper_trades.jsonl`, `data/manager_actions.jsonl`, `data/losses.log`)
- PM auto exits now book against the prior check price, the repricing guard panic floor was removed, and late trades that should have been wins were corrected in the journal. (`position_manager.py`, `data/paper_trades.jsonl`)

## 2026-04-27

### Added
- Position manager learned projected-outside-bucket exits, while the dashboard gained clearer resolved-trade metadata: PM TP/SL badges, entered timestamps, midpoint marks, tighter labels, and 10-row pagination. (`position_manager.py`, `dashboard.py`)
- LATE sizing moved to edge bands and dropped the old daily budget approach. (`late_trader.py`)

### Fixed
- Bucket settlement resolution was corrected, the Warsaw counterfactual exit was fixed, and three late trades were force-closed as wins to clean up the journal. (`paper_journal.py`, `data/paper_trades.jsonl`)
- Scan-complete notifications were tightened to actual trades only. (`weather_trader.py`, `dashboard.py`)
- A year-collision bug in the journal event index loader was removed. (`paper_journal.py`)

## 2026-04-26

### Added
- Introduced `position_manager.py` for same-day exit/add management, plus the first counterfactual audit tooling for early exits. (`position_manager.py`, `audit_early_exits.py`)
- Added a WebSocket market listener for real-time entry triggering, plus dashboard upgrades for actionable candidates, LATE/LATE+ badges, full signal batches, and unified action controls. (`dashboard.py`, realtime listener tooling)
- Late trader expanded from a small whitelist to all 35 cities and gained a configurable `LATE_PRICE_FLOOR`. (`late_trader.py`)
- Added skip-win and bias audit scripts so skip and city-bias tuning could be tested against history. (`analyze_skip_backfill.py`, bias audit tooling)

### Changed
- City-tier sizing cap was removed, Milan was resized to the full 3% easy-city allocation, and location bias was recalibrated from a 214-event audit. (`weather_trader.py`, journal state)
- Dashboard and bot paths were standardized around the project repo, and `dashboard.py` / `weather_trader.py` fully moved under `scripts/`. (`dashboard.py`, `weather_trader.py`, docs)

### Fixed
- Position manager fixed a race condition, a silent add path, stale-bucket handling, and merged adds into parent trades so wins stop double-counting. (`position_manager.py`)
- Manual resolve/backfill now populate `actual_temp`, and dashboard live prices switched to direct CLOB pricing instead of stale Simmer-only marks. (`paper_journal.py`, `dashboard.py`)
- Same-day runtime state, journal rows, and late-trader paper mode priming were cleaned up so PM and LATE could run without ghost failures. (`paper_journal.py`, `late_trader.py`, runtime data)

## 2026-04-25

### Added
- Added `analyze_skip_backfill.py` for strong-signal skipped-trade reconstruction using city-tier stake sizing. (`analyze_skip_backfill.py`)

### Changed
- CORE now fully skips D+0, leaving day-of handling to LATE, and the CORE/PUNT boundary was cleaned up so CORE ignores `<=15¢` while PUNT owns the `0–14.9¢` band. (`weather_trader.py`)

### Fixed
- `get_positions()` now merges journal truth with Simmer prices instead of trusting Simmer for paper position state. (`format_scan.py` / journal integration)
- Added hard de-dupe on `log_paper_trade()` to prevent duplicate `market_id` entries. (`paper_journal.py`)
- Added aggressive garbage collection during scans to stop cfgrib/xarray memory buildup from getting the process SIGKILLed. (`weather_trader.py`)
- Folded a broad audit pass covering data integrity, live-safety guardrails, and ops cleanup into the repo. (`weather_trader.py`, `paper_journal.py`, misc runtime files)

## 2026-04-24

### Changed
- PUNT gates were loosened and config handling was cleaned up after an orphaned `config.json` / production-defaults mess. (`weather_trader.py`, config files)
- Wellington and Seoul were demoted to `medium` tier after the expanded trader-sample review. (`weather_trader.py`)
- Weak signals were hidden from the dashboard and scan output so the UI focuses on actionable candidates. (`dashboard.py`, scan output)

### Fixed
- Diagnostic output now reports the real funnel reason and nearest bucket in native units instead of the misleading old “No bucket found” message. (`weather_trader.py`)
- City lists were unified to 35, market-fetch caps were raised, METAR override consistency was fixed, and dashboard env/path issues were cleaned up. (`weather_trader.py`, `dashboard.py`)
- Punt safety tightened: stale and unresolved punt entries are blocked, the invalid Atlanta punt was removed, and the scan formatter regained honest uPnL output. (`weather_trader.py`, journal/runtime data)
- Removed the orphaned `format_scan.py` copy that had drifted from reality. (`scripts/format_scan.py`)

## 2026-04-23

### Added
- METAR observation pull now populates `metar_temp` for **all forecasts**, not just D+0.
  Station observation is recorded as context on every scan. Signal-strength
  adjustments (afternoon lower-bound, divergence downgrade) still only apply
  on D+0 high markets. (`ensemble_forecast.py`)
- Silent `no bucket match` rejections now hit `skip_events.jsonl` with a
  classified reason — `no_bucket_parseable`, `no_bucket_price_extreme`, or
  `no_bucket_low_edge` — and capture the best-ranked bucket's price,
  confidence, edge, and `market_id`. Makes the funnel debuggable for signals
  that silently fell through (Wellington 2026-04-25 was the catalyst). (`weather_trader.py:2137`)
- `docs/DEB_PLAN.md` — design doc for Dynamic Error Balancing (per-city
  model accuracy tracking to replace static ensemble weights).
- This changelog.

### Changed
- **Dashboard · Core Trading config panel** now reflects the tiered sizing
  strategy. Replaced the stale `Max position: $200` / `Sizing %: 5%` rows
  with `Sizing: tiered (city difficulty)` plus computed dollar values
  (`Easy (3%) $300` / `Medium (2%) $200` / `Hard (1%) $100`). Values are
  derived live from `paper_balance`. (`dashboard.py:1036`)
- **Dashboard · Header layout**. Live pill and `Last updated` now sit inline
  in a single flex row. `Overview` and `Config` tabs kept together; `Scan Now`
  moved to its own cell at the far right. Scan Now restyled as a rounded
  green-filled pill with white text. Grid widened to 4 columns; mobile
  breakpoints reshuffled so the status line spans full width. (`dashboard.py:143`)
- **Dashboard · Signal spread rendering** for non-US rows. `fmtCelsiusDelta`
  now rounds the raw spread value without converting °F→°C — so a spread
  of `1.7` renders as `2°C` instead of `1°C`. Matches how the data is
  intuitively read. US rows unchanged. (`dashboard.py:645`)
- LATE mode landed: day-of entries now use observed conditions with per-city ceilings, Trading Modes tiles in the dashboard, and refreshed whitelist/ceiling logic from a DST-corrected backtest. (`late_trader.py`, `dashboard.py`)
- Tier-only sizing replaced the old `max_position_usd` cap, entry/slippage ceilings were raised, and GRIB download handling got socket-timeout plus concurrency fixes so scans stop tripping over themselves. (`weather_trader.py`, GRIB/cache plumbing)
- Root-level duplicate bot files were removed, docs were corrected back to the live 4-model stack, and `errors.log` tracking was formalized. (`dashboard.py`, `weather_trader.py`, docs, git tracking)

## 2026-04-22

### Added
- Added trader-analysis tooling, top-trader batch reports, and historical backtest/pull scripts to study profitable weather-market behavior and city difficulty. (`polymarket_analyze.py`, `analyze_top_traders.py`, `pull_weather_markets.py`, `backtest.py`)
- Dashboard gained resolved-trade pagination and open-position city-difficulty badges. (`dashboard.py`)
- Added LFS tracking and committed the larger local analysis/runtime artifacts needed by the project. (`.gitattributes`, data/report files)

### Changed
- Edge-based bucket selection and empirical city-tier position sizing replaced the older simpler bucket-pick logic. (`weather_trader.py`)
- `dashboard.py` and `weather_trader.py` were moved under `scripts/`, and repo docs were updated to point at the canonical project location. (`scripts/`, `SKILL.md`, `LESSONS.md`)

### Fixed
- Same-day resolution tracking and fake uPnL on resolved positions were corrected. (`dashboard.py`, trade resolution logic)
- Dashboard mobile/layout bugs and blank report capture issues were cleaned up. (`dashboard.py`, reporting scripts)

## 2026-04-21

### Added
- Expanded the system to 35 cities with full METAR station coverage, added Buenos Aires, started tracking `skip_events.jsonl`, `losses.log`, and structured `errors.log`, and introduced analytics for model accuracy, calibration, city stats, and skip-funnel review. (`weather_trader.py`, `analytics.py`, data logs)
- Per-model ensemble temperatures are now stored on paper trades for later analysis. (`paper_journal.py`)

### Changed
- The ensemble stack was churned heavily: BOM ACCESS was removed, MeteoFrance/UKMO were briefly added, weights were flattened, and docs/dashboard were synced to the evolving model mix. (`ensemble_forecast.py`, docs, dashboard)
- METAR afternoon logic was upgraded so D+0 highs can use observation floors and confidence boosts. (`ensemble_forecast.py`)

### Fixed
- Fixed bucket parsing to always trust question text for weather markets, which removed false losses like Lucknow and corrected bucket-label handling throughout the stack. (`weather_trader.py`, tests, journal cleanup)
- Signal invalidation was added, tightened to D+0, then disabled again after false closures from bad bucket data. Net result: invalidation no longer silently wrecks open trades. (`weather_trader.py`)
- Atomic journal writes, negative-temp handling, deduping, NO-side P&L, show-more toggles, and win-rate denominator bugs were cleaned up in a broad audit pass. (`paper_journal.py`, `dashboard.py`, tests)
- cfgrib `.idx` handling was fixed by deleting stale index files before open. (`weather data / GRIB handling`)

## 2026-04-20

### Added
- Added paper-trade journaling, tracked `paper_trades.jsonl` in repo, and made the dashboard/journal flows depend on that local source of truth. (`paper_journal.py`, `data/paper_trades.jsonl`)
- Added PUNT mode for tail-priced bucket mispricings and enabled it by default. (`weather_trader.py`)
- Dashboard got a major visual overhaul with glass-morphism styling, Polymarket links, and more robust auto-resolution plumbing. (`dashboard.py`)

### Changed
- Discovery-cache handling, forecast logging, and market-id capture were tightened so scans persist the right context and don’t silently miss strong signals. (`weather_trader.py`, `forecast_history.jsonl`)
- Hard `MAX_SPREAD=5.8` guardrail was reintroduced. (`weather_trader.py`)

### Fixed
- Replaced blocked `clob.polymarket.com` resolution calls with Simmer resolution APIs and guarded `outcome=None` cases. (`weather_trader.py`)
- Fixed bucket-match failures when Simmer returned `outcome_name='Yes'`, removed bogus manual entries like the fake SF low-temp trade, and backfilled missed strong signals from the Apr 21 cron. (`weather_trader.py`, journal/runtime data)
- AIFS cache validation and dashboard rough edges were cleaned up enough to lock in the first decent run of punts and core trades. (`weather data cache`, `dashboard.py`)
