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


class TestCorpsePriceGuard(unittest.TestCase):
    @patch.object(pm, "city_tier", return_value="easy")
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=6.0)
    def test_easy_core_positions_exit_above_generic_corpse_floor(self, *_mocks):
        trade = {
            "trade_id": "warsaw-core",
            "market_id": "m-war",
            "location": "Warsaw",
            "target_date": "2026-04-29",
            "side": "yes",
            "strategy": "core",
            "entered_at": "2026-04-28T07:11:48+00:00",
            "question": "Will the highest temperature in Warsaw be 11°C on April 29?",
            "forecast_temp": 51.8,
            "metric": "high",
            "bucket": "11°C",
            "entry_price": 0.17,
        }
        market = {"id": "m-war", "external_price_yes": 0.055}
        now_utc = real_datetime(2026, 4, 29, 5, 19, 0, tzinfo=timezone.utc)  # 7:19 local, before generic 5¢ floor hit

        decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "exit")
        self.assertIn("corpse_price_guard", decision["reason"])
        self.assertIn("floor=$0.070", decision["reason"])

    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=25.0)
    def test_corpse_price_exits_before_peak_hour(self, *_mocks):
        trade = {
            "trade_id": "sao-paulo-core",
            "market_id": "m-sao",
            "location": "Sao Paulo",
            "target_date": "2026-04-28",
            "side": "yes",
            "strategy": "core",
            "entered_at": "2026-04-27T19:09:56+00:00",
            "question": "Will the highest temperature in Sao Paulo be 25°C or below on April 28?",
            "forecast_temp": 75.2,
            "metric": "high",
            "bucket": "25°C or below",
            "entry_price": 0.47,
        }
        market = {"id": "m-sao", "external_price_yes": 0.04}
        now_utc = real_datetime(2026, 4, 28, 15, 19, 0, tzinfo=timezone.utc)  # 12:19 local, before repricing guard start

        decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "exit")
        self.assertIn("corpse_price_guard", decision["reason"])
        self.assertLess(decision["local_hour"], pm.REPRICING_GUARD_START_HOUR)

    @patch.object(pm, "city_tier", return_value="easy")
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=25.0)
    def test_corpse_price_does_not_stop_positions_that_entered_cheap(self, *_mocks):
        trade = {
            "trade_id": "cheap-punt",
            "market_id": "m-punt",
            "location": "Sao Paulo",
            "target_date": "2026-04-28",
            "side": "yes",
            "strategy": "punt",
            "entered_at": "2026-04-27T19:09:56+00:00",
            "question": "Will the highest temperature in Sao Paulo be 25°C or below on April 28?",
            "forecast_temp": 75.2,
            "metric": "high",
            "bucket": "25°C or below",
            "entry_price": 0.06,
        }
        market = {"id": "m-punt", "external_price_yes": 0.04}
        now_utc = real_datetime(2026, 4, 28, 15, 19, 0, tzinfo=timezone.utc)

        decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["reason"], "hold_no_signal")

    @patch.object(pm, "_fetch_clob_price_yes", return_value=(0.005, "clob_midpoint"))
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=19.0)
    def test_clob_price_overrides_stale_simmer_for_corpse_exit(self, *_mocks):
        trade = {
            "trade_id": "ankara-core",
            "market_id": "m-ank",
            "location": "Ankara",
            "target_date": "2026-05-09",
            "side": "yes",
            "strategy": "core",
            "entered_at": "2026-05-07T20:45:13+00:00",
            "question": "Will the highest temperature in Ankara be 20°C on May 9?",
            "forecast_temp": 68.0,
            "metric": "high",
            "bucket": "20°C",
            "entry_price": 0.365,
            "polymarket_token_id": "yes-token",
        }
        market = {"id": "m-ank", "external_price_yes": 0.525}
        now_utc = real_datetime(2026, 5, 9, 10, 21, 0, tzinfo=timezone.utc)

        decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "exit")
        self.assertIn("corpse_price_guard", decision["reason"])
        self.assertEqual(decision["current_price"], 0.005)
        self.assertEqual(decision["current_price_source"], "clob_midpoint")


