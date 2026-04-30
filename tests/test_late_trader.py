#!/usr/bin/env python3
"""Tests for LATE entry guards around projected bucket drift."""

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

import late_trader as lt


class BeijingLateDateTime(real_datetime):
    @classmethod
    def now(cls, tz=None):
        dt = real_datetime(2026, 4, 30, 7, 5, 0, tzinfo=timezone.utc)
        if tz is None:
            return dt.replace(tzinfo=None)
        return dt.astimezone(tz)


class TestLateProjectedGuard(unittest.TestCase):
    @patch.object(lt, "datetime", BeijingLateDateTime)
    @patch("paper_journal.get_stats", return_value={"total_pnl": 0.0})
    @patch("paper_journal.get_open_positions", return_value=[])
    @patch.object(lt, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(lt, "_running_extreme", return_value=27.0)
    @patch.object(lt, "parse_market_bucket", return_value=((27, 27, "C"), None))
    @patch.object(lt, "parse_weather_event", return_value={"location": "Beijing", "date": "2026-04-30", "metric": "high"})
    def test_skips_exact_bucket_when_projection_leaves_bucket(self, *_mocks):
        result = lt._scan_city(
            "Beijing",
            dry_run=True,
            markets=[{
                "id": "m-beijing-27",
                "question": "Will the highest temperature in Beijing be 27°C on April 30?",
                "external_price_yes": 0.2365,
            }],
            log=lambda *_args, **_kwargs: None,
            late_state={},
        )

        self.assertEqual(result["status"], "skip")
        self.assertIn("projected_outside_bucket", result["reason"])
        self.assertEqual(result["projected_c"], 27.5)

    @patch.object(lt, "datetime", BeijingLateDateTime)
    @patch("paper_journal.get_stats", return_value={"total_pnl": 0.0})
    @patch("paper_journal.get_open_positions", return_value=[])
    @patch.object(lt, "log_paper_trade")
    @patch.object(lt, "execute_trade", return_value={"success": True, "shares_bought": 200, "simulated": True})
    @patch.object(lt, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(lt, "_running_extreme", return_value=27.0)
    @patch.object(lt, "parse_market_bucket", return_value=((27, 28, "C"), None))
    @patch.object(lt, "parse_weather_event", return_value={"location": "Beijing", "date": "2026-04-30", "metric": "high"})
    def test_allows_bucket_when_projection_stays_inside(self, *_mocks):
        result = lt._scan_city(
            "Beijing",
            dry_run=True,
            markets=[{
                "id": "m-beijing-range",
                "question": "Will the highest temperature in Beijing be 27-28°C on April 30?",
                "external_price_yes": 0.2365,
                "polymarket_token_id": "yes-token",
                "polymarket_no_token_id": "no-token",
            }],
            log=lambda *_args, **_kwargs: None,
            late_state={},
        )

        self.assertEqual(result["status"], "buy")
        self.assertEqual(result["projected_c"], 27.5)
        self.assertEqual(result["bucket"], "27-28°C")


if __name__ == "__main__":
    unittest.main()
