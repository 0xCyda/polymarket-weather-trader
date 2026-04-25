---
name: polymarket-weather-trader
description: Trade Polymarket weather markets using an AIFS ENS (ECMWF AI ensemble) + 4-model global blend (ECMWF IFS, GFS, ARPEGE). Uses signal strength and model agreement to dynamically adjust confidence. Simmer API handles market discovery and execution. Inspired by gopfan2's $2M+ strategy.
metadata:
  author: Jarvis (SoleBrace)
  version: "1.17.2"
  displayName: Polymarket Weather Trader (AIFS ENS)
  difficulty: intermediate
  attribution: Strategy inspired by gopfan2; ensemble architecture by Jarvis
---
# Polymarket Weather Trader

Trade temperature markets on Polymarket using an **AIFS ENS + 4-model global ensemble** with dynamic signal confidence.

> Remix it with different forecast models, additional locations, or new market types (precipitation, wind, etc.). The skill handles all the plumbing — market discovery, ensemble fetching, bucket matching, trade execution, and safeguards.

## Signal Architecture

The core change in v2.0: instead of single-source NOAA, the trader uses a **weighted multi-model ensemble**. The original 8-model set was trimmed to the 4 best-performing sources after outlier models (UKMO, ICON, GEM, JMA) were found to inflate spread and block valid signals.

| Model | Weight | Source |
|-------|--------|--------|
| ecmwf_ifs025 | 24% | Open-Meteo (ECMWF IFS 0.25°) |
| aifs_ens | 18% | ECMWF AI ensemble mean (GRIB download via AWS S3) |
| gfs_seamless | 14% | Open-Meteo (NOAA GFS) |
| meteofrance_seamless | 10% | Open-Meteo (Météo-France ARPEGE) |

Weights intentionally do not sum to 1.0 (total = 0.66); they renormalize across whichever models return data for a given scan. The ensemble also computes `max_delta` (worst disagreement in degrees) and `agreement_pct` (% of models within 3° of the weighted average).

For same-day (D+0) markets after 14:00 local, live **METAR station observations** are fetched as a ground-truth anchor. If METAR diverges >5° from the ensemble, the signal is downgraded. Downgrade is skipped in the morning since current temp is naturally well below the daily high.

## When to Use This Skill

- User wants to trade weather markets automatically
- Set up gopfan2-style temperature trading
- Check weather trading positions or P&L
- Configure thresholds, locations, or signal parameters

## What's New

- **AIFS ENS ensemble**: ECMWF AI-generated ensemble with CF (control) + PF (perturbed, 5 members) GRIB download. True ensemble spread and agreement computed from all members.
- **4-model global blend**: Weighted combination of ECMWF IFS, GFS, and Météo-France ARPEGE via Open-Meteo, plus AIFS ENS via GRIB. All models run concurrently via ThreadPoolExecutor.
- **Signal strength confidence**: Dynamic confidence based on ensemble agreement:
  - `strong` (≥4 models, agreement ≥70%, max_delta ≤6°): confidence = 0.88
  - `moderate` (≥3 models, max_delta ≤10°): confidence = 0.80
  - `weak` (otherwise): confidence = 0.68 — allowed through if edge ≥ MIN_EDGE (0.25)
  - `single_source` / unknown: confidence = 0.72
- **MAX_SPREAD cap**: hard cap at 5.8°F — any candidate with spread above this is skipped regardless of edge or signal
- **market_id in forecast_history**: `log_forecast()` called after bucket matching — `market_id` captured in all new scan entries
- **Cache per (location, date, metric)**: Each market's ensemble result cached separately to prevent stale data across different dates or high/low temp events for the same city.
- **METAR ground truth**: Same-day markets get live airport station observations as an extra signal quality check.

## Bot Repo Path (CRITICAL)
- **Repo path:** `/home/brandon/projects/polymarket-weather-trader`
- **Git dir:** `/home/brandon/projects/polymarket-weather-trader.git`
- **Latest commit:** `4822936` — "Dashboard UI overhaul — glass-morphism"
- **WRONG (ignore):** `~/.openclaw/workspace/skills/polymarket-weather-trader` — do NOT use this path