class TestExactCoreTrailingStop(unittest.TestCase):
    @patch.object(pm, "_peak_logged_price", return_value=0.445)
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=16.0)
    def test_exact_core_positions_exit_after_30pct_drop_from_peak(self, *_mocks):
        trade = {
            "trade_id": "paris-exact",
            "market_id": "m-paris",
            "location": "Paris",
            "target_date": "2026-05-04",
            "side": "yes",
            "strategy": "core",
            "entered_at": "2026-05-03T05:30:58+00:00",
            "question": "Will the highest temperature in Paris be 17°C on May 4?",
            "forecast_temp": 62.6,
            "metric": "high",
            "bucket": "17°C",
            "entry_price": 0.155,
        }
        market = {"id": "m-paris", "external_price_yes": 0.22}
        now_utc = real_datetime(2026, 5, 4, 9, 19, 0, tzinfo=timezone.utc)  # 11:19 local

        decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "exit")
        self.assertIn("exact_core_trailing_stop", decision["reason"])

    @patch.object(pm, "_peak_logged_price", return_value=0.29)
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=16.0)
    def test_exact_core_positions_do_not_exit_before_trailing_arm(self, *_mocks):
        trade = {
            "trade_id": "paris-exact",
            "market_id": "m-paris",
            "location": "Paris",
            "target_date": "2026-05-04",
            "side": "yes",
            "strategy": "core",
            "entered_at": "2026-05-03T05:30:58+00:00",
            "question": "Will the highest temperature in Paris be 17°C on May 4?",
            "forecast_temp": 62.6,
            "metric": "high",
            "bucket": "17°C",
            "entry_price": 0.155,
        }
        market = {"id": "m-paris", "external_price_yes": 0.16}
        now_utc = real_datetime(2026, 5, 4, 8, 19, 0, tzinfo=timezone.utc)

        decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["reason"], "hold_no_signal")


class TestExactCoreMarketCollapseGuard(unittest.TestCase):
    @patch.object(pm, "_peak_logged_price", return_value=0.455)
    @patch.object(pm, "_last_logged_price", return_value=0.425)
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=22.0)
    def test_exact_core_market_collapse_exits_even_when_weather_still_in_bucket(self, *_mocks):
        trade = {
            "trade_id": "ankara-exact",
            "market_id": "m-ankara",
            "location": "Ankara",
            "target_date": "2026-05-12",
            "side": "yes",
            "strategy": "core",
            "core_low_edge_exact_carveout": True,
            "entered_at": "2026-05-10T09:26:48+00:00",
            "question": "Will the highest temperature in Ankara be 22°C on May 12?",
            "forecast_temp": 71.6,
            "metric": "high",
            "bucket": "22°C",
            "entry_price": 0.35,
        }
        market = {"id": "m-ankara", "external_price_yes": 0.095}
        now_utc = real_datetime(2026, 5, 12, 10, 50, 0, tzinfo=timezone.utc)  # 13:50 local

        decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "exit")
        self.assertIn("exact_core_market_collapse_guard", decision["reason"])
        self.assertEqual(decision["current_price"], 0.095)

    @patch.object(pm, "_peak_logged_price", return_value=0.34)
    @patch.object(pm, "_last_logged_price", return_value=0.245)
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=28.0)
    def test_exact_core_market_collapse_exits_at_65pct_drawdown(self, *_mocks):
        trade = {
            "trade_id": "shenzhen-exact",
            "market_id": "m-shenzhen",
            "location": "Shenzhen",
            "target_date": "2026-05-13",
            "side": "yes",
            "strategy": "core",
            "core_low_edge_exact_carveout": True,
            "entered_at": "2026-05-12T21:48:11+00:00",
            "question": "Will the highest temperature in Shenzhen be 30°C on May 13?",
            "forecast_temp": 86.0,
            "metric": "high",
            "bucket": "30°C",
            "entry_price": 0.345,
        }
        market = {"id": "m-shenzhen", "external_price_yes": 0.11}
        now_utc = real_datetime(2026, 5, 13, 2, 30, 0, tzinfo=timezone.utc)  # 10:30 local

        decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "exit")
        self.assertIn("exact_core_market_collapse_guard", decision["reason"])
        self.assertIn("drawdown=67.6% >= 65.0%", decision["reason"])
        self.assertEqual(decision["current_price"], 0.11)


