# Bias unit routing check — 2026-06-01

## Root cause

`LOCATION_BIAS_C` was only applied inside the `is_international` branch in `scripts/weather_trader.py`.
That meant configured bias values for non-`INTERNATIONAL_LOCATIONS` cities like Seattle, Beijing, Chongqing, and Shenzhen were inert at entry time.

## Fix

- infer the event's traded unit from the actual market buckets, not the city list
- apply `LOCATION_BIAS_C` in the market's native unit before rounding for settlement
- filter CORE and PUNT bucket scans to the event's expected unit
- update `scripts/no_bias_pnl_compare.py` to use the same unit-routing logic
- add regression coverage in `tests/test_parsers.py`

## Verification

- `python3 -m py_compile scripts/weather_trader.py scripts/no_bias_pnl_compare.py`
- `python3 -m unittest tests.test_parsers`
- result: 37 tests passed

## Replay after fix

Scope: 46 resolved YES trades across Chongqing, London, Beijing, Tel Aviv, Seoul, Shenzhen, Seattle.

Using the suggested calibrations:
- Chongqing `+3.5`
- London `+3.3`
- Beijing `+3.1`
- Tel Aviv `+2.6`
- Seoul `-2.4`
- Shenzhen `+2.2`
- Seattle `+0.9`

Resolved losing YES trades in scope: 24
Would flip from loss to positive settlement P&L: 5

Flips:
- Shenzhen 2026-05-14: `28°C` -> `31°C or higher`
- Shenzhen 2026-04-22: `28°C` -> `31°C`
- Tel Aviv 2026-05-09: stayed `27°C`, but the replayed settlement-hold P&L is positive
- Seattle 2026-05-23: `63°F or below` -> `64-65°F`
- Seoul 2026-05-26: `23°C` -> `25°C or higher`

## Files touched

- `scripts/weather_trader.py`
- `scripts/no_bias_pnl_compare.py`
- `tests/test_parsers.py`
