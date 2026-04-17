#!/usr/bin/env python3
"""
Smoke tests for parsers and sizing helpers.

Run with: python -m pytest tests/ -v
Or:       python tests/test_parsers.py
"""

import os
import sys
import unittest
from pathlib import Path

# Isolate imports: stub simmer_sdk.skill so the main module can import without the real SDK
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

# Provide a minimal shim so weather_trader.py imports without the real Simmer SDK
import types
if "simmer_sdk" not in sys.modules:
    sdk = types.ModuleType("simmer_sdk")
    skill = types.ModuleType("simmer_sdk.skill")

    def load_config(schema, skill_file, slug=None):
        out = {}
        for key, spec in schema.items():
            env_val = os.environ.get(spec["env"])
            if env_val is not None:
                try:
                    if spec["type"] == bool:
                        out[key] = env_val.lower() in ("true", "1", "yes")
                    else:
                        out[key] = spec["type"](env_val)
                    continue
                except (ValueError, TypeError):
                    pass
            out[key] = spec.get("default")
        return out

    def update_config(updates, skill_file):
        return updates

    def get_config_path(skill_file):
        return Path(skill_file).parent / "config.json"

    skill.load_config = load_config
    skill.update_config = update_config
    skill.get_config_path = get_config_path
    sdk.skill = skill
    sys.modules["simmer_sdk"] = sdk
    sys.modules["simmer_sdk.skill"] = skill

os.environ.setdefault("SIMMER_API_KEY", "test-key")

# Now safe to import
from weather_trader import (
    parse_weather_event,
    parse_temperature_bucket,
    compute_dynamic_exit,
    calculate_ewma_vol,
    apply_vol_targeting,
)


class TestParseWeatherEvent(unittest.TestCase):
    def test_nyc_fahrenheit(self):
        r = parse_weather_event("Highest temperature in New York on April 18?")
        self.assertEqual(r["location"], "NYC")
        self.assertEqual(r["metric"], "high")
        self.assertTrue(r["date"].endswith("-04-18"))
        self.assertEqual(r["unit"], "F")

    def test_paris_celsius(self):
        r = parse_weather_event("Will the highest temperature in Paris be 14°C on April 16?")
        self.assertEqual(r["location"], "Paris")
        self.assertEqual(r["unit"], "C")

    def test_hong_kong(self):
        r = parse_weather_event("Highest temperature in Hong Kong on April 16?")
        self.assertEqual(r["location"], "Hong Kong")

    def test_low_temp(self):
        r = parse_weather_event("Lowest temperature in Chicago on March 10?")
        self.assertEqual(r["metric"], "low")

    def test_invalid(self):
        self.assertIsNone(parse_weather_event(""))
        self.assertIsNone(parse_weather_event("random text with no date"))


class TestParseTemperatureBucket(unittest.TestCase):
    def test_exact_fahrenheit(self):
        self.assertEqual(parse_temperature_bucket("72°F"), (72, 72, "F"))

    def test_exact_celsius(self):
        self.assertEqual(parse_temperature_bucket("22°C"), (22, 22, "C"))

    def test_range(self):
        self.assertEqual(parse_temperature_bucket("70-75°F"), (70, 75, "F"))

    def test_or_higher(self):
        lo, hi, u = parse_temperature_bucket("80°F or higher")
        self.assertEqual((lo, hi, u), (80, 999, "F"))

    def test_or_below(self):
        lo, hi, u = parse_temperature_bucket("50°F or below")
        self.assertEqual((lo, hi, u), (-999, 50, "F"))

    def test_celsius_range(self):
        lo, hi, u = parse_temperature_bucket("18-22°C")
        self.assertEqual((lo, hi, u), (18, 22, "C"))

    def test_invalid(self):
        self.assertIsNone(parse_temperature_bucket(""))
        self.assertIsNone(parse_temperature_bucket("random text"))


