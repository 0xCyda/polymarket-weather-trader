# Dynamic Error Balancing (DEB) — Implementation Plan

**Status:** deferred as of 2026-04-23. Revisit mid-May 2026.
**Reason:** insufficient data to train per-city weights. Only 14 resolved paper trades; `forecast_history.jsonl` has 268 rows with no `actual_temp` backfilled. A bucket-match bug is also leaking signals, which is a bigger edge problem than weighting.
**Prerequisites before resuming:** (1) fix bucket-match bug, (2) ship Phase 1 backfill script (`backfill_forecast_actuals.py`) to start accumulating actuals now — pure data collection, no weighting change, (3) tighten entry logic when forecast is within ~1°F of a bucket edge.
**When resuming:** confirm 30+ samples per (city, model) pair; start in shadow mode; consider constrained regression over inverse-MAE to handle model correlation (AIFS trained on ECMWF IFS reanalysis); evaluate on P&L, not just MAE.
**Inspiration:** `yangyuan-zhen/PolyWeather`.
**Goal:** replace our static per-model weights with per-city weights learned from
recent forecast error. Tightens edge on hard cities (Tokyo, Shanghai, Wellington)
where one model is systematically more or less reliable.

---

## The Problem

Today `ENSEMBLE_MODELS` has global weights:

```python
ENSEMBLE_MODELS = {
    "ecmwf_ifs025":         0.24,
    "aifs_ens":             0.18,
    "gfs_seamless":         0.14,
    "meteofrance_seamless": 0.10,
}
```

Every city gets the same weights. But models have local biases:

- GFS is weak in complex terrain (Wellington, Chongqing — mountain adjacency).
- ECMWF IFS tends to run cold in tropical coastal (Hong Kong, Miami, Singapore).
- AIFS ENS has known issues at very high latitudes (not relevant for our set).
- MeteoFrance ARPEGE is strongest in Europe, weaker in Asia.

A per-city weighted ensemble outperforms a globally-weighted one on out-of-sample
error. The weights should be inverse to each model's recent MAE for that city.

---

## Data Requirements

We need two things per `(city, model, day)`:

1. **Model forecast** — we already log this in `forecast_history.jsonl` under
   `model_temps`. Requires no new instrumentation.
2. **Actual resolution temp** — currently absent from `forecast_history`; present
   in `paper_trades.jsonl` for resolved trades (via `backfill_actual_temps.py`).

**Gap to fill:** an actuals backfill that targets `forecast_history.jsonl`, not
just `paper_trades.jsonl`. Needs a new script:
`scripts/backfill_forecast_actuals.py` that loops past-dated forecast_history
entries, calls `fetch_historical_temp(loc, date, "high", "F")`, and writes
`actual_temp` + per-model errors back to the row.

A ~14 day rolling window of forecasts × ~4 models × ~35 cities = ~2000 error
samples. Enough for stable per-city weights after two weeks of data collection.

---

## Algorithm

### Error calculation (daily, at resolution)

For each `(city, model)` where both forecast and actual exist:

```
abs_err     = |forecast - actual|
signed_err  = forecast - actual   # + = model warm-biased, - = cold-biased
```

Store as `data/model_errors.jsonl`:

```json
{"date":"2026-04-22","city":"Wellington","model":"aifs_ens",
 "forecast":58.1,"actual":57.2,"abs_err":0.9,"signed_err":0.9}
```

### Per-city weight recomputation (daily, after backfill)

Over the last N days (default N=14):

```python
def compute_city_weights(city, window_days=14):
    errors = load_errors(city, days=window_days)
    # Group by model, compute MAE per model for this city
    mae_by_model = {m: mean(e.abs_err for e in errors if e.model == m)
                    for m in ENSEMBLE_MODELS}
    # Inverse-error weights: lower MAE = higher weight
    # Softplus floor to prevent div-by-zero and runaway weights
    inv = {m: 1.0 / max(mae, 1.5) for m, mae in mae_by_model.items()}
    total = sum(inv.values())
    return {m: w / total for m, w in inv.items()}
```

Floor of `1.5°F` on MAE reflects the irreducible uncertainty from gridding +
station representativeness — stops any model from getting 80% weight on a
lucky week.

### Fallback logic

- Until a city has ≥ 10 samples per model, use the global `ENSEMBLE_MODELS`
  weights (current behavior).
- Blend: `weight = α × city_weight + (1 − α) × global_weight`, with
  `α = min(samples / 30, 1.0)` so weights phase in smoothly as data accrues.

### Per-city bias correction

Signed errors also reveal systematic bias. After weight computation, optionally
apply a bias offset:

```python
bias_c = median(e.signed_err for e in errors)  # stable vs mean
corrected_temp = weighted_temp - bias_c
```

This is orthogonal to weighting — it corrects for directional model drift
(e.g. if *every* model runs cold for Hong Kong, the ensemble still runs cold).
Already implemented as `LOCATION_BIAS_C` for HK and Shenzhen; DEB makes this
data-driven and auto-updating instead of hand-coded.

---

## Integration Points

1. **New file:** `scripts/model_error_tracker.py` — writes `model_errors.jsonl`
   from backfilled forecast_history entries.
2. **New file:** `scripts/backfill_forecast_actuals.py` — cron daily, pulls
   actuals into forecast_history.
3. **Modified:** `ensemble_forecast.py::get_ensemble_forecast` — replaces the
   hardcoded `ENSEMBLE_MODELS` lookup with `get_city_weights(city)`.
4. **Modified:** `weather_trader.py::LOCATION_BIAS_C` — dict becomes a function
   backed by DEB error data, with the current hand-coded values as fallback.
5. **Dashboard:** add a "Model Accuracy" panel showing per-city MAE by model
   over last 14d.

---

## Rollout Phases

**Phase 1 — data collection** (week 1):
- Ship `backfill_forecast_actuals.py` + cron.
- Start accumulating `model_errors.jsonl`.
- Don't change production weights yet.

**Phase 2 — shadow mode** (week 2):
- Compute DEB weights but log both `weighted_temp_global` and
  `weighted_temp_deb` to forecast_history.
- Run paper backtest comparing the two on the week's resolved trades.

**Phase 3 — go live** (week 3):
- Once DEB beats global by > 0.5°F MAE on shadow, switch primary weighting.
- Keep global as fallback for new cities / insufficient data.

---

## Risks & Guardrails

- **Overfit** to a lucky 14d window. Mitigation: MAE floor of 1.5°F + minimum
  sample count before any rebalance.
- **Regime change** (seasonal transition). Mitigation: exponential weighting
  toward recent days (`decay = 0.9 ^ days_ago`) — optional, start without.
- **One model dominating** post-normalization. Mitigation: clamp any single
  model's weight to `[0.05, 0.60]`.
- **Silent data rot** if backfill fails. Mitigation: dashboard panel surfaces
  per-city sample counts and data age.
