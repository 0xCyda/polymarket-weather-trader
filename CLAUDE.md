# CLAUDE.md — Polymarket Weather Trader

Session context for Claude Code agents working on this repo.

## Architecture Overview

Paper-trading bot that trades Polymarket weather markets using an AIFS ENS + 4-model global ensemble. Three strategies run on cron:

- **CORE** — D+1/D+2/D+3+ forecast-based entries. Edge = Gaussian bucket probability × signal discount − market price. Price floor 15¢ (below that is PUNT territory). Skips D+0 markets entirely — LATE handles those.
- **PUNT** — tail-mispricing hunter for buckets priced 0–14.9¢. Requires ≥70% model confidence and ≥50% edge. Fixed $15 stake, $100/day budget cap.
- **LATE** — day-of intraday entries at 3pm local using TWC observed running daily max (not forecasts). Per-city price ceilings derived from DST-corrected backtest. Whitelisted cities only.

## Key Files

| File | Purpose |
|------|---------|
| `scripts/weather_trader.py` | Main orchestrator — CORE + PUNT strategies, config schema, market parsing |
| `scripts/late_trader.py` | LATE mode — hourly cron, TWC observations, per-city ceilings |
| `scripts/ensemble_forecast.py` | 4-model ensemble (ECMWF IFS 24%, AIFS 18%, GFS 14%, MeteoFrance 10%) + METAR |
| `scripts/aifs_forecast.py` | AIFS ENS GRIB download from ECMWF open data (AWS S3), cfgrib decode |
| `scripts/paper_journal.py` | Paper trade JSONL journal with atomic writes + file locking |
| `scripts/forecast_history.py` | Forecast accuracy tracking (log_forecast, update_resolutions) |
| `scripts/analytics.py` | Per-model accuracy, calibration, city/time stratification, skip funnel |
| `scripts/format_scan.py` | Discord/cron scan output formatter |
| `scripts/dashboard.py` | FastAPI glass-morphism dashboard (port 8414) |
| `scripts/polymarket_analyze.py` | Reverse-engineer any Polymarket trader's weather strategy |
| `scripts/analyze_top_traders.py` | Run polymarket_analyze on 4 verified pro wallets |
| `scripts/pull_weather_markets.py` | Pull all resolved weather events from Gamma API |
| `scripts/backtest.py` | Replay bot logic against historical Polymarket events |
| `scripts/backfill_forecast_actuals.py` | Backfill actual_temp on forecast_history from Open-Meteo archive |
| `scripts/late_entry_backtest.py` | Late-entry strategy backtest with DST-aware timezone handling |
| `scripts/config.json` | Runtime config (canonical — root config.json was removed) |

## Ensemble Models

4 models via Open-Meteo + AIFS via GRIB. Weights renormalize when models fail.

```
ecmwf_ifs025           24%   ECMWF deterministic
aifs_ens               18%   ECMWF AI ensemble (GRIB download)
gfs_seamless           14%   NOAA GFS
meteofrance_seamless   10%   Météo-France ARPEGE
```

Dropped models: BOM ACCESS (data feed suspended mid-2025), UKMO/ICON/GEM/JMA (outlier models inflating spread, blocking valid signals).

METAR observations fetched for all 35 cities. D+0 afternoon (≥15:00 local): METAR overrides weighted_temp as a floor if observation > ensemble. Also upgrades moderate→strong when METAR agrees within 3°.

## 35 Cities

US: NYC, Chicago, Seattle, Atlanta, Dallas, Miami, Houston, San Francisco, Phoenix, Los Angeles, Denver, Austin, Las Vegas

International: Tel Aviv, Munich, London, Tokyo, Seoul, Ankara, Lucknow, Wellington, Toronto, Paris, Milan, Sao Paulo, Warsaw, Singapore, Shanghai, Beijing, Shenzhen, Chengdu, Chongqing, Wuhan, Hong Kong, Buenos Aires

## City Difficulty Tiers (from 9k+ pro trades)

Position sizing scales by tier: EASY=3%, MEDIUM=2%, HARD=1% of paper balance, capped by MAX_POSITION_USD.

- **EASY** (≥75% pro win rate): Tel Aviv, Warsaw, San Francisco, Los Angeles, Milan, Chengdu, Houston, Munich, Seoul
- **MEDIUM** (55-75%): NYC, Chicago, Buenos Aires, Dallas, London, Atlanta, Singapore, Toronto, Paris, Ankara, Miami, Lucknow, Sao Paulo, Austin, Denver, Seattle, Chongqing, Shenzhen
- **HARD** (≤55%): Tokyo, Shanghai, Wellington, Beijing, Wuhan

## Price Zones

```
0 – 14.9¢    PUNT only (≥70% confidence, ≥50% edge)
15¢ – 55¢    CORE only (≥25% edge)
55¢+         No entry (price ceiling)
```

CORE also skips D+0 markets entirely (LATE handles those).

## Known Bugs & Gotchas

