#!/usr/bin/env python3
"""Targeted tests for paper_journal fallbacks and dedupe."""

import tempfile
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


class TestPaperTradeDedupe(unittest.TestCase):
    def test_same_market_and_strategy_never_logs_twice(self):
        with tempfile.TemporaryDirectory() as td:
            journal = Path(td) / "paper_trades.jsonl"
            lock = journal.with_suffix(".lock")
            with patch.object(pj, "JOURNAL_FILE", journal), patch.object(pj, "_LOCK_FILE", lock):
                first = pj.log_paper_trade(
                    market_id="m1",
                    question="Q1",
                    side="yes",
                    entry_price=0.12,
                    shares=100,
                    cost=12,
                    bucket="12°C",
                    forecast_temp=54,
                    signal_strength="strong",
                    location="Warsaw",
                    date_str="2026-04-29",
                    metric="high",
                    models_used=4,
                    agreement_pct=100,
                    spread=2.0,
                    strategy="punt",
                )
                trades = pj._load_trades()
                trades[0]["status"] = "resolved"
                pj._save_trades(trades)

                second = pj.log_paper_trade(
                    market_id="m1",
                    question="Q1",
                    side="yes",
                    entry_price=0.12,
                    shares=100,
                    cost=12,
                    bucket="12°C",
                    forecast_temp=54,
                    signal_strength="strong",
                    location="Warsaw",
                    date_str="2026-04-29",
                    metric="high",
                    models_used=4,
                    agreement_pct=100,
                    spread=2.0,
                    strategy="punt",
                )

                self.assertEqual(first, second)
                self.assertEqual(len(pj._load_trades()), 1)
                self.assertTrue(pj.has_logged_trade("m1", strategy="punt"))


class TestBackfillActualTemps(unittest.TestCase):
    def test_upgrades_open_meteo_actual_when_polymarket_resolves_later(self):
        with tempfile.TemporaryDirectory() as td:
            journal = Path(td) / "paper_trades.jsonl"
            lock = journal.with_suffix(".lock")
            trade = {
                "trade_id": "paper_sao",
                "status": "resolved",
                "resolution_source": "early_exit_position_manager",
                "audit_source": "open_meteo_archive",
                "location": "Sao Paulo",
                "target_date": "2026-05-04",
                "metric": "high",
                "actual_temp": 77.7,
            }
            with (
                patch.object(pj, "JOURNAL_FILE", journal),
                patch.object(pj, "_LOCK_FILE", lock),
                patch.object(pj, "_is_past_target_date_for_location", return_value=True),
                patch.object(pj, "fetch_historical_temp", return_value=80.6),
            ):
                pj._save_trades([trade])

                patched = pj.backfill_actual_temps()
                saved = pj._load_trades()

            self.assertEqual(len(patched), 1)
            self.assertEqual(saved[0]["actual_temp"], 80.6)
            self.assertEqual(saved[0]["audit_source"], "polymarket_cache")


if __name__ == "__main__":
    unittest.main()