class TestExactCoreWeakPriceGuard(unittest.TestCase):
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=30.0)
    def test_exact_core_positions_exit_before_generic_corpse_floor_when_weather_lags(self, *_mocks):
        trade = {
            "trade_id": "singapore-exact",
            "market_id": "m-sg",
            "location": "Singapore",
            "target_date": "2026-05-04",
            "side": "yes",
            "strategy": "core",
            "entered_at": "2026-05-03T13:41:14+00:00",
            "question": "Will the highest temperature in Singapore be 33°C on May 4?",
            "forecast_temp": 91.4,
            "metric": "high",
            "bucket": "33°C",
            "entry_price": 0.175,
        }
        market = {"id": "m-sg", "external_price_yes": 0.075}
        now_utc = real_datetime(2026, 5, 4, 2, 19, 0, tzinfo=timezone.utc)  # 10:19 local

        decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "exit")
        self.assertIn("exact_core_weak_price_guard", decision["reason"])

    @patch.object(pm, "_peak_logged_price", return_value=0.425)
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=30.0)
    def test_range_bucket_does_not_trigger_exact_core_weak_price_guard(self, *_mocks):
        trade = {
            "trade_id": "range-not-exact",
            "market_id": "m-range",
            "location": "Singapore",
            "target_date": "2026-05-04",
            "side": "yes",
            "strategy": "core",
            "entered_at": "2026-05-03T13:41:14+00:00",
            "question": "Will the highest temperature in Singapore be 32-33°C on May 4?",
            "forecast_temp": 91.4,
            "metric": "high",
            "bucket": "32-33°C",
            "entry_price": 0.175,
        }
        market = {"id": "m-range", "external_price_yes": 0.075}
        now_utc = real_datetime(2026, 5, 4, 2, 19, 0, tzinfo=timezone.utc)

        decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["reason"], "hold_no_signal")


class TestRepricingGuard(unittest.TestCase):
    @patch.object(pm, "city_tier", return_value="easy")
    @patch.object(pm, "_last_logged_price", return_value=0.20)
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=10.0)
    def test_easy_exact_core_ignores_repricing_guard_before_tp(self, *_mocks):
        trade = {
            "trade_id": "warsaw-repricing",
            "market_id": "m-war-reprice",
            "location": "Warsaw",
            "target_date": "2026-04-29",
            "side": "yes",
            "strategy": "core",
            "entered_at": "2026-04-28T07:11:48+00:00",
            "question": "Will the highest temperature in Warsaw be 11°C on April 29?",
            "forecast_temp": 51.8,
            "metric": "high",
            "bucket": "11°C",
            "entry_price": 0.17,
        }
        market = {"id": "m-war-reprice", "external_price_yes": 0.09}
        now_utc = real_datetime(2026, 4, 29, 10, 19, 0, tzinfo=timezone.utc)  # 12:19 local in Warsaw

        decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["reason"], "hold_no_signal")


