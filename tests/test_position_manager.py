#!/usr/bin/env python3
"""Targeted tests for position_manager late-entry cooldown behavior."""

import os
import sys
import types
import unittest
from datetime import datetime as real_datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

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

import position_manager as pm


class FakeDateTime(real_datetime):
    @classmethod
    def now(cls, tz=None):
        dt = real_datetime(2026, 4, 27, 14, 33, 0, tzinfo=timezone.utc)
        if tz is None:
            return dt.replace(tzinfo=None)
        return dt.astimezone(tz)


class TestLateCooldown(unittest.TestCase):
    @patch.object(pm, "datetime", FakeDateTime)
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=21.0)
    def test_fresh_late_trade_is_not_immediately_auto_exited(self, *_mocks):
        trade = {
            "trade_id": "t1",
            "market_id": "m1",
            "location": "London",
            "target_date": "2026-04-27",
            "side": "yes",
            "strategy": "late",
            "entered_at": "2026-04-27T14:32:15+00:00",
            "question": "Will the highest temperature in London be 21°C on April 27?",
            "forecast_temp": 69.8,
            "metric": "high",
            "bucket": "21°C",
        }

        decision = pm._evaluate_position(trade, market=None)

        self.assertEqual(decision["action"], "hold")
        self.assertIn("late_cooldown", decision["reason"])
        self.assertLess(decision["age_min"], pm.LATE_PROJECTED_EXIT_COOLDOWN_MIN)

    @patch.object(pm, "datetime", FakeDateTime)
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=21.0)
    def test_older_late_trade_can_still_auto_exit(self, *_mocks):
        trade = {
            "trade_id": "t2",
            "market_id": "m2",
            "location": "London",
            "target_date": "2026-04-27",
            "side": "yes",
            "strategy": "late",
            "entered_at": "2026-04-27T13:00:00+00:00",
            "question": "Will the highest temperature in London be 21°C on April 27?",
            "forecast_temp": 69.8,
            "metric": "high",
            "bucket": "21°C",
        }

        decision = pm._evaluate_position(trade, market=None)

        self.assertEqual(decision["action"], "exit")
        self.assertIn("projected_outside_bucket", decision["reason"])


if __name__ == "__main__":
    unittest.main()
