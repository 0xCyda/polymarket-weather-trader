# Changelog

All notable changes to the Polymarket Weather Trader. Newest first.

## 2026-04-29

### Fixed
- Early PM TP/SL exits no longer populate `actual_temp` before the market is truly resolved. Dashboard rows now stay blank on same-day exits, and the backfill logic only restores actuals after the target date is past in the city’s local timezone. (`position_manager.py`, `paper_journal.py`, `dashboard.py`)
- Existing early-exit journal rows were cleaned up so stale same-day actuals do not leak into the dashboard API. (`data/paper_trades.jsonl`)
- D+0 CORE skip logging no longer pollutes the skip journal with fake `no_bucket_parseable` / `no_bucket_low_edge` reasons caused by intentionally suppressing CORE entries on day-of markets. `d0_core_skip` rows now carry best-bucket audit metadata instead. (`weather_trader.py`)
- Position manager now has a same-day corpse-price guard so dead core positions can be dumped once the market has already priced them as toast, without nuking tiny punts. (`position_manager.py`, `weather_trader.py`, `tests/test_position_manager.py`)

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
