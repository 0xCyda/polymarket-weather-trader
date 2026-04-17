# Polymarket Weather Trader

Trade Polymarket weather markets using an **AIFS ENS (ECMWF AI ensemble) + 6-model global blend**. Dynamic signal confidence based on model agreement, with Simmer API handling market discovery and execution.

Inspired by gopfan2's $2M+ weather trading strategy.

## Architecture

- **AIFS ENS** — ECMWF AI ensemble system (51 member forecast)
- **6-model global blend** — GFS, ECMWF, ICON, GEM, ARPEGE, HRES
- **Signal confidence** — dynamically adjusted based on model agreement
- **Simmer API** — market discovery and execution

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Configure
cp config.json config.env  # add your API keys

# Run
python weather_trader.py
```

## Key Files

- `weather_trader.py` — main entry point
- `scripts/aifs_forecast.py` — AIFS ENS fetch and parsing
- `scripts/ensemble_forecast.py` — 6-model global ensemble
- `scripts/forecast_validator.py` — cross-model validation
- `config.json` — market locations and signal thresholds
