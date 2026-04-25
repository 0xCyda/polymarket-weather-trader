# Polymarket Weather Trader

Trade Polymarket weather markets using an **AIFS ENS (ECMWF AI ensemble) + 4-model global blend**. Dynamic signal confidence based on model agreement, with Simmer API handling market discovery and execution.

Inspired by gopfan2's $2M+ weather trading strategy.

## Architecture

- **AIFS ENS** — ECMWF AI ensemble system (51 member forecast)
- **4-model global blend** — AIFS ENS (18%), ECMWF IFS (24%), GFS (14%), Météo-France ARPEGE (10%). Trimmed from the original 8; UK Met Office, ICON, GEM, and JMA were dropped because they inflated spread and blocked valid signals.
- **Signal confidence** — dynamically adjusted based on model agreement
- **Simmer API** — market discovery and execution
- **Three strategies**:
  - **Core** — edge-based entry (min_edge 0.25) on well-matched buckets, $200 per trade, dynamic profit-take exits. Runs every 4h.
  - **Punt** — hunts tail mispricings (buckets priced ≤15¢ where model says ≥70%), fixed $15 stake, $100/day budget cap. Runs alongside Core. CORE refuses entries ≤15¢ — that's PUNT territory.
  - **Late** — day-of intraday entry at 3pm local per city. Uses TWC observed running daily max rather than forecasts; per-city ceilings derived from backtest hit rates. Runs hourly.

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Set your API key
export SIMMER_API_KEY=your_key_here

# Dry run (paper mode — default)
python weather_trader.py

# Live trading (requires WALLET_PRIVATE_KEY)
python weather_trader.py --live

# Check positions
python weather_trader.py --positions

# Dashboard
python dashboard.py
```

## Key Files

- `scripts/weather_trader.py` — main entry point, core + punt strategies
- `scripts/late_trader.py` — late-mode hourly intraday trader
- `scripts/dashboard.py` — local HTML dashboard for paper journal
- `scripts/aifs_forecast.py` — AIFS ENS fetch and parsing
- `scripts/ensemble_forecast.py` — 4-model global ensemble
- `scripts/forecast_validator.py` — cross-model validation
- `scripts/paper_journal.py` — paper trade logging (JSONL)
- `scripts/forecast_history.py` — forecast accuracy tracking
- `scripts/backfill_forecast_actuals.py` — populate actual_temp from Open-Meteo archive
- `scripts/analytics.py` — per-model accuracy / calibration / city reports
- `scripts/config.json` — runtime config (paper_balance, entry_threshold, etc.)

## Config highlights

- 35 cities (US + international), both °F and °C bucket types supported
- Edge-based entry gate (price × confidence driven)
- Dynamic exit: entry_price × 4.0 profit target
- 5.8° spread cap, 10 max trades per core cycle
- Punt mode on by default, bounded by $100 daily cap

See [SKILL.md](SKILL.md) for full configuration reference.