## Simmer API Key
- Key: `sk_live_XREDACTED`
- Location: `\\wsl.localhost\Ubuntu\home\brandon\.hermes\workspace\SOLEBRACE\API`
- Confirmed working by Brandon

## Setup Flow

1. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Set environment variables**
   - `SIMMER_API_KEY` — use key above (from simmer.markets/dashboard → SDK tab)
   - `WALLET_PRIVATE_KEY` — required for live trading (signs orders client-side automatically)

3. **Confirm locations** (default: NYC only; configurable)

4. **Set up cron** (disabled by default — user must enable)

## Cron Notes
- Cron JSON must escape newlines as `\\n` in string values, not bare `\n`
- `payload.message` and `prompt` fields break if newlines aren't properly escaped
- Always use correct repo path in cron prompt

## Configuration

### Core strategy

| Setting | Environment Variable | Default | Description |
|---------|---------------------|---------|-------------|
| Min edge | `SIMMER_WEATHER_MIN_EDGE` | 0.25 | Primary entry gate: `confidence - price` must be ≥ this |
| Entry ceiling | `SIMMER_WEATHER_ENTRY_THRESHOLD` | 0.50 | Price sanity cap (secondary filter) |
| Exit threshold | `SIMMER_WEATHER_EXIT_THRESHOLD` | 0.45 | Fixed exit floor |
| Exit profit multiplier | `SIMMER_WEATHER_EXIT_PROFIT_MULT` | 4.0 | Dynamic exit: entry_price × this (e.g. 15¢ entry → 60¢ target) |
| Max position | `SIMMER_WEATHER_MAX_POSITION_USD` | 200.00 | Maximum USD per trade |
| Max trades/run | `SIMMER_WEATHER_MAX_TRADES_PER_RUN` | 10 | Maximum trades per scan cycle |
| Locations | `SIMMER_WEATHER_LOCATIONS` | NYC | Comma-separated cities (see below for supported list) |
| Binary only | `SIMMER_WEATHER_BINARY_ONLY` | false | Skip range-bucket events, only trade binary yes/no |
| Smart sizing | `SIMMER_WEATHER_SIZING_PCT` | 0.05 | % of balance per trade (--smart-sizing) |
| Paper balance | `SIMMER_WEATHER_PAPER_BALANCE` | 10000.0 | Starting balance for paper trading |
| Slippage max | `SIMMER_WEATHER_SLIPPAGE_MAX` | 0.15 | Skip trades with slippage above this |
| Min liquidity | `SIMMER_WEATHER_MIN_LIQUIDITY` | 0 | Skip markets with liquidity below this USD |
| Order type | `SIMMER_WEATHER_ORDER_TYPE` | GTC | Order type (GTC or FAK) |
| Vol targeting | `SIMMER_WEATHER_VOL_TARGETING` | false | Enable EWMA volatility targeting |
| Target vol | `SIMMER_WEATHER_TARGET_VOL` | 0.20 | Target annualized volatility |
| Vol max leverage | `SIMMER_WEATHER_VOL_MAX_LEVERAGE` | 2.0 | Max scale-up multiplier |
| Vol min alloc | `SIMMER_WEATHER_VOL_MIN_ALLOC` | 0.2 | Min allocation floor |
| Vol EWMA span | `SIMMER_WEATHER_VOL_SPAN` | 10 | EWMA responsiveness |
| Discovery cache | `SIMMER_WEATHER_DISCOVERY_CACHE_MIN` | 60 | Per-location discovery cache TTL (minutes) |
| Forecast cache (disk) | `SIMMER_WEATHER_FORECAST_CACHE_DISK` | true | Persist forecast cache across runs (1h D+0, 3h D+1+) |

There is also a fixed **MAX_SPREAD = 5.8°F** cap hard-coded in `weather_trader.py` — any candidate with ensemble spread above this is skipped regardless of edge or signal strength.

### Punt mode (side strategy — hunts tail-priced mispricings)

On by default. Finds deeply-mispriced buckets (e.g. "London 59°F or below" priced at 0.1¢ when models say 95%+ likely). Fixed small stake, own daily budget, tagged separately in the paper journal.

