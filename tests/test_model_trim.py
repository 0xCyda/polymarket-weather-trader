#!/usr/bin/env python3
"""
Compare spread/signal with all 8 models vs top 4 by weight.
Run: python3 tests/test_model_trim.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from ensemble_forecast import get_ensemble_forecast, ENSEMBLE_MODELS
from datetime import datetime, timedelta, timezone

# Cities with recent strong signals
CITIES = [
    "Munich", "Milan", "Warsaw", "San Francisco", "Hong Kong",
    "NYC", "Tokyo", "Wellington"
]

# Top 4 by weight order
TOP4 = {"ecmwf_ifs025", "aifs_ens", "gfs_seamless", "meteofrance_seamless"}

def compute_stats(model_temps, weights):
    if len(model_temps) < 2:
        return None, None, None
    total_w = sum(weights[m] for m in model_temps if m in weights)
    w_temp = sum(model_temps[m] * (weights[m] / total_w) for m in model_temps if m in weights)
    w_temp = round(w_temp, 1)
    temps = [model_temps[m] for m in model_temps if m in weights]
    spread = round(max(temps) - min(temps), 1)
    within_3 = sum(1 for t in temps if abs(t - w_temp) <= 3)
    agree = round(100.0 * within_3 / len(temps), 1)
    return w_temp, spread, agree

def main():
    # Target tomorrow (most likely to have open markets)
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    print(f"\nForecast comparison — target: {tomorrow}")
    print(f"{'City':<16} {'ALL8':>6} {'spread':>7} {'agree':>7}  |  {'TOP4':>6} {'spread':>7} {'agree':>7}  {'delta_spread':>13}")
    print("-" * 80)

    for city in CITIES:
        result = get_ensemble_forecast(city, tomorrow, metric="high", unit="F")
        mt = result.get("model_temps", {})
        if not mt:
            print(f"{city:<16}  no data")
            continue

        all8_w = result["weighted_temp"]
        all8_spread = result["max_delta"]
        all8_agree = result["agreement_pct"]

        top4_mt = {m: t for m, t in mt.items() if m in TOP4}
        top4_w, top4_spread, top4_agree = compute_stats(top4_mt, ENSEMBLE_MODELS)

        if top4_w is None:
            print(f"{city:<16}  insufficient top4 data")
            continue

        delta = round((all8_spread or 0) - (top4_spread or 0), 1)
        flag = " <-- WIDER" if delta > 3 else ""
        print(
            f"{city:<16} {all8_w:>6.1f}F {all8_spread:>6.1f}° {all8_agree:>6.1f}%"
            f"  |  {top4_w:>6.1f}F {top4_spread:>6.1f}° {top4_agree:>6.1f}%"
            f"  spread diff: {delta:>+.1f}°{flag}"
        )
        # Show which models are diverging
        if mt:
            sorted_models = sorted(mt.items(), key=lambda x: ENSEMBLE_MODELS.get(x[0], 0), reverse=True)
            model_str = "  ".join(f"{m}:{t:.1f}" for m, t in sorted_models)
            print(f"  {model_str}")

    print()
    print(f"Top 4 models: {', '.join(sorted(TOP4, key=lambda m: -ENSEMBLE_MODELS[m]))}")
    print(f"Bottom 4 models: {', '.join(m for m in ENSEMBLE_MODELS if m not in TOP4)}")

if __name__ == "__main__":
    main()
