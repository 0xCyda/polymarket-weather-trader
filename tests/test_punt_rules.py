#!/usr/bin/env python3
"""Targeted tests for punt entry guards."""

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


class TestPuntRules(unittest.TestCase):
    def test_find_punt_candidates_skips_sub_tick_prices(self):
        event_markets = [{
            "id": "m-subtick",
            "external_price_yes": 0.0005,
            "question": "Will the highest temperature in Dallas be 73°F or below on April 29?",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.0005", "0.9995"],
        }]

        out = wt.find_punt_candidates(
            event_markets=event_markets,
            forecast_temp=68.0,
            spread=2.0,
            core_match_id=None,
            already_held=set(),
            location="Dallas",
            date_str="2026-04-29",
            metric="high",
            is_international=False,
            signal_strength="strong",
            models_used=4,
            agreement_pct=100.0,
        )

        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
