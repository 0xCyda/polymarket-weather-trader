---
name: polymarket-weather-trader
description: Trade Polymarket weather markets using an AIFS ENS (ECMWF AI ensemble) + 6-model global blend. Uses signal strength and model agreement to dynamically adjust confidence. Simmer API handles market discovery and execution. Inspired by gopfan2's $2M+ strategy.
metadata:
  author: Jarvis (SoleBrace)
  version: "2.0.0"
  displayName: Polymarket Weather Trader (AIFS ENS)
  difficulty: intermediate
  attribution: Strategy inspired by gopfan2; ensemble architecture by Jarvis
---
# Polymarket Weather Trader

Trade temperature markets on Polymarket using an **AIFS ENS + 6-model global ensemble** with dynamic signal confidence.

> Remix it with different forecast models, additional locations, or new market types (precipitation, wind, etc.). The skill handles all the plumbing — market discovery, ensemble fetching, bucket matching, trade execution, and safeguards.

## Signal Architecture

The core change in v2.0: instead of single-source NOAA, the trader uses a **weighted multi-model ensemble**:

| Model | Weight | Source |
|-------|--------|--------|
| aifs_ens | 25% | ECMWF AI ensemble mean (GRIB download, AWS S3 fallback) |
| ecmwf_ifs025 | 35% | Open-Meteo |
| gfs_seamless | 20% | Open-Meteo |
| icon_global | 15% | Open-Meteo |
| gem_global | 10% | Open-Meteo |
| jma_seamless | 10% | Open-Meteo |
| bom_access_global | 10% | Open-Meteo |

`weighted_temp` = weighted average across all models that return data. The ensemble also computes `max_delta` (worst disagreement in degrees) and `agreement_pct` (% of models within 3° of the weighted average).

For same-day (D+0) markets, live **METAR station observations** are fetched as a ground-truth anchor. If METAR diverges >5° from the ensemble, the signal is downgraded.

## When to Use This Skill

- User wants to trade weather markets automatically
- Set up gopfan2-style temperature trading
- Check weather trading positions or P&L
- Configure thresholds, locations, or signal parameters

## What's New in v2.0.0

- **AIFS ENS ensemble**: ECMWF AI-generated ensemble mean via GRIB download (primary) with AWS S3 fallback. Handles the forecast backbone.
- **6-model global blend**: Weighted combination of major global forecast models via Open-Meteo. All models run concurrently via ThreadPoolExecutor.
- **Signal strength confidence**: Dynamic confidence (0.70–0.88) based on ensemble agreement, not a hardcoded NOAA probability.
  - `strong` (spread ≤5°): confidence = 0.88
  - `moderate` (spread 5–10°): confidence = 0.80
  - `weak` (spread >10°): confidence = 0.70 — **trade skipped**
  - `single_source` / unknown: confidence = 0.72
- **Cache per (location, date)**: Each market's ensemble result cached separately to prevent stale data across different dates for the same city.
- **METAR ground truth**: Same-day markets get live airport station observations as an extra signal quality check.

## Setup Flow

1. **Install the Simmer SDK**
   ```bash
   pip install simmer-sdk
   ```

2. **Set environment variables**
   - `SIMMER_API_KEY` — from simmer.markets/dashboard → SDK tab
   - `WALLET_PRIVATE_KEY` — required for live trading (signs orders client-side automatically)

3. **Confirm locations** (default: NYC only; configurable)

4. **Set up cron** (disabled by default — user must enable)

## Configuration

