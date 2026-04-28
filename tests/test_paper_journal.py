#!/usr/bin/env python3
"""Targeted tests for paper_journal fallbacks."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import paper_journal as pj


class TestHistoricalFallbackSettlement(unittest.TestCase):
    @patch.object(pj, "_fetch_archive_actual_temp", return_value=66.9)
    @patch.object(pj, "_fetch_actual_temp_via_polymarket_cli", return_value=None)
    @patch.object(pj, "fetch_historical_temp", return_value=None)
    def test_uses_archive_when_polymarket_sources_are_empty(self, *_mocks):
        trade = {
            "location": "Chicago",
            "target_date": "2026-04-27",
            "metric": "high",
            "bucket": "62-63°F",
        }

        result = pj._historical_fallback_settlement(trade, force=True)

        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "open_meteo_archive")
        self.assertEqual(result["actual_temp"], 66.9)
        self.assertEqual(result["outcome"], "no")
        self.assertEqual(result["exit_price"], 0.0)


if __name__ == "__main__":
    unittest.main()
