# Polymarket Weather Trader — Lessons Learned

Pruned to the durable stuff that still matters on the current stack.

## 2026-04-29 — PM needs a corpse-price guard, not just weather logic

**Symptom:** Buenos Aires 18°C YES and Sao Paulo 25°C-or-below YES were held during PM check near 4¢, then auto-exited later at even worse fills.

**Lesson:** Same-day PM logic cannot rely only on forecast support and relative collapse checks. If the market already prices a mature core position like a corpse, PM needs permission to get out.

**Current rule:** `position_manager.py` uses a corpse-price guard with:
- `position_corpse_price_floor` default `0.05`
- `position_corpse_entry_frac` default `0.35`

That catches dying core positions without nuking cheap punts.

## 2026-04-29 — A strict skipped-signal audit can still show “nothing missed”

Audit slice:
- `signal_strength = strong`
- `agreement_pct = 100%`
- `spread <= 2.0`
- dates `2026-04-25` through `2026-04-28`
- excluding events eventually traded

Result: only 2 candidates, 0 would-have-won.

**Lesson:** D+0 core skips are not automatically lost alpha. Sometimes the skip rules are doing their job.

## 2026-04-29 — Source `.env` before any manual scan or PM run

**Symptom:** Manual weather scan failed with `SIMMER_API_KEY environment variable not set`.

**Lesson:** The project depends on `.env` for Simmer auth. Manual invocations must source it first.

**Use:**
```bash
cd /home/brandon/projects/polymarket-weather-trader
set -a && source .env && set +a
```

## 2026-04-29 — For a real fresh scan, kill both discovery and forecast reuse

A normal manual run may silently look stale because discovery cache and forecast cache are still warm.

**Use this when Brandon asks for a truly fresh weather scan:**
```bash
SIMMER_WEATHER_DISCOVERY_CACHE_MIN=0 \
SIMMER_WEATHER_FORECAST_CACHE_DISK=false \
python3.12 scripts/weather_trader.py
```

## 2026-04-29 — Dashboard binding once does not mean it stayed alive

**Symptom:** Dashboard was reported as started, then the process got killed right after.

**Lesson:** Verify the listener after launch. Don't claim success off the spawn alone.

**Reliable pattern:**
```bash
setsid /usr/bin/python3.12 /home/brandon/projects/polymarket-weather-trader/scripts/dashboard.py >/tmp/polymarket-dashboard.log 2>&1 < /dev/null &
ss -tlnp | grep 8414
```

## Canonical repo and interpreter

- Repo: `/home/brandon/projects/polymarket-weather-trader`
- Run from this repo, not the old workspace copy
- Use `python3.12` for manual commands

## Paper journal is the source of truth

For paper mode:
- `data/paper_trades.jsonl` is authoritative
- Simmer can be missing positions, stale, or auth-broken
- Dashboard and helper tools should fall back to the journal when in doubt

## Live price lookup has two ID paths

Polymarket data in this stack can use two market ID shapes:

- **CLOB UUID**: works with Simmer context
- **Gamma integer ID**: requires Gamma → CLOB token lookup

**Lesson:** If a dashboard price is mysteriously zero or blank, check the ID format before assuming the market is dead.

## Illiquid bucket prices can be garbage

Simmer `current_price` is good enough for rough monitoring, but it can be stale or wrong on near-zero, thinly traded buckets.

**Lesson:** For position-critical calls on ugly illiquid buckets, sanity-check against the actual Polymarket market page or direct market data path.

## AIFS / cfgrib cache failures are usually index-file issues, not strategy issues

Two durable facts:
- corrupt or partial GRIB files can pass naive freshness checks
- stale `.idx` files can break otherwise valid fresh GRIBs

**Lesson:** When you see cfgrib noise, separate “cache plumbing issue” from “forecast logic issue”. Fixing the cache layer is usually enough.

## Weak-signal edge alone is not enough

A mathematically pretty edge on a weak, wide-spread forecast is still junk.

**Current protection:** `MAX_SPREAD = 5.8°` hard cap in `scripts/weather_trader.py` blocks noisy trades even if edge looks attractive.

## Resolved-market data is still messy across providers

Durable behavior:
- Simmer can know a market is resolved without surfacing the winning outcome cleanly
- Gamma can lag or expose weird partial state during settlement windows
- dashboard/journal tooling must handle missing marks and missing outcomes gracefully

**Lesson:** Treat settlement plumbing as messy by default. Don't build logic that assumes one clean provider response.

## Skip logs are useful only when cross-checked against forecasts and trades

`skip_events.jsonl` by itself is noisy.

A real audit needs all three:
- `data/forecast_history.jsonl`
- `scripts/data/skip_events.jsonl`
- `data/paper_trades.jsonl`

Otherwise you end up counting events that were eventually traded or were never actionable.

## Durable operating shortcuts

### PM check
```bash
python3.12 scripts/position_manager.py
```

### Fresh scan
```bash
SIMMER_WEATHER_DISCOVERY_CACHE_MIN=0 \
SIMMER_WEATHER_FORECAST_CACHE_DISK=false \
python3.12 scripts/weather_trader.py
```

### Dashboard restart
```bash
pkill -f dashboard.py || true
setsid /usr/bin/python3.12 /home/brandon/projects/polymarket-weather-trader/scripts/dashboard.py >/tmp/polymarket-dashboard.log 2>&1 < /dev/null &
```

### Open paper positions
```bash
grep '"status": "open"' /home/brandon/projects/polymarket-weather-trader/data/paper_trades.jsonl
```

## Removed from this file on purpose

Pruned out:
- old Hermes-specific tool workarounds
- one-off patching mistakes
- obsolete path confusion repeated five different ways
- stale dashboard implementation notes that no longer describe the live system

If a lesson isn't changing present-day behavior, it doesn't belong here.