| Setting | Environment Variable | Default | Description |
|---------|---------------------|---------|-------------|
| Entry threshold | `SIMMER_WEATHER_ENTRY_THRESHOLD` | 0.15 | Buy when price below this |
| Exit threshold | `SIMMER_WEATHER_EXIT_THRESHOLD` | 0.45 | Sell when price above this |
| Max position | `SIMMER_WEATHER_MAX_POSITION_USD` | 100.00 | Maximum USD per trade |
| Max trades/run | `SIMMER_WEATHER_MAX_TRADES_PER_RUN` | 5 | Maximum trades per scan cycle |
| Locations | `SIMMER_WEATHER_LOCATIONS` | NYC | Comma-separated cities |
| Binary only | `SIMMER_WEATHER_BINARY_ONLY` | false | Skip range-bucket events, only trade binary yes/no |
| Smart sizing | `SIMMER_WEATHER_SIZING_PCT` | 0.05 | % of balance per trade (--smart-sizing) |
| Slippage max | `SIMMER_WEATHER_SLIPPAGE_MAX` | 0.15 | Skip trades with slippage above this |
| Min liquidity | `SIMMER_WEATHER_MIN_LIQUIDITY` | 0 | Skip markets with liquidity below this USD |
| Vol targeting | `SIMMER_WEATHER_VOL_TARGETING` | false | Enable EWMA volatility targeting |
| Target vol | `SIMMER_WEATHER_TARGET_VOL` | 0.20 | Target annualized volatility |
| Vol max leverage | `SIMMER_WEATHER_VOL_MAX_LEVERAGE` | 2.0 | Max scale-up multiplier |
| Vol min alloc | `SIMMER_WEATHER_VOL_MIN_ALLOC` | 0.2 | Min allocation floor |
| Vol EWMA span | `SIMMER_WEATHER_VOL_SPAN` | 10 | EWMA responsiveness |

**US locations** (via AIFS ENS + Open-Meteo GFS): NYC, Chicago, Seattle, Atlanta, Dallas, Miami

**International locations** (via Open-Meteo): Tel Aviv, Munich, London, Tokyo, Seoul, Ankara, Lucknow, Wellington, Shanghai, Hong Kong, Toronto, Paris, Milan, Sao Paulo, Warsaw, Singapore

## Quick Commands

```bash
# Dry run (default — shows opportunities, no trades)
python weather_trader.py --dry-run

# Execute real trades
python weather_trader.py --live

# With smart position sizing
python weather_trader.py --live --smart-sizing

# Check positions only
python weather_trader.py --positions

# View config
python weather_trader.py --config

# Disable safeguards (not recommended)
python weather_trader.py --no-safeguards

# Disable trend detection
python weather_trader.py --no-trends

# Enable volatility targeting
python weather_trader.py --live --smart-sizing --vol-targeting

# Quiet mode — only output on trades/errors
python weather_trader.py --live --quiet
```

## How It Works

Each cycle:

1. **Market discovery** — Fetches active weather markets from Simmer API, groups by event
2. **Ensemble fetch** — For each (location, date, metric):
   - Downloads AIFS ENS GRIB from ECMWF open data API (AWS S3 fallback on failure)
   - Fetches 6 global models concurrently via Open-Meteo
   - Computes weighted average (`weighted_temp`), spread (`max_delta`), agreement %
   - For D+0 markets: fetches live METAR as ground-truth anchor
3. **Signal strength** — Classifies ensemble agreement:
   - `strong`: spread ≤5°, confidence 0.88 — trade eligible
   - `moderate`: spread 5–10°, confidence 0.80 — trade eligible
   - `weak`: spread >10°, confidence 0.70 — **skipped**
   - `no_data`: models couldn't forecast date (too far ahead or missing data)
4. **Bucket matching** — Finds the Polymarket bucket matching `weighted_temp`. Bucket values in Celsius are automatically converted to Fahrenheit before comparison (ensemble always returns °F). Exact buckets (e.g. "will it be 18°C on Apr 18?") require the forecast to fall exactly within that degree; range buckets (e.g. "53°F or below") are more tradeable. A `— nearest: [bucket]` suffix shows the closest non-matching bucket when the forecast is near a threshold.
5. **Safeguards** — Checks flip-flop warnings, slippage, time decay, market status
6. **Trend detection** — Looks for recent price drops (stronger buy signal)
7. **Entry** — If price < threshold and safeguards pass → BUY
8. **Exit** — If open position and price > exit threshold → SELL
9. **Signal metadata** — Logged with trade: `weighted_temp`, `signal_strength`, `models_count`, `agreement_pct`, `max_delta`

## Example Output