class TestTakeProfitRunner(unittest.TestCase):
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=21.0)
    def test_core_trade_triggers_partial_take_profit_at_1p9x(self, *_mocks):
        trade = {
            "trade_id": "tp-core-1",
            "market_id": "m-tp-1",
            "location": "Paris",
            "target_date": "2026-05-04",
            "side": "yes",
            "strategy": "core",
            "entered_at": "2026-05-03T05:30:58+00:00",
            "question": "Will the highest temperature in Paris be 17°C on May 4?",
            "forecast_temp": 62.6,
            "metric": "high",
            "bucket": "17°C",
            "entry_price": 0.20,
            "shares": 1000,
            "cost": 200.0,
        }
        market = {"id": "m-tp-1", "external_price_yes": 0.39}
        now_utc = real_datetime(2026, 5, 4, 9, 19, 0, tzinfo=timezone.utc)

        decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "exit")
        self.assertAlmostEqual(decision["partial_exit_frac"], 0.75)
        self.assertIn("take_profit_1p9x", decision["reason"])

    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=21.0)
    def test_carveout_trade_triggers_partial_take_profit_at_1p9x(self, *_mocks):
        trade = {
            "trade_id": "tp-carve-1",
            "market_id": "m-tp-carve-1",
            "location": "Paris",
            "target_date": "2026-05-04",
            "side": "yes",
            "strategy": "carveout",
            "core_low_edge_exact_carveout": True,
            "entered_at": "2026-05-03T05:30:58+00:00",
            "question": "Will the highest temperature in Paris be 17°C on May 4?",
            "forecast_temp": 62.6,
            "metric": "high",
            "bucket": "17°C",
            "entry_price": 0.20,
            "shares": 1000,
            "cost": 200.0,
        }
        market = {"id": "m-tp-carve-1", "external_price_yes": 0.39}
        now_utc = real_datetime(2026, 5, 4, 9, 19, 0, tzinfo=timezone.utc)

        decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "exit")
        self.assertAlmostEqual(decision["partial_exit_frac"], 0.75)
        self.assertIn("take_profit_1p9x", decision["reason"])

    @patch.object(pm, "_peak_logged_price", return_value=0.60)
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=21.0)
    def test_runner_remainder_uses_trailing_stop_after_partial_take_profit(self, *_mocks):
        trade = {
            "trade_id": "tp-core-2",
            "market_id": "m-tp-2",
            "location": "Paris",
            "target_date": "2026-05-04",
            "side": "yes",
            "strategy": "core",
            "entered_at": "2026-05-03T05:30:58+00:00",
            "question": "Will the highest temperature in Paris be 17°C on May 4?",
            "forecast_temp": 62.6,
            "metric": "high",
            "bucket": "17°C",
            "entry_price": 0.20,
            "shares": 250,
            "cost": 50.0,
            "partial_exits": [{"reason": "take_profit_1p9x_75pct", "shares": 750, "price": 0.39}],
            "realized_pnl": 142.5,
        }
        market = {"id": "m-tp-2", "external_price_yes": 0.41}
        now_utc = real_datetime(2026, 5, 4, 10, 19, 0, tzinfo=timezone.utc)

        decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "exit")
        self.assertIn("runner_trailing_stop", decision["reason"])
        self.assertEqual(decision["peak_seen_price"], 0.6)
        self.assertEqual(decision["trail_floor_price"], 0.42)

    @patch.object(pm, "log_loss")
    @patch.object(pm, "update_trade_atomically")
    def test_execute_take_profit_reduces_position_and_tracks_realized_pnl(self, mock_update, _mock_log_loss):
        trade = {
            "trade_id": "tp-core-3",
            "status": "open",
            "side": "yes",
            "entry_price": 0.20,
            "shares": 1000.0,
            "cost": 200.0,
        }

        def _run_mutator(_trade_id, mutator):
            target = dict(trade)
            return mutator(target)

        mock_update.side_effect = _run_mutator

        updated = pm._execute_take_profit("tp-core-3", current_price=0.39, reason="take_profit_1p9x")

        self.assertIsNotNone(updated)
        self.assertEqual(updated["status"], "open")
        self.assertAlmostEqual(updated["shares"], 250.0)
        self.assertAlmostEqual(updated["cost"], 50.0)
        self.assertAlmostEqual(updated["realized_pnl"], 142.5)
        self.assertEqual(len(updated["partial_exits"]), 1)
        self.assertAlmostEqual(updated["partial_exits"][0]["shares"], 750.0)
        self.assertAlmostEqual(updated["partial_exits"][0]["price"], 0.39)
        self.assertAlmostEqual(updated["partial_exits"][0]["cost"], 150.0)
        self.assertAlmostEqual(updated["partial_exits"][0]["entry_price"], 0.20)

    @patch.object(pm, "log_loss")
    @patch.object(pm, "update_trade_atomically")
    def test_execute_exit_includes_prior_partial_realized_pnl(self, mock_update, _mock_log_loss):
        trade = {
            "trade_id": "tp-core-4",
            "status": "open",
            "side": "yes",
            "entry_price": 0.20,
            "shares": 250.0,
            "cost": 50.0,
            "realized_pnl": 142.5,
            "partial_exits": [{"reason": "take_profit_1p9x_75pct", "shares": 750, "price": 0.39}],
        }

        def _run_mutator(_trade_id, mutator):
            target = dict(trade)
            return mutator(target)

        mock_update.side_effect = _run_mutator

        updated = pm._execute_exit("tp-core-4", current_price=0.30, reason="runner_trailing_stop")

        self.assertIsNotNone(updated)
        self.assertEqual(updated["status"], "resolved")
        self.assertAlmostEqual(updated["pnl"], 167.5)
        self.assertEqual(updated["outcome"], "yes")