### Simmer API quirks
- `outcome_name` field is unreliable — can return "Yes", "17°C" when actual bucket is "43°C". Always parse from `question` field first (handled by `parse_market_bucket`).
- CLOB API returns 403 from most environments. Use Simmer SDK abstraction.
- Simmer often leaves `outcome=None` on resolved markets. Paper journal infers from settlement price (<0.05 = NO, >0.95 = YES).

### Forecast data
- All forecasts fetched in °F internally (`unit='F'`). International markets display in °C but bucket matching converts °C→°F before comparison.
- `forecast_temp` in paper_trades.jsonl is stored in °F regardless of market unit.
- METAR floor override can make `weighted_temp` disagree with `model_temps` — the `metar_override` key in model_temps dict signals this happened.
- Transient model fetch glitches can return garbage (e.g. 19°C for Singapore). The forecast disk cache has a 1h D+0 / 3h D+1+ TTL and self-heals on next fetch.

### Signal invalidation
- Disabled (`check_signal_invalidation` commented out at call site). Was closing positions incorrectly due to bucket-label mismatches from the outcome_name bug. Can be re-enabled once bucket data is trustworthy.

### Unit display
- US cities display °F, international display °C on dashboard and scan output.
- The "no bucket found" diagnostic converts back to native units for display (fixed — was showing °F numbers with °C label).

## Data Files

| File | Format | Purpose |
|------|--------|---------|
| `data/paper_trades.jsonl` | JSONL (git-lfs) | All paper trades — open + resolved |
| `data/forecast_history.jsonl` | JSONL (git-lfs) | Every ensemble forecast logged |
| `data/skip_events.jsonl` | JSONL | Every rejected trade candidate with reason |
| `data/forecast_cache.json` | JSON | Disk-persisted forecast cache (TTL-based) |
| `data/losses.log` | JSONL | Full signal context for every losing trade |
| `data/errors.log` | JSONL | Runtime API errors |
| `data/polymarket_events.jsonl` | JSONL | Resolved Polymarket weather events (from Gamma API) |
| `scripts/data/skip_events.jsonl` | JSONL | Skip events from scans running in scripts/ cwd |
| `reports/` | Text | Saved outputs from analysis scripts |

## Analytics Commands

```bash
python scripts/analytics.py --all              # model accuracy, calibration, city stats, skip funnel
python scripts/analytics.py --model-report     # per-model MAE/bias (needs actual_temp backfill first)
python scripts/backfill_forecast_actuals.py    # populate actual_temp from Open-Meteo archive
python scripts/analyze_top_traders.py          # run polymarket_analyze on 4 verified pro wallets
python scripts/pull_weather_markets.py         # pull all resolved weather events from Gamma API
```

## Pro Trader Intelligence

Verified weather specialist wallets (from polymarket_analyze.py):

| Handle | Wallet | Weather P&L | Key insight |
|--------|--------|-------------|-------------|
| Hans323 | `0x0f37cb80dee49d55b5f6d9e595d52591d6371410` | +$80k | 56% win rate but losers 4.3× winners. Range buckets = +$773k, threshold = -$725k |
| ColdMath | `0x594edb9112f526fa6a80b8f858a6379c8a2c1c11` | -$173k | 66% win rate but net negative (losers 2.1× winners). 6271 trades across 25+ cities |

Key findings from pro data:
- **Range buckets are most profitable** (Hans323: 72% of volume, +$773k P&L)
- **Threshold buckets lose money** (Hans323: -$725k on "or above/below")
- **66% win rate can still lose money** if losers are larger than winners (ColdMath)
- **Low-temp markets are dead** — <0.4% of pro volume

## Concurrency & File Safety

- `paper_trades.jsonl` uses `os.replace()` atomic write + `fcntl.flock` advisory lock (POSIX) / `msvcrt.locking` (Windows) to prevent CORE + LATE clobber.
- Late budget tracking (`late_daily_budget.json`) also uses file locking via `_spend_budget()`.
- Paper journal has hard dedup: refuses to log if market_id already has an open position.

## Testing

```bash
python -m unittest discover -s tests    # 28 tests, no network required
```

Tests cover: bucket parsing, temperature conversion, market bucket extraction, negative temps, multi-word cities.

## Git Workflow

- Develop on `claude/general-session-TQhrp` branch
- Push to master and main when ready
- `git push -u origin <branch>` with retry on network failure (up to 4 retries, exponential backoff)
- Never force-push to main/master without user permission
- `data/*.jsonl` files are in git-lfs — actual content not readable from sandboxed environments

## Common Operations

```bash
# Dry run scan (paper mode)
python scripts/weather_trader.py --dry-run

# Full scan with all cities
SIMMER_WEATHER_LOCATIONS="NYC,Chicago,..." python scripts/weather_trader.py --dry-run

# Late mode dry run
python scripts/late_trader.py --force

# Dashboard
python scripts/dashboard.py  # http://localhost:8414

# Paper journal summary
python scripts/paper_journal.py

# Resolve open trades (backfill from Simmer + Open-Meteo archive)
python scripts/paper_journal.py --backfill
```
