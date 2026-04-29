---
name: polymarket-weather-trader
description: Trade Polymarket weather markets with the current AIFS ENS + 4-model ensemble stack. Covers CORE scans, PUNT tails, LATE entries, the position manager, dashboard, and skip audits.
metadata:
  author: Jarvis (SoleBrace)
  version: "1.18.0"
  displayName: Polymarket Weather Trader
  difficulty: intermediate
---

# Polymarket Weather Trader

Current system docs for the live repo, not the old workspace copy.

## Canonical repo

- Repo: `/home/brandon/projects/polymarket-weather-trader`
- Run from this directory
- Ignore old compatibility copies under `~/.openclaw/workspace/skills/...`

## What the system is now

The stack has five moving parts:

1. **CORE**: ensemble-based weather scan and entries
2. **PUNT**: tiny tail-priced mispricing buys
3. **LATE**: narrow post-3pm-local intraday entries
4. **PM / position manager**: same-day hold, exit, corpse-price, and repricing logic
5. **Dashboard**: local FastAPI UI on port `8414`

## Forecast stack

Current ensemble is:

- `ecmwf_ifs025`
- `aifs_ens`
- `gfs_seamless`
- `meteofrance_seamless`

Dropped models like UKMO, ICON, GEM, and JMA are not part of the trading blend anymore.

Signal classification in the live code:

- `strong`: at least 4 models, agreement at least 70%, max delta at most 6°
- `moderate`: at least 3 models, max delta at most 10°
- `weak`: wider disagreement or thin model coverage

Hard trade filter in `scripts/weather_trader.py`:

- `MAX_SPREAD = 5.8°`

## Required runtime assumptions

- Use **`python3.12`** for manual runs
- Source the project `.env` first for anything that needs Simmer auth
- Paper journal is the source of truth for paper-mode positions and P&L

Manual pattern:

```bash
cd /home/brandon/projects/polymarket-weather-trader
set -a && source .env && set +a
```

## Core commands

### Weather scan

```bash
cd /home/brandon/projects/polymarket-weather-trader
set -a && source .env && set +a
python3.12 scripts/weather_trader.py
```

Notes:
- default is paper mode
- `--live` enables real execution
- `--positions` prints positions only
- `--config` prints resolved config
- `--quiet` suppresses noise unless trades/errors happen

### Force a fresh scan

Bypass discovery and forecast reuse when you want a truly fresh pass:

```bash
cd /home/brandon/projects/polymarket-weather-trader
set -a && source .env && set +a
SIMMER_WEATHER_DISCOVERY_CACHE_MIN=0 \
SIMMER_WEATHER_FORECAST_CACHE_DISK=false \
python3.12 scripts/weather_trader.py
```

### Position manager / PM check

Dry run:

```bash
cd /home/brandon/projects/polymarket-weather-trader
set -a && source .env && set +a
python3.12 scripts/position_manager.py
```

Execute journal changes:

```bash
python3.12 scripts/position_manager.py --execute
```

### LATE mode

```bash
cd /home/brandon/projects/polymarket-weather-trader
set -a && source .env && set +a
python3.12 scripts/late_trader.py
```

Force a city or bypass the local-time gate:

```bash
python3.12 scripts/late_trader.py --city London --force
python3.12 scripts/late_trader.py --live --city Seoul --force
```

### Dashboard

Launch:

```bash
setsid /usr/bin/python3.12 /home/brandon/projects/polymarket-weather-trader/scripts/dashboard.py >/tmp/polymarket-dashboard.log 2>&1 < /dev/null &
```

Check:

```bash
ss -tlnp | grep 8414
```

URL:

- `http://localhost:8414`
- over Tailscale: `http://100.70.22.118:8414`

### Status and journal helpers

```bash
python3.12 scripts/status.py
python3.12 scripts/status.py --positions
python3.12 scripts/paper_journal.py --backfill
```

### Skip audit

Built-in audit script:

```bash
python3.12 scripts/skip_win_analysis.py
```

For custom slices, use an ad hoc Python filter against:

- `data/forecast_history.jsonl`
- `scripts/data/skip_events.jsonl`
- `data/paper_trades.jsonl`

## High-value config knobs

These matter most in practice:

