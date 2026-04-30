#!/usr/bin/env python3
"""Tests for AIFS stale-cache fallback and signal degradation."""

import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import aifs_forecast as af
import ensemble_forecast as ef


class _FakeDataset:
    def __init__(self, ts: float):
        self.variables = {"time": types.SimpleNamespace(data=ts)}


class TestAifsStaleFallback(unittest.TestCase):
    def test_uses_stale_cache_when_refresh_fails(self):
        fake_np = types.SimpleNamespace(mean=lambda vals: sum(vals) / len(vals), max=max, min=min)
        fake_cfgrib = object()
        stale_run = datetime.now(timezone.utc) - timedelta(hours=18)

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            cf_cache = cache_dir / "latest_cf.grib2"
            pf_cache = cache_dir / "latest_pf.grib2"
            cf_cache.write_bytes(b"cf")
            pf_cache.write_bytes(b"pf")
            stale_mtime = stale_run.timestamp()
            Path(cf_cache).touch()
            Path(pf_cache).touch()
            import os
            os.utime(cf_cache, (stale_mtime, stale_mtime))
            os.utime(pf_cache, (stale_mtime, stale_mtime))

            with patch.object(af, "AIFS_CACHE_DIR", cache_dir), \
                 patch.object(af, "_LAST_REFRESH_FAILURE_AT", 0.0), \
                 patch.object(af, "_load_aifs_dependencies", return_value={"Client": object(), "cfgrib": fake_cfgrib, "np": fake_np, "missing": []}), \
                 patch.object(af, "_open_grib_dataset", return_value=_FakeDataset(stale_run.timestamp())), \
                 patch.object(af, "_download_aifs_grib", side_effect=RuntimeError("503 Slow Down")), \
                 patch.object(af, "_extract_member_daily_values", side_effect=[[20.0], [19.0, 21.0]]):
                result = af.get_aifs_ens_forecast(
                    lat=1.0,
                    lon=2.0,
                    date_str="2026-05-01",
                    metric="high",
                    unit="F",
                    timezone_name="UTC",
                )

        self.assertEqual(result["source"], "aifs_ens")
        self.assertTrue(result["stale"])
        self.assertGreater(result["stale_age_hours"], 12)
        self.assertIn("503 Slow Down", result["refresh_error"])
        self.assertIsNotNone(result["ensemble_mean"])

    def test_stale_aifs_downgrades_signal_strength(self):
        future_date = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")

        def fake_model(city, date_str, metric, unit, model_name):
            vals = {
                "ecmwf_ifs025": 70.0,
                "gfs_seamless": 70.5,
                "meteofrance_seamless": 69.5,
            }
            return vals.get(model_name)

        with patch.object(ef, "_fetch_aifs_result", return_value={
            "ensemble_mean": 70.0,
            "stale": True,
            "stale_age_hours": 18.0,
            "refresh_error": "503 Slow Down",
            "run_date": "2026-04-30",
            "run_hour": 12,
        }), patch.object(ef, "_fetch_model_temp", side_effect=fake_model), patch.dict(ef.METAR_STATIONS, {}, clear=True):
            result = ef.get_ensemble_forecast("Dallas", future_date, metric="high", unit="F")

        self.assertTrue(result["aifs_stale"])
        self.assertEqual(result["signal_strength"], "moderate")
        self.assertEqual(result["aifs_refresh_error"], "503 Slow Down")


if __name__ == "__main__":
    unittest.main()