| Setting | Environment Variable | Default | Description |
|---------|---------------------|---------|-------------|
| Punt mode | `SIMMER_WEATHER_PUNT_MODE` | true | Enable punt side strategy |
| Punt size | `SIMMER_WEATHER_PUNT_POSITION_USD` | 15.00 | Fixed USD per punt trade |
| Punt price ceiling | `SIMMER_WEATHER_PUNT_PRICE_CEILING` | 0.149 | Max price for a punt candidate (14.9¢). One tick below CORE's price floor — PUNT handles ≤14.9¢, CORE handles ≥15¢, no overlap. |
| Punt min edge | `SIMMER_WEATHER_PUNT_MIN_EDGE` | 0.50 | Min edge for a punt |
| Punt min confidence | `SIMMER_WEATHER_PUNT_MIN_CONFIDENCE` | 0.70 | Min model probability for a punt |
| Punt daily budget | `SIMMER_WEATHER_PUNT_DAILY_BUDGET` | 100.00 | Max USD spent on punts per day |

**US locations** (AIFS ENS + Open-Meteo + METAR): NYC, Chicago, Seattle, Atlanta, Dallas, Miami, Houston, San Francisco, Phoenix, Los Angeles, Denver, Austin, Las Vegas

**International locations** (AIFS ENS + Open-Meteo): Tel Aviv, Munich, London, Tokyo, Seoul, Ankara, Lucknow, Wellington, Toronto, Paris, Milan, Sao Paulo, Warsaw, Singapore, Shanghai, Beijing, Shenzhen, Chengdu, Chongqing, Wuhan, Hong Kong, Buenos Aires

All 35 cities are wired end-to-end: forecast fetch, bucket matching, METAR (US), discovery, parsing. Both Celsius and Fahrenheit bucket types are supported.

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

# Update config values
python weather_trader.py --set entry_threshold=0.20

# Disable safeguards (not recommended)
python weather_trader.py --no-safeguards

# Disable trend detection
python weather_trader.py --no-trends

# Enable volatility targeting
python weather_trader.py --live --smart-sizing --vol-targeting

# Enable punt mode (on by default; use this to force-enable from CLI)
python weather_trader.py --punt-mode