```
🌤️  Simmer Weather Trading Skill
==================================================
  [PAPER MODE] Trades will be simulated with real prices. Use --live for real trades.

⚙️  Configuration:
  Entry threshold: 15% (buy below this)
  Exit threshold:  45% (sell above this)
  Max position:    $100.00
  Max trades/run:  5
  Locations:       NYC

📡 Fetching weather markets...
  Found 100 weather markets
  Grouped into 12 events

📍 NYC 2026-04-18 (high temp)
  Fetching ensemble forecast (AIFS ENS + 6-model blend)...
  AIFS ENS: 63.5°F | signal: weak | 6 models | agree: 66.7% | spread: 10.2°
  ⚠️  No bucket found for 63.5°F

📊 Summary:
  Events scanned: 12
  Entry opportunities: 0
  Exit opportunities:  0
  Trades executed:     0
```

## Signal Strength Classification

| Signal | Condition | Confidence | Action |
|--------|-----------|------------|--------|
| `strong` | spread ≤5°, agreement ≥80% | 0.88 | Trade eligible |
| `moderate` | spread 5–10° | 0.80 | Trade eligible |
| `weak` | spread >10° or agreement <67% | 0.70 | **Skipped** |
| `single_source` | only 1 model returned data | 0.72 | Trade eligible (degraded) |
| `no_data` | no models returned data | — | **Skipped** |

## Safeguards

Before trading, the skill checks:
- **Flip-flop warning**: Skips if direction reversals are excessive on this market
- **Slippage**: Skips if estimated slippage > 15%
- **Time decay**: Skips if market resolves in < 2 hours
- **Market status**: Skips if market already resolved
- **Signal strength**: Skips if `weak` or `no_data`
- **Bucket match**: Skips if no Polymarket bucket matches the forecast temperature

## Troubleshooting

**"No ensemble forecast for [date] (signal: no_data)"**
- Date is too far ahead for models to forecast (typically >10 days). This is expected for far-future markets.
- Also check: is the GRIB cache stale? Delete `/home/brandon/.cache/aifs_ens/` if idx/grib mismatch errors appear.

**"Safeguard blocked: Severe flip-flop warning"**
- Too many direction reversals on this market. Wait before trading again.

**"Slippage too high"**
- Market is illiquid. Reduce position size or skip.

**"Resolves in Xh — too soon"**
- Elevated risk on imminent resolution. Skip.

**"signal: weak — skipped"**
- Ensemble spread >10° or agreement <67%. Signal is not confident enough. This is intentional — weak-signal trades are skipped to avoid noise.

**"No bucket found for XX.X°F"**
- The forecast temperature doesn't fall within any of the Polymarket buckets for that market. This is normal — it means the forecast doesn't align with the market's defined ranges.
- If a Celsius market (e.g. Tokyo, Shanghai): the bucket value is converted from °C to °F before comparison. The ensemble always returns Fahrenheit; `parse_temperature_bucket` detects the market's unit and converts accordingly.
- The output shows `— nearest: [bucket]` when the forecast is close to a bucket threshold — useful for seeing near-miss signals on tight Celsius markets.

**"External wallet requires a pre-signed order"**
- `WALLET_PRIVATE_KEY` is not set. Fix: `export WALLET_PRIVATE_KEY=0x<your-polymarket-wallet-private-key>`. The SDK signs orders automatically.

**"Balance shows $0 but I have USDC on Polygon"**
- Polymarket uses **USDC.e** (bridged USDC, contract `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`). Bridge native USDC to USDC.e if needed.

**"API key invalid"**
- Get new key from simmer.markets/dashboard → SDK tab

## File Structure

```
polymarket-weather-trader/
├── weather_trader.py          # Main entry point
└── scripts/
    ├── aifs_forecast.py        # AIFS ENS GRIB download + parse (ECM open data API + AWS S3)
    ├── ensemble_forecast.py   # Multi-model ensemble (weighted blend + METAR)
    ├── forecast_validator.py  # Forecast consistency checks
    └── status.py              # Balance and position checks
```

## Key Functions

```python
# In scripts/ensemble_forecast.py
get_ensemble_forecast(city, date_str, metric, unit) -> dict
# Returns:
#   weighted_temp   # weighted average temperature
#   model_temps      # {model_name: temp}
#   models_count     # how many models returned data
#   max_delta        # worst disagreement (degrees)
#   agreement_pct    # % of models within 3° of weighted avg
#   signal_strength  # "strong"|"moderate"|"weak"|"single_source"|"no_data"
#   metar_temp       # live station obs (D+0 markets only)
#   metar_delta      # abs(ensemble - METAR)
```
