#!/usr/bin/env python3
"""Targeted tests for cheap exact-bucket CORE entry guards."""

import os
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

if "simmer_sdk" not in sys.modules:
    sdk = types.ModuleType("simmer_sdk")
    skill = types.ModuleType("simmer_sdk.skill")

    def load_config(schema, skill_file, slug=None):
        return {key: spec.get("default") for key, spec in schema.items()}

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

if "ensemble_forecast" not in sys.modules:
    ensemble = types.ModuleType("ensemble_forecast")
    ensemble.get_ensemble_forecast = lambda *args, **kwargs: {}
    sys.modules["ensemble_forecast"] = ensemble

if "aifs_forecast" not in sys.modules:
    aifs = types.ModuleType("aifs_forecast")
    aifs.prewarm_grib_cache = lambda *args, **kwargs: None
    sys.modules["aifs_forecast"] = aifs

os.environ.setdefault("SIMMER_API_KEY", "test-key")

import weather_trader as wt


class TestExactCoreRules(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "CORE_LOW_EDGE_EXACT_CARVEOUT_ENABLED": wt.CORE_LOW_EDGE_EXACT_CARVEOUT_ENABLED,
            "CORE_LOW_EDGE_EXACT_CARVEOUT_MIN_EDGE": wt.CORE_LOW_EDGE_EXACT_CARVEOUT_MIN_EDGE,
            "CORE_LOW_EDGE_EXACT_CARVEOUT_MIN_PRICE": wt.CORE_LOW_EDGE_EXACT_CARVEOUT_MIN_PRICE,
            "CORE_LOW_EDGE_EXACT_CARVEOUT_MAX_PRICE": wt.CORE_LOW_EDGE_EXACT_CARVEOUT_MAX_PRICE,
            "MIN_EDGE": wt.MIN_EDGE,
        }

    def tearDown(self):
        for key, value in self._saved.items():
            setattr(wt, key, value)

    def test_cheap_exact_bucket_requires_clean_signal(self):
        rb = {
            "bucket": (22, 22, "C"),
            "price": 0.19,
            "confidence": 0.58,
            "edge": 0.39,
        }

        allowed, reason = wt._core_exact_bucket_allowed(rb, signal_strength="moderate", spread=4.2)

        self.assertFalse(allowed)
        self.assertIn("cheap_exact_guard", reason)

    def test_cheap_exact_bucket_can_still_trade_when_setup_is_unusually_clean(self):
        rb = {
            "bucket": (22, 22, "C"),
            "price": 0.19,
            "confidence": 0.72,
            "edge": 0.53,
        }

        allowed, reason = wt._core_exact_bucket_allowed(rb, signal_strength="strong", spread=2.8)

        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_range_bucket_is_not_subject_to_exact_guard(self):
        rb = {
            "bucket": (21, 23, "C"),
            "price": 0.19,
            "confidence": 0.50,
            "edge": 0.31,
        }

        allowed, reason = wt._core_exact_bucket_allowed(rb, signal_strength="moderate", spread=5.0)

        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_low_edge_exact_carveout_allows_strong_exact_in_price_band(self):
        wt.CORE_LOW_EDGE_EXACT_CARVEOUT_ENABLED = True
        wt.CORE_LOW_EDGE_EXACT_CARVEOUT_MIN_EDGE = 0.05
        wt.CORE_LOW_EDGE_EXACT_CARVEOUT_MIN_PRICE = 0.30
        wt.CORE_LOW_EDGE_EXACT_CARVEOUT_MAX_PRICE = 0.40
        wt.MIN_EDGE = 0.25
        rb = {
            "bucket": (22, 22, "C"),
            "price": 0.35,
            "edge": 0.10,
        }

        allowed, reason = wt._core_low_edge_exact_carveout_allowed(rb, signal_strength="strong")

        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_low_edge_exact_carveout_rejects_price_below_band(self):
        wt.CORE_LOW_EDGE_EXACT_CARVEOUT_ENABLED = True
        rb = {
            "bucket": (22, 22, "C"),
            "price": 0.18,
            "edge": 0.10,
        }

        allowed, reason = wt._core_low_edge_exact_carveout_allowed(rb, signal_strength="strong")

        self.assertFalse(allowed)
        self.assertIn("price=0.18<", reason)

    def test_low_edge_exact_carveout_rejects_range_bucket(self):
        wt.CORE_LOW_EDGE_EXACT_CARVEOUT_ENABLED = True
        rb = {
            "bucket": (21, 23, "C"),
            "price": 0.35,
            "edge": 0.10,
        }

        allowed, reason = wt._core_low_edge_exact_carveout_allowed(rb, signal_strength="strong")

        self.assertFalse(allowed)
        self.assertEqual(reason, "not_exact")

    def test_low_edge_exact_carveout_rejects_non_strong_signal(self):
        wt.CORE_LOW_EDGE_EXACT_CARVEOUT_ENABLED = True
        rb = {
            "bucket": (22, 22, "C"),
            "price": 0.35,
            "edge": 0.10,
        }

        allowed, reason = wt._core_low_edge_exact_carveout_allowed(rb, signal_strength="moderate")

        self.assertFalse(allowed)
        self.assertEqual(reason, "signal=moderate")

    def test_low_edge_exact_carveout_rejects_price_above_band(self):
        wt.CORE_LOW_EDGE_EXACT_CARVEOUT_ENABLED = True
        rb = {
            "bucket": (22, 22, "C"),
            "price": 0.42,
            "edge": 0.10,
        }

        allowed, reason = wt._core_low_edge_exact_carveout_allowed(rb, signal_strength="strong")

        self.assertFalse(allowed)
        self.assertIn("price=0.42>=", reason)

    def test_low_edge_exact_carveout_rejects_tiny_edge(self):
        wt.CORE_LOW_EDGE_EXACT_CARVEOUT_ENABLED = True
        wt.CORE_LOW_EDGE_EXACT_CARVEOUT_MIN_EDGE = 0.05
        rb = {
            "bucket": (22, 22, "C"),
            "price": 0.35,
            "edge": 0.03,
        }

        allowed, reason = wt._core_low_edge_exact_carveout_allowed(rb, signal_strength="strong")

        self.assertFalse(allowed)
        self.assertIn("edge=0.03<0.05", reason)

    def test_low_edge_exact_carveout_rejects_edge_already_above_main_floor(self):
        wt.CORE_LOW_EDGE_EXACT_CARVEOUT_ENABLED = True
        wt.MIN_EDGE = 0.25
        rb = {
            "bucket": (22, 22, "C"),
            "price": 0.35,
            "edge": 0.27,
        }

        allowed, reason = wt._core_low_edge_exact_carveout_allowed(rb, signal_strength="strong")

        self.assertFalse(allowed)
        self.assertEqual(reason, "edge=0.27>=min_edge")


if __name__ == "__main__":
    unittest.main()