# Quiet mode — only output on trades/errors
python weather_trader.py --live --quiet
```

## How It Works

Each cycle:

1. **Market discovery** — Fetches active weather markets from Simmer API, groups by event
2. **Ensemble fetch** — For each (location, date, metric):
   - Downloads AIFS ENS GRIB (CF + PF members) from ECMWF open data via AWS S3
   - Fetches 6 global models concurrently via Open-Meteo
   - Computes weighted average (`weighted_temp`), spread (`max_delta`), agreement %
   - For D+0 markets (local timezone): fetches live METAR as ground-truth anchor
3. **Signal strength** — Classifies ensemble agreement:
   - `strong`: ≥4 models, agreement ≥70%, max_delta ≤5°, confidence 0.88 — trade eligible
   - `moderate`: ≥3 models, max_delta ≤8°, confidence 0.80 — trade eligible
   - `weak`: otherwise, confidence 0.70 — **skipped**
   - `no_data`: models couldn't forecast date (too far ahead or missing data)
4. **Bucket matching** — Finds the Polymarket bucket matching `weighted_temp`. Two-pass algorithm: exact range match first, then threshold buckets ("X or higher" / "X or below") closest to forecast. Bucket values in Celsius are automatically converted to Fahrenheit before comparison (ensemble always returns °F). A `— nearest: [bucket]` suffix shows the closest non-matching bucket when the forecast is near a threshold.
5. **Safeguards** — Checks flip-flop warnings, slippage, time decay, market status, liquidity
6. **Trend detection** — Looks for recent price drops (stronger buy signal)
7. **Core entry** — If `edge = confidence - price ≥ MIN_EDGE (0.25)` AND `price < ENTRY_THRESHOLD (0.50)` AND spread ≤ `MAX_SPREAD (5.8°)`, safeguards pass → BUY. Candidates are ranked by edge; top `MAX_TRADES_PER_RUN (10)` execute.
8. **Punt scan** — After core trades, scans every bucket in each event for deep tail mispricings. A bucket qualifies if `price ≤ 14.9¢` AND Gaussian model probability ≥ 70% AND edge ≥ 50% AND not already held/core-matched. Executes at fixed $15 per punt up to $100/day, tagged `strategy="punt"` in the paper journal. CORE refuses any entry below 15¢ — those are PUNT territory.
9. **Exit** — For each open position, dynamic exit at `max(EXIT_THRESHOLD 0.45, entry_price × EXIT_PROFIT_MULTIPLIER 4.0)`. Optional laddered partial exits.
10. **Signal metadata** — Logged with trade: `weighted_temp`, `signal_strength`, `models_count`, `agreement_pct`, `max_delta`, `edge`, `strategy` (core|punt)

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
  Fetching ensemble forecast (AIFS ENS + 4-model blend)...
  AIFS ENS: 63.5°F | signal: weak | 4 models | agree: 66.7% | spread: 10.2°
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
| `strong` | ≥4 models, agreement ≥70%, max_delta ≤5° | 0.88 | Trade eligible |
| `moderate` | ≥3 models, max_delta ≤8° | 0.80 | Trade eligible |
| `weak` | fewer models or larger disagreement | 0.70 | **Skipped** |
| `single_source` | only 1 model returned data | 0.72 | Trade eligible (degraded) |
| `no_data` | no models returned data | — | **Skipped** |

METAR divergence on D+0 markets can downgrade strong→moderate (>5°) or moderate→weak (>8°).

## Safeguards

Before trading, the skill checks:
- **Flip-flop warning**: Skips if direction reversals are excessive on this market
- **Slippage**: Skips if estimated slippage > 15% (configurable)
- **Time decay**: Skips if market resolves in < 2 hours
- **Market status**: Skips if market already resolved
- **Liquidity**: Skips if market liquidity below `SIMMER_WEATHER_MIN_LIQUIDITY`
- **Signal strength**: Skips if `weak` or `no_data`
- **Bucket match**: Skips if no Polymarket bucket matches the forecast temperature

## Cron Setup — Python Interpreter

**Always use `python3.12` (system Python), NOT the hermes-agent venv Python.**
...
## Verifying Bot Health
Check lock file PID directly (no systemctl in WSL — WSL has no systemd user bus):
```bash
BOT_PID=$(cat ~/.polymarket-paper-bot/bot.lock 2>/dev/null | grep -oP 'pid:\s*\K\d+' | head -1)
kill -0 "$BOT_PID" 2>/dev/null && echo "Running PID $BOT_PID" || echo "Not running"
```
Lock file: `~/.polymarket-paper-bot/bot.lock`
```bash
python3.12 weather_trader.py --dry-run
```
Using plain `python3` (the hermes venv interpreter) fails with `ModuleNotFoundError: No module named 'simmer_sdk'`. The Polymarket Weather Scan cron was updated to use `python3.12`.

### AIFS ENS GRIB cache has stale files
Old GRIB files from Apr 12 (`2026-04-12_00_*.grib2`) sit in `~/.cache/aifs_ens/` alongside the active `latest_*.grib2` cache. The active cache is identified by `latest_*.grib2` — the stale files can be deleted manually.

## Manual Position Closing

`log_paper_trade()` is for **entries only** — it creates a new open trade. To manually close a position:

```python
import sys
sys.path.insert(0, '.')
from scripts.paper_journal import _load_trades, _save_trades
import datetime, requests

