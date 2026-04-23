#!/usr/bin/env python3
"""
AIFS ENS point forecast helpers.

Fetches AIFS ensemble GRIB via ECMWF open data (AWS S3), decodes with cfgrib,
and returns ensemble mean + spread for a single lat/lon/date.

Module:
  from scripts.aifs_forecast import get_aifs_ens_forecast
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

# Hard cap on socket-level I/O during GRIB downloads. The ecmwf-opendata/multiurl
# library has no per-request timeout; without this a stalled S3 connection hangs
# the process until the cron job's process-level timeout kills the whole scan.
_GRIB_DOWNLOAD_SOCKET_TIMEOUT_S = 120

# Ensures only one thread downloads the GRIB at a time. Without this, concurrent
# city forecast threads each detect a stale cache and race to write the same file.
_GRIB_DOWNLOAD_LOCK = threading.Lock()


AIFS_CACHE_DIR = Path.home() / ".cache" / "aifs_ens"
AIFS_CACHE_MAX_AGE_HOURS = 12  # Re-download every 12h to match AIFS ENS cycle (00z and 12z runs)
# Covers next-day midnight forecasts at steps 12-36
DEFAULT_STEPS = tuple(range(0, 73, 6))   # 0,6,12,18,24,30,36,42,48,54,60,66,72 (covers D+2 targets)
DEFAULT_RUN_HOURS = (0, 12)


def _load_aifs_dependencies():
    """Import ECMWF/GRIB deps lazily so the module remains importable."""
    missing = []

    try:
        from ecmwf.opendata import Client
    except ImportError:
        Client = None
        missing.append("ecmwf-opendata")

    try:
        import cfgrib
    except ImportError:
        cfgrib = None
        missing.append("cfgrib")

    try:
        import eccodes  # noqa: F401
    except ImportError:
        missing.append("eccodes")

    try:
        import numpy as np
    except ImportError:
        np = None
        missing.append("numpy")

    return {
        "Client": Client,
        "cfgrib": cfgrib,
        "np": np,
        "missing": missing,
    }


def get_dependency_status() -> dict:
    """Return whether AIFS runtime dependencies are available."""
    deps = _load_aifs_dependencies()
    return {
        "available": not deps["missing"],
        "missing": deps["missing"],
    }


def _candidate_runs(now_utc: datetime | None = None) -> list[tuple[str, int]]:
    """Return recent ECMWF run candidates newest-first."""
    now_utc = now_utc or datetime.now(timezone.utc)
    rounded = now_utc.replace(minute=0, second=0, microsecond=0)
    runs: list[tuple[str, int]] = []
    for back in range(0, 48, 6):
        candidate = rounded - timedelta(hours=back)
        run_hour = max(h for h in DEFAULT_RUN_HOURS if h <= candidate.hour)
        run_time = candidate.replace(hour=run_hour)
        run_date = run_time.strftime("%Y-%m-%d")
        pair = (run_date, run_hour)
        if pair not in runs:
            runs.append(pair)
    return runs


def _build_request_variants(target_path: str, steps: Iterable[int],
                            include_cf: bool = True,
                            include_pf: bool = True) -> list[dict]:
    """
    Build AIFS ENS request variants for AWS S3 source.

    Uses model='aifs-ens' (NOT 'aifs') and source='aws' for reliability.
    ECMWF portal is too slow and returns bloated full-ensemble files.
    """
    step_list = list(steps)
    variants = []
    if include_cf:
        # CF (control forecast): ~17MB from AWS S3 for 9 steps
        variants.append({
            "model": "aifs-ens",
            "stream": "enfo",
            "type": "cf",
            "param": ["2t"],
            "step": step_list,
            "target": target_path,
        })
    if include_pf:
        # 5 evenly-spaced members: 1,5,9,13,17 — ~7MB from AWS S3
        # Full 50-member spread is well-approximated by 5-member sample
        variants.append({
            "model": "aifs-ens",
            "stream": "enfo",
            "type": "pf",
            "number": [1, 5, 9, 13, 17],  # 5 members
            "param": ["2t"],
            "step": step_list,
            "target": target_path,
        })
    return variants


def _download_aifs_grib(target_path: str,
                        run_date: str | None = None,
                        run_hour: int | None = None,
                        steps: Iterable[int] = DEFAULT_STEPS) -> dict:
    """
    Download AIFS ENS GRIB files (CF + PF subset) to target_path.

    Uses AWS S3 exclusively — ECMWF portal serves 8x larger files.
    Returns metadata dict with paths and download info.
    """
    deps = _load_aifs_dependencies()
    Client = deps["Client"]
    if Client is None:
        raise RuntimeError(
            f"AIFS dependencies missing: {', '.join(deps['missing'])}"
        )

    # AWS S3 only — reliable, no rate limiting, right file size
    client = Client(source="aws")

    def _is_retryable_aifs_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(token in msg for token in ("slowdown", "503", "service unavailable", "timed out", "timeout", "connection", "reset", "broken pipe", "eof"))

    def _retrieve_with_backoff(date_str: str, hour: int, request: dict, path: str, min_bytes: int, label: str) -> tuple[bool, str | None]:
        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                if os.path.exists(path):
                    os.unlink(path)
                prev_timeout = socket.getdefaulttimeout()
                socket.setdefaulttimeout(_GRIB_DOWNLOAD_SOCKET_TIMEOUT_S)
                try:
                    client.retrieve(date=date_str, time=hour, **request)
                finally:
                    socket.setdefaulttimeout(prev_timeout)
                if os.path.exists(path) and os.path.getsize(path) > min_bytes:
                    return True, None
                last_err = f"{label}: file missing or too small after retrieve"
            except Exception as exc:
                last_err = f"{label}: {exc}"
                if attempt < max_attempts and _is_retryable_aifs_error(exc):
                    time.sleep(2 ** (attempt - 1))
                    continue
                return False, last_err
        return False, last_err

    attempts = (
        [(run_date, int(run_hour))]
        if run_date and run_hour is not None
        else _candidate_runs()
    )

    errors = []

    for date_str, hour in attempts:
        # Step 1: download CF (control forecast)
        cf_path = target_path.replace(".grib2", "_cf.grib2")
        cf_ok = False
        for request in _build_request_variants(cf_path, steps, include_cf=True, include_pf=False):
            cf_ok, cf_err = _retrieve_with_backoff(
                date_str=date_str,
                hour=hour,
                request=request,
                path=cf_path,
                min_bytes=1_000_000,
                label=f"{date_str} {hour:02d}Z CF",
            )
            if cf_ok:
                cf_meta = {k: v for k, v in request.items() if k != "target"}
                break
            errors.append(cf_err or f"{date_str} {hour:02d}Z CF: unknown error")
            cf_path = None
            break

        if not cf_ok:
            continue

        # Step 2: download PF subset (5 members)
        pf_path = target_path.replace(".grib2", "_pf.grib2")
        pf_ok = False
        for request in _build_request_variants(pf_path, steps, include_cf=False, include_pf=True):
            pf_ok, pf_err = _retrieve_with_backoff(
                date_str=date_str,
                hour=hour,
                request=request,
                path=pf_path,
                min_bytes=100_000,
                label=f"{date_str} {hour:02d}Z PF",
            )
            if pf_ok:
                pf_meta = {k: v for k, v in request.items() if k != "target"}
                break
            errors.append(pf_err or f"{date_str} {hour:02d}Z PF: unknown error")
            pf_path = None
            pf_meta = None
            break

        return {
            "cf_path": cf_path,
            "pf_path": pf_path if pf_ok else None,
            "run_date": date_str,
            "run_hour": hour,
            "cf_meta": cf_meta,
            "pf_meta": pf_meta if pf_ok else None,
            "source": "aws",
        }

    raise RuntimeError(
        "Unable to download AIFS ENS GRIB. Last errors: " + " | ".join(errors[-6:])
    )


def _normalize_longitude(lon: float) -> float:
    """Ensure longitude is 0-360 (ECMWF convention)."""
    return lon % 360 if lon < 0 else lon


def _extract_member_daily_values(grib_path: str, lat: float, lon: float,
                                 date_str: str, metric: str,
                                 timezone_name: str = "UTC") -> list[float]:
    """
    Extract daily high/low temperature values from a GRIB file at one lat/lon.

    cfgrib 0.9.x API: Dataset.variables is a dict of (dimension -> Variable).
    Variable.data is a raw numpy array indexed by dimension order.
    No xarray-style .sel() — use numpy indexing directly.
    """
    deps = _load_aifs_dependencies()
    cfgrib_mod = deps["cfgrib"]
    np = deps["np"]
    if cfgrib_mod is None or np is None:
        raise RuntimeError(
            f"AIFS dependencies missing: {', '.join(deps['missing'])}"
        )

    try:
        tzinfo = ZoneInfo(timezone_name)
    except Exception:
        tzinfo = timezone.utc

    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()

    try:
        # cfgrib creates a .idx index file keyed to GRIB content — stale idx from
        # a prior run causes FileNotFoundError when GRIB changes, so nuke it first
        idx_path = grib_path + ".idx"
        if os.path.exists(idx_path):
            os.unlink(idx_path)
        ds = cfgrib_mod.open_file(grib_path)
    except Exception as exc:
        raise RuntimeError(f"cfgrib failed to open {grib_path}: {exc}")

    # Navigate cfgrib Dataset.variables dict
    vars_ = ds.variables  # dict of variable_name -> Variable
    if "t2m" not in vars_ and "2t" not in vars_:
        raise RuntimeError(f"'t2m' not in variables: {list(vars_.keys())}")

    temp_var = vars_.get("t2m") or vars_.get("2t")
    temp_dims = temp_var.dimensions  # e.g. ('step', 'latitude', 'longitude')

    # Resolve coordinate arrays
    lat_arr = vars_["latitude"].data
    lon_arr = vars_["longitude"].data
    lon_360 = _normalize_longitude(lon)

    # Find nearest grid point indices
    lat_idx = int(np.abs(lat_arr - lat).argmin())
    lon_idx = int(np.abs(lon_arr - lon_360).argmin())

    # Get step array and valid_time array
    step_arr = vars_["step"].data  # hours since run time
    vt_arr = vars_["valid_time"].data  # Unix timestamps

    # Extract temp values at target lat/lon for each step
    dim_map = dict(zip(temp_dims, range(len(temp_dims))))
    step_dim_idx = dim_map.get("step", 0)
    lat_dim_idx = dim_map.get("latitude", 1)
    lon_dim_idx = dim_map.get("longitude", 2)

    reducer = max if metric == "high" else min

    # Shape check — verify indexing will work
    shape = temp_var.data.shape
    if (step_dim_idx >= len(shape) or lat_dim_idx >= len(shape)
            or lon_dim_idx >= len(shape)):
        raise RuntimeError(
            f"Dimension mismatch. dims={temp_dims} shape={shape}"
        )

    # Iterate steps and collect temps for target local date
    step_count = shape[step_dim_idx]
    daily_temps: list[float] = []

    for i in range(int(step_count)):
        # Build indexing tuple — use ellipsis for uninteresting dims
        idx = [slice(None)] * len(shape)
        idx[step_dim_idx] = i
        idx[lat_dim_idx] = lat_idx
        idx[lon_dim_idx] = lon_idx
        raw_kelvin = float(temp_var.data[tuple(idx)])

        # Skip NaN
        if raw_kelvin != raw_kelvin:
            continue

        # Decode valid_time Unix timestamp
        vt_ts = float(vt_arr[i])
        vt_utc = datetime.fromtimestamp(vt_ts, tz=timezone.utc)
        vt_local = vt_utc.astimezone(tzinfo)

        if vt_local.date() != target_date:
            continue

        celsius = raw_kelvin - 273.15
        daily_temps.append(celsius)

    if not daily_temps:
        raise RuntimeError(
            f"No AIFS values for {date_str} at lat={lat}, lon={lon} "
            f"(looked up grid: lat_idx={lat_idx} lon_idx={lon_idx})"
        )

    return [float(reducer(daily_temps))]


def _convert_unit(value_c: float, unit: str) -> float:
    if unit == "F":
        return (value_c * 9.0 / 5.0) + 32.0
    return value_c


def get_aifs_ens_forecast(lat: float, lon: float, date_str: str,
                          metric: str = "high", unit: str = "F",
                          timezone_name: str = "UTC",
                          run_date: str | None = None,
                          run_hour: int | None = None) -> dict:
    """
    Return AIFS ENS point summary for a target local date.

    Output:
      {
        "ensemble_mean": float|None,
        "spread": float|None,
        "agreement_pct": float,
        "member_count": int,
        "run_date": "YYYY-MM-DD"|None,
        "run_hour": int|None,
        "source": "aifs_ens",
        "error": str|None,
      }
    """
    deps = _load_aifs_dependencies()
    if deps["missing"]:
        return {
            "ensemble_mean": None,
            "spread": None,
            "agreement_pct": 0.0,
            "member_count": 0,
            "run_date": run_date,
            "run_hour": run_hour,
            "source": "aifs_ens",
            "error": f"Missing deps: {', '.join(deps['missing'])}",
        }

    np = deps["np"]
    # Persistent cache: one download per run_date+run_hour, reused across all cities/scans
    AIFS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_ts = f"latest"
    cf_cache = AIFS_CACHE_DIR / f"{cache_ts}_cf.grib2"
    pf_cache = AIFS_CACHE_DIR / f"{cache_ts}_pf.grib2"

    def _is_cache_fresh(path: Path) -> bool:
        if not path.exists():
            return False
        age_hours = (datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
        if age_hours >= AIFS_CACHE_MAX_AGE_HOURS:
            return False
        # Validate the GRIB file is actually readable by cfgrib (not a corrupt stub)
        try:
            cfgrib_mod.open_file(str(path))
            return True
        except Exception:
            return False

    cfgrib_mod = deps["cfgrib"]
    if cfgrib_mod is None:
        return {
            "ensemble_mean": None,
            "spread": None,
            "agreement_pct": 0.0,
            "member_count": 0,
            "run_date": run_date,
            "run_hour": run_hour,
            "source": "aifs_ens",
            "error": f"Missing deps: {', '.join(deps['missing'])}",
        }

    # Acquire lock before checking cache — prevents concurrent threads from each
    # detecting a stale cache and racing to write the same GRIB file simultaneously.
    with _GRIB_DOWNLOAD_LOCK:
        if _is_cache_fresh(cf_cache):
            ds = cfgrib_mod.open_file(str(cf_cache))
            time_ts = float(ds.variables["time"].data)
            run_dt = datetime.fromtimestamp(time_ts, tz=timezone.utc)
            run_date_str = run_dt.strftime("%Y-%m-%d")
            run_hour_int = run_dt.hour
            download = {
                "cf_path": str(cf_cache),
                "pf_path": str(pf_cache) if pf_cache.exists() else None,
                "cached": True,
                "run_date": run_date_str,
                "run_hour": run_hour_int,
            }
        else:
            import tempfile as _tmp
            with _tmp.TemporaryDirectory(prefix="aifs_ens_") as tmpdir:
                cf_tmp = str(Path(tmpdir) / "latest_cf.grib2")
                try:
                    download = _download_aifs_grib(
                        target_path=cf_tmp,
                        run_date=run_date,
                        run_hour=run_hour,
                        steps=DEFAULT_STEPS,
                    )
                except Exception as exc:
                    return {
                        "ensemble_mean": None,
                        "spread": None,
                        "agreement_pct": 0.0,
                        "member_count": 0,
                        "run_date": run_date,
                        "run_hour": run_hour,
                        "source": "aifs_ens",
                        "error": str(exc),
                    }
                cf_path = download.get("cf_path")
                if cf_path and Path(cf_path).exists():
                    shutil.copy2(cf_path, cf_cache)
                pf_path = download.get("pf_path")
                if pf_path and Path(pf_path).exists():
                    shutil.copy2(pf_path, pf_cache)

    member_values_c: list[float] = []

    # Extract from CF (control forecast, 1 member)
    try:
        cf_values = _extract_member_daily_values(
            grib_path=download["cf_path"],
            lat=lat, lon=lon, date_str=date_str,
            metric=metric, timezone_name=timezone_name,
        )
        member_values_c.extend(cf_values)
    except Exception:
        pass

    # Extract from PF (perturbed forecast members) if available
    pf_path = download.get("pf_path")
    if pf_path and Path(pf_path).exists():
        try:
            pf_values = _extract_member_daily_values(
                grib_path=pf_path,
                lat=lat, lon=lon, date_str=date_str,
                metric=metric, timezone_name=timezone_name,
            )
            member_values_c.extend(pf_values)
        except Exception:
            pass

    if not member_values_c:
        return {
            "ensemble_mean": None,
            "spread": None,
            "agreement_pct": 0.0,
            "member_count": 0,
            "run_date": download.get("run_date"),
            "run_hour": download.get("run_hour"),
            "source": "aifs_ens",
            "error": f"No AIFS values for {date_str} at lat={lat}, lon={lon}",
        }

    mean_value = float(np.mean(member_values_c))
    ensemble_mean = round(_convert_unit(mean_value, unit), 1)

    if len(member_values_c) > 1:
        spread = round(float(np.max(member_values_c) - np.min(member_values_c)) * (9.0 / 5.0 if unit == "F" else 1.0), 1)
        within_3c = sum(1 for v in member_values_c if abs(v - mean_value) <= (3 * 5.0 / 9.0 if unit == "F" else 3))
        agreement_pct = round(100.0 * within_3c / len(member_values_c), 1)
    else:
        spread = 0.0
        agreement_pct = 0.0

    return {
        "ensemble_mean": ensemble_mean,
        "spread": spread,
        "agreement_pct": agreement_pct,
        "member_count": len(member_values_c),
        "run_date": download.get("run_date"),
        "run_hour": download.get("run_hour"),
        "source": "aifs_ens",
        "error": None,
    }


def prewarm_grib_cache(run_date: str | None = None, run_hour: int | None = None) -> bool:
    """
    Download the GRIB files once, synchronously, before the scan loop starts.
    Returns True if cache is warm (fresh or just downloaded), False on failure.
    Call this from the main scan thread so city forecast threads always hit warm cache.
    """
    deps = _load_aifs_dependencies()
    if deps["missing"] or deps["cfgrib"] is None:
        return False

    AIFS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cf_cache = AIFS_CACHE_DIR / "latest_cf.grib2"
    pf_cache = AIFS_CACHE_DIR / "latest_pf.grib2"

    cfgrib_mod = deps["cfgrib"]

    def _is_cache_fresh(path: Path) -> bool:
        if not path.exists():
            return False
        age_hours = (datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
        if age_hours >= AIFS_CACHE_MAX_AGE_HOURS:
            return False
        try:
            cfgrib_mod.open_file(str(path))
            return True
        except Exception:
            return False

    with _GRIB_DOWNLOAD_LOCK:
        if _is_cache_fresh(cf_cache):
            return True
        import tempfile as _tmp
        with _tmp.TemporaryDirectory(prefix="aifs_prewarm_") as tmpdir:
            try:
                download = _download_aifs_grib(
                    target_path=str(Path(tmpdir) / "latest_cf.grib2"),
                    run_date=run_date,
                    run_hour=run_hour,
                    steps=DEFAULT_STEPS,
                )
            except Exception:
                return False
            cf_path = download.get("cf_path")
            if cf_path and Path(cf_path).exists():
                shutil.copy2(cf_path, cf_cache)
            pf_path = download.get("pf_path")
            if pf_path and Path(pf_path).exists():
                shutil.copy2(pf_path, pf_cache)
    return _is_cache_fresh(cf_cache)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch AIFS ENS point forecast")
    parser.add_argument("--lat", required=True, type=float)
    parser.add_argument("--lon", required=True, type=float)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--metric", default="high", choices=["high", "low"])
    parser.add_argument("--unit", default="F", choices=["F", "C"])
    parser.add_argument("--timezone", default="UTC")
    parser.add_argument("--run-date")
    parser.add_argument("--run-hour", type=int)
    args = parser.parse_args()

    result = get_aifs_ens_forecast(
        lat=args.lat,
        lon=args.lon,
        date_str=args.date,
        metric=args.metric,
        unit=args.unit,
        timezone_name=args.timezone,
        run_date=args.run_date,
        run_hour=args.run_hour,
    )
    print(json.dumps(result, indent=2))
