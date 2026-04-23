# Changelog

All notable changes to the Polymarket Weather Trader. Newest first.

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
