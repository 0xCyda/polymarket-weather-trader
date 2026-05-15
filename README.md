# Polymarket Weather Trader

Trade Polymarket weather markets using an **AIFS ENS (ECMWF AI ensemble) + 4-model global blend**. Dynamic signal confidence is based on model agreement, with Simmer API handling market discovery and execution.

Inspired by gopfan2's $2M+ weather trading strategy.

## Architecture

- **AIFS ENS** — ECMWF AI ensemble system (51 member forecast)
- **4-model global blend** — AIFS ENS (18%), ECMWF IFS (24%), GFS (14%), Météo-France ARPEGE (10%). Trimmed from the original 8; UK Met Office, ICON, GEM, and JMA were dropped because they inflated spread and blocked valid signals.
- **Signal confidence** — dynamically adjusted based on model agreement
- **Simmer API** — market discovery and execution
- **Three entry strategies**:
  - **Core** — edge-based entry on well-matched buckets, $200 per trade, runs every 4h. Core refuses entries below the punt ceiling because cheap tail buckets use stricter punt rules.
  - **Core exact carve-out** — exact-bucket entries in the 35-40¢ band can pass with lower edge when the bucket is otherwise well matched. The wider 34-40¢ band is intentionally not active.
  - **Punt** — hunts tail mispricings priced <=14.9¢ where the model says >=70%, fixed $15 stake, $100/day budget cap. Runs alongside Core.
  - **Late** — day-of intraday entry around 3pm city-local time. Uses TWC observed running daily max rather than stale forecasts; per-city ceilings come from backtest hit rates. Runs through 15-minute windowed crons, not a simple hourly loop.
- **Position manager** — adaptive 10-minute cron for same-day open positions. Top-of-hour checks always run; extra 10-minute checks process only once positions are in the peak/late-day local window. Current posture is exit/hold only: scale-in adds are disabled by default.

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Source runtime env
set -a && source .env && set +a

# Dry run (paper mode - default)
python3.12 scripts/weather_trader.py

# Live trading (requires WALLET_PRIVATE_KEY)
python3.12 scripts/weather_trader.py --live

# Check positions
python3.12 scripts/weather_trader.py --positions

# Dashboard
python3.12 scripts/dashboard.py

# Position manager dry run
python3.12 scripts/position_manager.py

# Position manager execute mode
python3.12 scripts/position_manager.py --execute --scheduled
```

## Key Files

- `scripts/weather_trader.py` — main entry point, core + punt strategies
- `scripts/late_trader.py` — late-mode intraday trader for city-local 3pm windows
- `scripts/position_manager.py` — adaptive same-day exits, price-only take profit, hard stops, and optional disabled scale-ins
- `scripts/dashboard.py` — local HTML dashboard for paper journal
- `scripts/format_scan.py` — Discord scan formatter with portfolio/open-position enrichment
- `scripts/aifs_forecast.py` — AIFS ENS fetch and parsing
- `scripts/ensemble_forecast.py` — 4-model global ensemble
- `scripts/forecast_validator.py` — cross-model validation
- `scripts/paper_journal.py` — paper trade logging (JSONL)
- `scripts/forecast_history.py` — forecast accuracy tracking
- `scripts/backfill_forecast_actuals.py` — populate actual_temp from Open-Meteo archive
- `scripts/analytics.py` — per-model accuracy / calibration / city reports
- `scripts/config.json` — runtime config (paper_balance, entry_threshold, stop rules, etc.)

## Config Highlights

- 35 cities (US + international), both °F and °C bucket types supported
- Edge-based entry gate (price × confidence driven)
- Core exact carve-out active at 35-40¢ with min edge 0.05
- 5.8° spread cap, 10 max trades per core cycle
- Punt mode on by default, bounded by $100 daily cap
- Position manager takes 75% profit at 1.9x entry and keeps the runner with breakeven/trailing protection
- Universal hard stop at -65% from entry
- Exact-core market-collapse guard: exit around 12¢ after a 65% collapse from entry
- Position-manager scale-in adds disabled by default

## Active Automation

- `Polymarket Weather Scan` — every 4h, posts portfolio, signals, new entries, and scan stats to Discord.
- `Polymarket LATE Scan` — 15-minute windowed cron slots for city-local late-entry windows. Silent unless an error occurs; entries are surfaced by the next Weather Scan.
- `Polymarket Position Manager` — every 10 minutes with adaptive gating. Silent unless an exit fires or an error occurs.
- `Polymarket Early-Exit Audit` — daily counterfactual audit for whether early exits cut winners or saved losses.
- `Polymarket weather daily X post` — daily public post generator. Public copy must avoid internal bot terms like CARVE, carveout, scale-in, route names, and trigger names.

See [SKILL.md](SKILL.md) for full configuration reference.
