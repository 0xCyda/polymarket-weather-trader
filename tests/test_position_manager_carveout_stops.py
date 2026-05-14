#!/usr/bin/env python3
"""Targeted tests for carveout stop logic in position_manager."""

import os
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

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

import position_manager as pm


class TestPositionManagerCarveoutStops(unittest.TestCase):
    def setUp(self):
        self.now_utc = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        self.trade = {
            "trade_id": "carveout_1",
            "market_id": "market_1",
            "location": "Test City",
            "target_date": "2026-05-08",
            "side": "yes",
            "strategy": "carveout",
            "core_low_edge_exact_carveout": True,
            "question": "Will the highest temperature in Test City be 22°C on May 8?",
            "bucket": "22°C",
            "forecast_temp": 22.0,
            "entry_price": 0.30,
            "entered_at": "2026-05-08T10:00:00+00:00",
            "metric": "high",
        }
        self.market = {
            "question": self.trade["question"],
            "external_price_yes": 0.60,
        }

    def _common_patches(self):
        return [
            patch.object(pm, "LOCATIONS", {"Test City": (0.0, 0.0, "UTC")}),
            patch.object(pm, "STATIONS", {"Test City": {"id": "test-station"}}),
            patch.object(pm, "update_trade_atomically", lambda *args, **kwargs: None),
            patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}]),
            patch.object(pm, "_running_extreme", return_value=22.0),
            patch.object(pm, "_extreme_tracking_state", return_value=None),
            patch.object(pm, "_project_eod_max_c", return_value={"projected_c": 22.0, "confidence": 0.8, "mode": "steady"}),
        ]

    def test_carveout_trade_uses_trailing_stop_after_partial_tp(self):
        trade = {
            **self.trade,
            "shares": 250.0,
            "cost": 75.0,
            "partial_exits": [{"reason": "take_profit_1p9x_75pct", "shares": 750.0, "price": 0.61}],
            "realized_pnl": 232.5,
        }
        market = {**self.market, "external_price_yes": 0.60}
        patches = self._common_patches() + [
            patch.object(pm, "_peak_logged_price", return_value=0.90),
            patch.object(pm, "_last_logged_price", return_value=0.75),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
            out = pm._evaluate_position(trade, market, now_utc=self.now_utc, log=lambda *_: None)

        self.assertEqual(out["action"], "exit")
        self.assertIn("runner_trailing_stop", out["reason"])
        self.assertEqual(out["peak_seen_price"], 0.9)
        self.assertEqual(out["trail_floor_price"], 0.63)

    def test_carveout_trade_stops_runner_at_breakeven_after_partial_tp(self):
        trade = {
            **self.trade,
            "shares": 250.0,
            "cost": 75.0,
            "partial_exits": [{"reason": "take_profit_1p9x_75pct", "shares": 750.0, "price": 0.61}],
            "realized_pnl": 232.5,
        }
        market = {**self.market, "external_price_yes": 0.29}
        patches = self._common_patches() + [
            patch.object(pm, "_peak_logged_price", return_value=0.90),
            patch.object(pm, "_last_logged_price", return_value=0.31),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
            out = pm._evaluate_position(trade, market, now_utc=self.now_utc, log=lambda *_: None)

        self.assertEqual(out["action"], "exit")
        self.assertIn("runner_breakeven_stop_after_1p9x", out["reason"])
        self.assertEqual(out["runner_be_stop_price"], 0.3)

    def test_carveout_trade_uses_weak_price_guard(self):
        weak_market = {**self.market, "external_price_yes": 0.051}
        patches = self._common_patches() + [
            patch.object(pm, "_peak_logged_price", return_value=0.0),
            patch.object(pm, "_last_logged_price", return_value=0.10),
            patch.object(pm, "_running_extreme", return_value=20.0),
            patch.object(pm, "_project_eod_max_c", return_value={"projected_c": 20.5, "confidence": 0.8, "mode": "steady"}),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
            out = pm._evaluate_position(self.trade, weak_market, now_utc=self.now_utc, log=lambda *_: None)

        self.assertEqual(out["action"], "exit")
        self.assertIn("exact_core_weak_price_guard", out["reason"])

    def test_carveout_trade_ignores_generic_breakout_before_tp(self):
        breakout_market = {**self.market, "external_price_yes": 0.34}
        patches = self._common_patches() + [
            patch.object(pm, "_peak_logged_price", return_value=0.0),
            patch.object(pm, "_last_logged_price", return_value=0.36),
            patch.object(pm, "_running_extreme", return_value=24.0),
            patch.object(pm, "_project_eod_max_c", return_value={"projected_c": 24.5, "confidence": 0.9, "mode": "steady"}),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
            out = pm._evaluate_position(self.trade, breakout_market, now_utc=self.now_utc, log=lambda *_: None)

        self.assertEqual(out["action"], "hold")
        self.assertEqual(out["reason"], "hold_no_signal")


if __name__ == "__main__":
    unittest.main()
