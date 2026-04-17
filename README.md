# Polymarket Weather Trader

Trade Polymarket weather markets using an **AIFS ENS (ECMWF AI ensemble) + 7-model global blend**. Dynamic signal confidence based on model agreement, with Simmer API handling market discovery and execution.

Inspired by gopfan2's $2M+ weather trading strategy.

## Architecture

- **AIFS ENS** — ECMWF AI ensemble system (51 member forecast)
- **7-model global blend** — AIFS ENS, ECMWF IFS, GFS, ICON, GEM, JMA, BOM ACCESS
- **Signal confidence** — dynamically adjusted based on model agreement
- **Simmer API** — market discovery and execution

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Set your API key
export SIMMER_API_KEY=your_key_here

# Run
python weather_trader.py
```

## Key Files

- `weather_trader.py` — main entry point
- `scripts/aifs_forecast.py` — AIFS ENS fetch and parsing
- `scripts/ensemble_forecast.py` — 7-model global ensemble
- `scripts/forecast_validator.py` — cross-model validation
- `config.json` — market locations and signal thresholds