| Setting | Env var | Default |
|---|---|---:|
| Min edge | `SIMMER_WEATHER_MIN_EDGE` | `0.25` |
| Entry ceiling | `SIMMER_WEATHER_ENTRY_THRESHOLD` | `0.50` |
| Exit threshold | `SIMMER_WEATHER_EXIT_THRESHOLD` | `0.45` |
| Exit profit multiplier | `SIMMER_WEATHER_EXIT_PROFIT_MULT` | `4.0` |
| Max position | `SIMMER_WEATHER_MAX_POSITION_USD` | `200` |
| Max trades/run | `SIMMER_WEATHER_MAX_TRADES_PER_RUN` | `10` |
| Discovery cache minutes | `SIMMER_WEATHER_DISCOVERY_CACHE_MIN` | `60` |
| Forecast cache disk | `SIMMER_WEATHER_FORECAST_CACHE_DISK` | `true` |
| Punt enabled | `SIMMER_WEATHER_PUNT_MODE` | `true` |
| Punt size | `SIMMER_WEATHER_PUNT_POSITION_USD` | `15` |
| Punt ceiling | `SIMMER_WEATHER_PUNT_PRICE_CEILING` | `0.149` |
| Punt daily budget | `SIMMER_WEATHER_PUNT_DAILY_BUDGET` | `100` |
| Late price floor | `SIMMER_WEATHER_LATE_PRICE_FLOOR` | code-driven |
| PM corpse floor | `SIMMER_WEATHER_POSITION_CORPSE_PRICE_FLOOR` | `0.05` |
| PM corpse entry frac | `SIMMER_WEATHER_POSITION_CORPSE_ENTRY_FRAC` | `0.35` |

For the full set, inspect `CONFIG_SCHEMA` in `scripts/weather_trader.py`.

## Current operating rules

- **Do not put live keys in docs.** Use `.env`.
- **Do not trust stale dashboard prices** for near-zero illiquid buckets without a sanity check.
- **Do not treat Simmer as the source of truth for paper positions.** The journal wins.
- **Do not run from the old workspace copy.** Use the project repo.
- **Do not assume the dashboard stayed alive because it bound once.** Verify port `8414`.

## Common realities

### Simmer key missing

If you see `SIMMER_API_KEY environment variable not set`, you forgot to source `.env` before the run.

### Manual run looks thin right after a cron

Discovery cache probably ate the scan. Force it with:

```bash
SIMMER_WEATHER_DISCOVERY_CACHE_MIN=0 SIMMER_WEATHER_FORECAST_CACHE_DISK=false python3.12 scripts/weather_trader.py
```

### Weather spikes look insane

Usually thin books, bucket coupling, and a forecast/observation repricing burst. Do not read those wicks as clean consensus.

### Dashboard says weird price or zero

Could be one of three things:

- stale or wrong Simmer `current_price` on illiquid buckets
- Gamma integer market ID needing Gamma → CLOB lookup
- no live mark available, which should be treated as missing data, not zero

### cfgrib `.idx` noise

If you see stale idx chatter, the GRIB usually still runs, but stale indices can poison opens. The code already handles this better now, so don't panic unless forecasts actually fail.

## Files that matter

- `scripts/weather_trader.py`: CORE + PUNT entry engine
- `scripts/late_trader.py`: LATE strategy
- `scripts/position_manager.py`: PM logic
- `scripts/dashboard.py`: dashboard and API
- `scripts/paper_journal.py`: paper journal source of truth
- `scripts/ensemble_forecast.py`: signal generation
- `data/paper_trades.jsonl`: authoritative paper trades
- `data/forecast_history.jsonl`: forecast log
- `scripts/data/skip_events.jsonl`: skip reason log
- `data/errors.log`: structured persistent errors

## Fast troubleshooting

### Scan fails immediately

Check:

```bash
python3.12 -V
cd /home/brandon/projects/polymarket-weather-trader
test -f .env && echo env_ok
```

### Dashboard died

Restart it cleanly:

```bash
pkill -f dashboard.py || true
setsid /usr/bin/python3.12 /home/brandon/projects/polymarket-weather-trader/scripts/dashboard.py >/tmp/polymarket-dashboard.log 2>&1 < /dev/null &
ss -tlnp | grep 8414
```

### Need to inspect open positions

```bash
grep '"status": "open"' data/paper_trades.jsonl
```

### Need a real PM dry run now

```bash
python3.12 scripts/position_manager.py
```

## Bottom line

This system is no longer just a simple weather scan. It is a paper-trading stack with separate entry, late-entry, position-management, dashboard, and audit paths. When debugging it, verify which layer is actually wrong before touching code.