class TestExactCorePreTpGenericExits(unittest.TestCase):
    @patch.object(pm, "_project_eod_max_c", return_value={"projected_c": 20.5, "confidence": 0.95})
    @patch.object(pm, "_extreme_tracking_state", return_value={"locked": True})
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=19.0)
    def test_exact_core_holds_pre_tp_even_if_projection_and_running_look_bad(self, *_mocks):
        trade = {
            "trade_id": "pre-tp-core-1",
            "market_id": "m-pre-tp-1",
            "location": "Paris",
            "target_date": "2026-05-04",
            "side": "yes",
            "strategy": "core",
            "entered_at": "2026-05-04T02:30:58+00:00",
            "question": "Will the highest temperature in Paris be 22°C on May 4?",
            "forecast_temp": 71.6,
            "metric": "high",
            "bucket": "22°C",
            "entry_price": 0.30,
            "shares": 1000,
            "cost": 300.0,
        }
        market = {"id": "m-pre-tp-1", "external_price_yes": 0.32}
        now_utc = real_datetime(2026, 5, 4, 12, 19, 0, tzinfo=timezone.utc)

        decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["reason"], "hold_no_signal")


class TestPositionManagerAdds(unittest.TestCase):
    def _locked_in_trade(self, entry_price=0.30):
        return {
            "trade_id": "london-add",
            "market_id": "m-london-add",
            "location": "London",
            "target_date": "2026-04-27",
            "side": "yes",
            "strategy": "core",
            "entered_at": "2026-04-26T12:00:00+00:00",
            "question": "Will the highest temperature in London be 21°C on April 27?",
            "forecast_temp": 69.8,
            "metric": "high",
            "bucket": "21°C",
            "entry_price": entry_price,
            "shares": 1000,
            "cost": entry_price * 1000,
        }

    @patch.object(pm, "_project_eod_max_c", return_value={"projected_c": 21.0, "confidence": 0.9})
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=21.0)
    def test_adds_are_disabled_by_default(self, *_mocks):
        trade = self._locked_in_trade(entry_price=0.30)
        market = {"id": "m-london-add", "external_price_yes": 0.45}
        now_utc = real_datetime(2026, 4, 27, 14, 33, 0, tzinfo=timezone.utc)

        with patch.object(pm, "ADDS_ENABLED", False):
            decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["reason"], "add_blocked_position_adds_disabled")

    @patch.object(pm, "_project_eod_max_c", return_value={"projected_c": 21.0, "confidence": 0.9})
    @patch.object(pm, "_fetch_twc_intraday", return_value=[{"dummy": True}])
    @patch.object(pm, "_running_extreme", return_value=21.0)
    def test_enabled_adds_still_block_losing_positions(self, *_mocks):
        trade = self._locked_in_trade(entry_price=0.30)
        market = {"id": "m-london-add", "external_price_yes": 0.25}
        now_utc = real_datetime(2026, 4, 27, 14, 33, 0, tzinfo=timezone.utc)

        with patch.object(pm, "ADDS_ENABLED", True):
            decision = pm._evaluate_position(trade, market=market, now_utc=now_utc)

        self.assertEqual(decision["action"], "hold")
        self.assertIn("add_blocked_losing_position", decision["reason"])

    @patch.object(pm, "_live_price_yes", return_value=(0.25, "test"))
    def test_execute_add_refuses_losing_positions(self, *_mocks):
        trade = self._locked_in_trade(entry_price=0.30)
        updated = pm._execute_add(trade, market={}, size_usd=100.0, reason="test")
        self.assertIsNone(updated)


class TestLateCooldown(unittest.TestCase):
    @patch.object(pm, "datetime", FakeDateTime)
    @patch.object(pm, "_project_eod_max_c", return_value={"projected_c": 23.0, "confidence": 0.9})
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
    @patch.object(pm, "_project_eod_max_c", return_value={"projected_c": 23.0, "confidence": 0.9})
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