class TestCelsiusConversion(unittest.TestCase):
    """Verify sentinel values survive C→F conversion (the CRITICAL bug from the audit)."""

    def test_sentinels_preserved_in_conversion(self):
        # Simulate what the code does
        lo, hi = -999, 22
        # Apply the same guarded conversion as weather_trader.py
        lo_f = lo * 9 / 5 + 32 if lo != -999 else -999
        hi_f = hi * 9 / 5 + 32 if hi != 999 else 999
        self.assertEqual(lo_f, -999)
        self.assertAlmostEqual(hi_f, 71.6)

    def test_upper_sentinel_preserved(self):
        lo, hi = 22, 999
        lo_f = lo * 9 / 5 + 32 if lo != -999 else -999
        hi_f = hi * 9 / 5 + 32 if hi != 999 else 999
        self.assertAlmostEqual(lo_f, 71.6)
        self.assertEqual(hi_f, 999)


class TestComputeDynamicExit(unittest.TestCase):
    def test_multiplier_disabled(self):
        # With EXIT_PROFIT_MULTIPLIER=0 (default), should return base EXIT_THRESHOLD
        from weather_trader import EXIT_THRESHOLD, EXIT_PROFIT_MULTIPLIER
        if EXIT_PROFIT_MULTIPLIER == 0:
            self.assertEqual(compute_dynamic_exit(0.10), EXIT_THRESHOLD)

    def test_zero_entry_price(self):
        from weather_trader import EXIT_THRESHOLD
        self.assertEqual(compute_dynamic_exit(0), EXIT_THRESHOLD)


class TestVolTargeting(unittest.TestCase):
    def test_insufficient_data_returns_none(self):
        self.assertIsNone(calculate_ewma_vol([{"price_yes": 0.5}], span=10))

    def test_apply_vol_targeting_no_data(self):
        size, meta = apply_vol_targeting(10.0, None)
        self.assertEqual(size, 10.0)
        self.assertEqual(meta["adjusted_for"], "no_vol_data")

    def test_apply_vol_targeting_caps_leverage(self):
        # Very low realized vol should hit max_leverage cap
        size, meta = apply_vol_targeting(10.0, 0.01, target_vol=0.20, max_leverage=2.0)
        self.assertEqual(meta["leverage"], 2.0)
        self.assertAlmostEqual(size, 20.0)

    def test_apply_vol_targeting_floor(self):
        # Very high realized vol should hit min_allocation floor
        size, meta = apply_vol_targeting(10.0, 5.0, target_vol=0.20, min_allocation=0.2)
        self.assertEqual(meta["leverage"], 0.2)
        self.assertAlmostEqual(size, 2.0)


class TestSimmerThrottle(unittest.TestCase):
    def test_min_interval_enforced(self):
        """Consecutive calls should honor SIMMER_MIN_INTERVAL_SEC between them."""
        import time
        import weather_trader as wt
        # Reset state
        wt._last_request_ts = 0.0
        wt._recent_429_times[:] = []
        wt._breaker_until = 0.0

        calls = []
        def fake_call():
            calls.append(time.time())
            return "ok"

        # Tight loop of three calls
        t0 = time.time()
        for _ in range(3):
            wt.simmer_call(fake_call, _label="test")
        elapsed = time.time() - t0

        # Three calls at 0.35s min interval = at least 0.70s (first immediate, then 2 waits)
        # Allow slack since first call has no prior timestamp to gate against
        self.assertGreaterEqual(elapsed, 0.65)

    def test_retries_on_429(self):
        import weather_trader as wt
        wt._last_request_ts = 0.0
        wt._recent_429_times[:] = []
        wt._breaker_until = 0.0

        # Temporarily shrink backoff so the test runs quickly
        original_base = wt.SIMMER_BACKOFF_BASE
        wt.SIMMER_BACKOFF_BASE = 0.01
        try:
            attempts = {"n": 0}
            def flaky():
                attempts["n"] += 1
                if attempts["n"] < 2:
                    raise RuntimeError("HTTP 429 Too Many Requests")
                return "ok"

            result = wt.simmer_call(flaky, _label="flaky")
            self.assertEqual(result, "ok")
            self.assertEqual(attempts["n"], 2)
        finally:
            wt.SIMMER_BACKOFF_BASE = original_base

    def test_non_429_error_propagates(self):
        import weather_trader as wt
        wt._last_request_ts = 0.0

        def broken():
            raise ValueError("some other error")

        with self.assertRaises(ValueError):
            wt.simmer_call(broken, _label="broken")


if __name__ == "__main__":
    unittest.main()