trades = _load_trades()
for t in trades:
    if t['market_id'] == 'MARKET_ID_HERE' and t['status'] == 'open':
        r = requests.get(f'https://api.simmer.markets/api/sdk/context/{t["market_id"]}',
            headers={'Authorization': f'Bearer {SIMMER_API_KEY}'})
        current_price = r.json()['market']['current_price']
        t['status'] = 'resolved'
        t['exit_price'] = current_price
        t['outcome'] = None
        t['pnl'] = (current_price - t['entry_price']) * t['shares']
        t['resolved_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
_save_trades(trades)
```

**Paper journal positions (`format_scan.py`) carry no `current_price`** — `get_positions()` in `format_scan.py` now fetches live prices from `GET https://api.simmer.markets/api/sdk/context/{market_id}` for each paper journal position. The journal uses different field names: `shares` (not `shares_yes`/`shares_no`), `cost` (not `cost_basis`), `side` in {"yes","no"} (not Simmer's `shares_yes > 0`). `compute_upnl()` handles both schemas.

**To get current price for any market:** `GET https://api.simmer.markets/api/sdk/context/{market_id}` (not `/v1/markets/`). The SDK's `get_market_context()` silently returns `None` — use the direct REST call.

## Troubleshooting

**"ModuleNotFoundError: No module named 'simmer_sdk'"**
- The cron or script is using the wrong Python interpreter. Use `python3.12` explicitly. See Cron Setup above.

**"No ensemble forecast for [date] (signal: no_data)"**
- Date is too far ahead for models to forecast (typically >10 days). This is expected for far-future markets.
- Also check: is the GRIB cache stale? Delete `~/.cache/aifs_ens/` if idx/grib mismatch errors appear.

**"Safeguard blocked: Severe flip-flop warning"**
- Too many direction reversals on this market. Wait before trading again.

**"Slippage too high"**
- Market is illiquid. Reduce position size or skip.

**"Resolves in Xh — too soon"**
- Elevated risk on imminent resolution. Skip.

**"signal: weak — skipped"**
- Ensemble has fewer than 3 models, or `max_delta > 8°`. Signal is not confident enough. This is intentional — weak-signal trades are skipped to avoid noise.

**"9 markets scanned in cron but only 1 city shows in manual runs"**
- Cron ran a full scan with `--dry-run`, found 9 events, generated signals
- Manual run shows only NYC because ALL 35 cities are cached in `data/discovery_cache.json` (TTL 180 min)
- When cities are cached, the run skips them silently with `"Discovery cache hit for {location} — skipping"`
- This is expected behavior — after a full cron scan, manual runs will only show cities not yet cached
- To force a full re-scan: delete `data/discovery_cache.json` or touch only the cities you want to re-scan

**"No bucket found for XX.X°F"**
- The forecast temperature doesn't fall within any of the Polymarket buckets for that market. This is normal — it means the forecast doesn't align with the market's defined ranges.
- If a Celsius market (e.g. Tokyo, Shanghai): the bucket value is converted from °C to °F before comparison. The ensemble always returns Fahrenheit; `parse_temperature_bucket` detects the market's unit and converts accordingly. Sentinel values (-999/999) for "or higher"/"or below" buckets are preserved through conversion.
- The output shows `— nearest: [bucket]` when the forecast is close to a bucket threshold — useful for seeing near-miss signals on tight Celsius markets.

**"External wallet requires a pre-signed order"**
- `WALLET_PRIVATE_KEY` is not set. Fix: `export WALLET_PRIVATE_KEY=0x<your-polymarket-wallet-private-key>`. The SDK signs orders automatically.

**"Balance shows $0 but I have USDC on Polygon"**
- Polymarket uses **USDC.e** (bridged USDC, contract `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`). Bridge native USDC to USDC.e if needed.

**"Simmer API returning 401 Unauthorized on `/api/sdk/markets`"**
- If `GET /api/sdk/markets` returns `401 {"detail": "..."}`, the API key is invalid, expired, or lacks permissions
- The skill's `weather_trader.py` uses Simmer's CLOB proxy endpoint for market discovery, not the raw `/markets` endpoint
- Discovery works via Simmer's internal market list (the SDK wraps it differently), so a 401 on raw `/markets` may not block the bot
- However, if ALL Simmer API calls fail with 401, market discovery falls back to an empty list and no new trades will be found
- **Fix**: Verify API key at `simmer.markets/dashboard → SDK tab`. Re-check the key hasn't been rotated or expired.
- `GET /api/sdk/context/{market_id}` also returned `{"detail": "..."}` in recent testing — this broke live price lookups for open positions

**"Verifying bot health — lock file not found"**
- The skill's health check looks for `~/.polymarket-paper-bot/bot.lock`, but **the bot does not run as a persistent daemon**
- The cron job runs `python3.12 weather_trader.py --dry-run` as a **one-shot** command — no lock file is created
- To verify the cron actually fired, check instead:
  - `ls -la ~/.hermes/skills/polymarket-weather-trader/data/discovery_cache.json` — timestamps show last run
  - `ls -la ~/.hermes/skills/polymarket-weather-trader/data/paper_trades.jsonl` — same
  - Paper trades journal: `python3.12 scripts/paper_journal.py --backfill` to force-settle any ready trades
- `paper_trades.jsonl` is the authoritative source for open positions and P&L

**"Simmer API price for Shenzhen was $0.21 but actual market showed <1% (~$0.001)"**
- Simmer's `current_price` can be **stale or wrong** for illiquid/near-zero buckets
- Always cross-check against the live Polymarket page for position-critical decisions
- When locking P&L: check the Polymarket page directly (browser or scraper) for the real bucket price
- `GET /api/sdk/context/{market_id}` is fine for detection but unreliable for final settlement price

**"Paper trade never auto-resolved"**
- `clob.polymarket.com` returns **403 Forbidden** — bot-blocked without browser session cookies
- `_fetch_market_resolution()` now uses Simmer API (`/api/sdk/context/{market_id}`) for `resolved: True/False`
- **Limitation**: Simmer returns `resolved: True` but `outcome: null` — it does not surface the winning bucket
- As a result, `update_resolved_trades()` can detect resolution but cannot compute P&L automatically
- Manual close is required: get real bucket price from Polymarket page, then update journal manually

**Manual Position Closing (updated)**
```python
# Simmer's current_price can be wrong for illiquid buckets (<1% priced tokens)
# For position-critical decisions, ALWAYS check the Polymarket page directly
# Then update the paper journal:

import json, datetime

journal = "/path/to/data/paper_trades.jsonl"
with open(journal) as f:
    trades = [json.loads(l) for l in f]

EXIT_PRICE = 0.001  # from Polymarket page (bucket's implied probability)
for t in trades:
    if t['market_id'] == 'MARKET_ID' and t['status'] == 'open':
        t['status'] = 'resolved'
        t['outcome'] = None  # unknown from Simmer
        t['exit_price'] = EXIT_PRICE
        t['pnl'] = round((EXIT_PRICE - t['entry_price']) * t['shares'], 4)
        t['resolved_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()

with open(journal, 'w') as f:
    for t in trades:
        f.write(json.dumps(t, ensure_ascii=False) + '\n')
```

## File Structure

```
polymarket-weather-trader/
├── weather_trader.py             # Main entry point
├── dashboard.py                 # FastAPI dashboard (port 8414)
├── config.json                   # Runtime configuration overrides
├── clawhub.json                  # Skill registration + tunables
├── _meta.json                    # Version metadata
├── requirements.txt              # Python dependencies
├── README.md                     # Quick overview
├── SKILL.md                      # This file
└── scripts/
    ├── aifs_forecast.py          # AIFS ENS GRIB download + parse (CF + PF members)
    ├── ensemble_forecast.py      # Multi-model ensemble (weighted blend + METAR)
    ├── forecast_validator.py     # Forecast consistency checks (standalone)
    ├── forecast_history.py       # Forecast accuracy journal (JSONL)
    ├── paper_journal.py          # Local paper trading journal + P&L tracking
    └── status.py                 # Balance and position checks
```

## Key Functions

```python
# In scripts/ensemble_forecast.py
get_ensemble_forecast(city, date_str, metric, unit) -> dict
# Returns:
#   weighted_temp   # weighted average temperature
#   model_temps     # {model_name: temp}
#   models_count    # how many models returned data
#   max_delta       # worst disagreement (degrees)
#   agreement_pct   # % of models within 3° of weighted avg
#   signal_strength # "strong"|"moderate"|"weak"|"single_source"|"no_data"
#   metar_temp      # live station obs (D+0 markets only)
#   metar_delta     # abs(ensemble - METAR)
```
