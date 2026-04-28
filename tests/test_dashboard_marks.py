#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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
        {"status": "open", "pnl": 999},
    ])
    def test_portfolio_stats_counts_missing_marks(self, _mock_load):
        stats = dashboard._get_portfolio_stats([
            {"upnl": 10.0},
            {"upnl": None},
            {"upnl": 0.0},
        ])

        self.assertEqual(stats["realized_pnl"], 10.0)
        self.assertEqual(stats["unrealized_pnl"], 10.0)
        self.assertEqual(stats["marked_positions"], 2)
        self.assertEqual(stats["missing_marks"], 1)


if __name__ == "__main__":
    unittest.main()
