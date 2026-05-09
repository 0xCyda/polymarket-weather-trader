#!/usr/bin/env python3

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
