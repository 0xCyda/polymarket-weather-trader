#!/usr/bin/env python3

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import dashboard


class TestDashboardMarks(unittest.TestCase):
    @patch.object(dashboard, "_fetch_live_mark", return_value=(0.245, "clob_midpoint"))
    def test_enrich_positions_records_live_mark_source(self, _mock_fetch):
        positions = [{
            "market_id": "abc123",
            "entry_price": 0.20,
            "shares": 100,
            "side": "yes",
        }]

        enriched = dashboard._enrich_positions(positions)

        self.assertEqual(len(enriched), 1)
        row = enriched[0]
        self.assertEqual(row["current_price"], 0.245)
        self.assertEqual(row["upnl"], 4.5)
        self.assertEqual(row["price_source"], "clob_midpoint")
        self.assertEqual(row["mark_status"], "live")
        self.assertEqual(row["price_error"], "")

    @patch.object(dashboard, "_fetch_live_mark", return_value=(None, None))
    def test_enrich_positions_marks_missing_price_instead_of_fake_zero(self, _mock_fetch):
        positions = [{
            "market_id": "abc123",
            "entry_price": 0.20,
            "shares": 100,
            "side": "yes",
        }]

        enriched = dashboard._enrich_positions(positions)

        self.assertEqual(len(enriched), 1)
        row = enriched[0]
        self.assertIsNone(row["current_price"])
        self.assertIsNone(row["upnl"])
        self.assertIsNone(row["price_source"])
        self.assertEqual(row["mark_status"], "missing")
        self.assertEqual(row["price_error"], "live_price_unavailable")

    @patch.object(dashboard, "_load_trades_jsonl", return_value=[
        {"status": "resolved", "pnl": 12.34},
        {"status": "resolved", "pnl": -2.34},
        {"status": "open", "pnl": 999, "realized_pnl": 8.0},
    ])
    def test_portfolio_stats_counts_missing_marks(self, _mock_load):
        stats = dashboard._get_portfolio_stats([
            {"upnl": 10.0},
            {"upnl": None},
            {"upnl": 0.0},
        ])

        self.assertEqual(stats["realized_pnl"], 18.0)
        self.assertEqual(stats["unrealized_pnl"], 10.0)
        self.assertEqual(stats["marked_positions"], 2)
        self.assertEqual(stats["missing_marks"], 1)

    @patch.object(dashboard, "datetime")
    @patch.object(dashboard, "_load_trades_jsonl", return_value=[
        {
            "status": "resolved",
            "pnl": 12.0,
            "resolved_at": "2026-05-09T01:00:00+00:00",
        },
        {
            "status": "open",
            "realized_pnl": 8.5,
            "partial_exits": [
                {"ts": "2026-05-09T05:50:20+00:00", "pnl": 8.5, "shares": 75.0, "price": 0.39, "reason": "take_profit_1p9x_75pct"}
            ],
        },
    ])
    def test_get_stats_counts_partial_take_profit_in_total_and_today_pnl(self, _mock_load, mock_datetime):
        mock_datetime.now.return_value = datetime(2026, 5, 9, 15, 0, tzinfo=ZoneInfo("Australia/Perth"))
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
        mock_datetime.strptime.side_effect = datetime.strptime

        stats = dashboard._get_stats()

        self.assertEqual(stats["resolved_trades"], 1)
        self.assertEqual(stats["today_trades"], 2)
        self.assertEqual(stats["today_pnl"], 20.5)
        self.assertEqual(stats["total_pnl"], 20.5)

    @patch.object(dashboard, "_load_trades_jsonl")
    @patch.object(dashboard, "_get_simmer_positions", return_value=None)
    @patch.object(dashboard, "_enrich_positions", return_value=[])
    @patch.object(dashboard, "_get_portfolio_stats", return_value={})
    @patch.object(dashboard, "_get_stats", return_value={})
    @patch.object(dashboard, "_build_timeseries", return_value=[])
    @patch.object(dashboard, "_parse_signals_from_history", return_value=[])
    @patch.object(dashboard, "_get_last_scan_time", return_value=None)
    @patch.object(dashboard, "_get_config", return_value={})
    def test_api_state_merges_partial_take_profit_back_into_resolved_history(
        self,
        _mock_config,
        _mock_last_scan,
        _mock_signals,
        _mock_timeseries,
        _mock_stats,
        _mock_portfolio,
        _mock_enrich,
        _mock_simmer,
        mock_load,
    ):
        mock_load.return_value = [{
            "status": "resolved",
            "location": "Paris",
            "side": "yes",
            "entry_price": 0.20,
            "exit_price": 0.30,
            "shares": 100.0,
            "cost": 20.0,
            "pnl": 90.0,
            "target_date": "2026-05-04",
            "strategy": "core",
            "metric": "high",
            "partial_exits": [
                {"shares": 400.0, "price": 0.39, "reason": "take_profit_1p9x_75pct"}
            ],
        }]

        resp = dashboard.api_state()
        payload = json.loads(resp.body)
        row = payload["resolved"][0]

        self.assertEqual(row["shares"], 500.0)
        self.assertEqual(row["cost"], 100.0)
        self.assertEqual(row["pnl"], 90.0)
        self.assertEqual(row["exit_price"], 0.372)

    @patch.object(dashboard, "_load_trades_jsonl")
    @patch.object(dashboard, "_get_simmer_positions", return_value=None)
    @patch.object(dashboard, "_enrich_positions", return_value=[])
    @patch.object(dashboard, "_get_portfolio_stats", return_value={})
    @patch.object(dashboard, "_get_stats", return_value={})
    @patch.object(dashboard, "_build_timeseries", return_value=[])
    @patch.object(dashboard, "_parse_signals_from_history", return_value=[])
    @patch.object(dashboard, "_get_last_scan_time", return_value=None)
    @patch.object(dashboard, "_get_config", return_value={})
    def test_api_state_surfaces_open_partial_take_profit_in_resolved_history(
        self,
        _mock_config,
        _mock_last_scan,
        _mock_signals,
        _mock_timeseries,
        _mock_stats,
        _mock_portfolio,
        _mock_enrich,
        _mock_simmer,
        mock_load,
    ):
        mock_load.return_value = [{
            "status": "open",
            "location": "Tokyo",
            "question": "Will the highest temperature in Tokyo be 23°C on May 9?",
            "side": "yes",
            "entry_price": 0.375,
            "shares": 64.935065,
            "target_date": "2026-05-09",
            "strategy": "core",
            "metric": "high",
            "partial_exits": [{
                "ts": "2026-05-09T05:50:20+00:00",
                "shares": 194.805195,
                "price": 0.885,
                "pnl": 99.3506,
                "reason": "take_profit_1p9x_75pct (price=$0.885 >= trigger=$0.712, sell_frac=75%)",
                "fraction": 0.75,
            }],
        }]

        resp = dashboard.api_state()
        payload = json.loads(resp.body)
        row = payload["resolved"][0]

        self.assertEqual(row["location"], "Tokyo")
        self.assertEqual(row["shares"], 194.805195)
        self.assertEqual(row["cost"], 73.0519)
        self.assertEqual(row["exit_price"], 0.885)
        self.assertEqual(row["pnl"], 99.3506)
        self.assertEqual(row["resolved_at"], "2026-05-09T05:50:20+00:00")
        self.assertEqual(row["resolution_source"], "partial_take_profit")
        self.assertEqual(row["exit_reason"], "take_profit_1p9x_75pct (price=$0.885 >= trigger=$0.712, sell_frac=75%)")

    @patch.object(dashboard, "_load_trades_jsonl")
    @patch.object(dashboard, "_get_simmer_positions", return_value=None)
    @patch.object(dashboard, "_enrich_positions", return_value=[])
    @patch.object(dashboard, "_get_portfolio_stats", return_value={})
    @patch.object(dashboard, "_get_stats", return_value={})
    @patch.object(dashboard, "_build_timeseries", return_value=[])
    @patch.object(dashboard, "_parse_signals_from_history", return_value=[])
    @patch.object(dashboard, "_get_last_scan_time", return_value=None)
    @patch.object(dashboard, "_get_config", return_value={})
    def test_api_state_uses_weighted_average_exit_for_resolved_partial_exits(
        self,
        _mock_config,
        _mock_last_scan,
        _mock_signals,
        _mock_timeseries,
        _mock_stats,
        _mock_portfolio,
        _mock_enrich,
        _mock_simmer,
        mock_load,
    ):
        mock_load.return_value = [{
            "status": "resolved",
            "location": "Tel Aviv",
            "side": "yes",
            "entry_price": 0.30,
            "exit_price": 0.28,
            "shares": 250.0,
            "cost": 75.0,
            "pnl": 77.5,
            "target_date": "2026-05-10",
            "strategy": "core",
            "metric": "high",
            "partial_exits": [
                {"shares": 750.0, "price": 0.61, "reason": "take_profit_1p9x_75pct"}
            ],
        }]

        resp = dashboard.api_state()
        payload = json.loads(resp.body)
        row = payload["resolved"][0]

        self.assertEqual(row["shares"], 1000.0)
        self.assertEqual(row["cost"], 300.0)
        self.assertEqual(row["pnl"], 77.5)
        self.assertEqual(row["exit_price"], 0.5275)

    @patch.object(dashboard, "_load_trades_jsonl")
    @patch.object(dashboard, "datetime")
    def test_parse_signals_filters_past_dates_from_dashboard(self, mock_datetime, mock_load):
        mock_datetime.now.return_value = datetime(2026, 4, 30, 8, 0, tzinfo=ZoneInfo("Australia/Perth"))
        mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
        mock_datetime.strptime.side_effect = datetime.strptime

        mock_load.side_effect = [[
            {
                "logged_at": "2026-04-30T00:00:00+00:00",
                "location": "Munich",
                "target_date": "2026-04-29",
                "metric": "high",
                "forecast_temp": 19,
                "signal_strength": "strong",
                "models_used": 4,
                "agreement_pct": 75,
                "spread": 5.6,
            },
            {
                "logged_at": "2026-04-30T00:01:00+00:00",
                "location": "Paris",
                "target_date": "2026-04-30",
                "metric": "high",
                "forecast_temp": 22,
                "signal_strength": "moderate",
                "models_used": 4,
                "agreement_pct": 75,
                "spread": 5.4,
            },
        ], []]

        signals = dashboard._parse_signals_from_history()

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["location"], "Paris")
        self.assertEqual(signals[0]["date"], "2026-04-30")


if __name__ == "__main__":
    unittest.main()
