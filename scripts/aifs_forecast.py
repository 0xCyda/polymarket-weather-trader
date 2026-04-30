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
_GRIB_DOWNLOAD_SOCKET_TIMEOUT_S = 300

# Ensures only one thread downloads the GRIB at a time. Without this, concurrent
# city forecast threads each detect a stale cache and race to write the same file.
_GRIB_DOWNLOAD_LOCK = threading.Lock()
_LAST_REFRESH_FAILURE_AT = 0.0


AIFS_CACHE_DIR = Path.home() / ".cache" / "aifs_ens"
AIFS_CACHE_MAX_AGE_HOURS = 12  # Legacy age gate kept for telemetry, not refresh policy.
AIFS_STALE_FALLBACK_MAX_AGE_HOURS = 24  # One full extra cycle max when upstream throttles
AIFS_REFRESH_RETRY_COOLDOWN_S = 1800    # Avoid hammering AWS after a refresh failure
AIFS_RUN_READY_DELAY_HOURS = 4          # Don't chase a new 00z/12z run until it's likely published
AIFS_HTTP_MAX_RETRIES = 4               # Override multiurl's default 500 HTTP retries
AIFS_HTTP_RETRY_AFTER = (2, 8, 2)       # 2s, 4s, 8s, then fail soft to stale cache
# Covers next-day midnight forecasts at steps 12-36
DEFAULT_STEPS = tuple(range(0, 73, 6))   # 0,6,12,18,24,30,36,42,48,54,60,66,72 (covers D+2 targets)
DEFAULT_RUN_HOURS = (0, 12)


def _clear_cfgrib_index_files(grib_path: str | Path) -> None:
    """Delete any sibling cfgrib .idx files for this GRIB path."""
    path = Path(grib_path)
    for idx_path in path.parent.glob(f"{path.name}*.idx"):
        try:
            idx_path.unlink()
        except FileNotFoundError:
            pass


def _open_grib_dataset(cfgrib_mod, grib_path: str | Path):
    """Open a GRIB without persisting cfgrib index files to disk."""
    _clear_cfgrib_index_files(grib_path)
    return cfgrib_mod.open_file(str(grib_path), indexpath="")


def _cache_age_hours(path: Path) -> float:
    return (datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600


def _get_cache_info(cfgrib_mod, path: Path, max_age_hours: float | None = None) -> dict | None:
    """Return readable cache metadata, or None if missing/corrupt/too old."""
    if not path.exists():
        return None
    age_hours = _cache_age_hours(path)
    if max_age_hours is not None and age_hours > max_age_hours:
        return None
    try:
        ds = _open_grib_dataset(cfgrib_mod, path)
        time_ts = float(ds.variables["time"].data)
        run_dt = datetime.fromtimestamp(time_ts, tz=timezone.utc)
        return {
            "path": str(path),
            "run_date": run_dt.strftime("%Y-%m-%d"),
            "run_hour": run_dt.hour,
            "age_hours": age_hours,
        }
    except Exception:
        return None


def _run_cache_prefix(run_date: str, run_hour: int) -> str:
    return f"{run_date}_{int(run_hour):02d}"


def _run_cache_paths(run_date: str, run_hour: int) -> tuple[Path, Path]:
    prefix = _run_cache_prefix(run_date, run_hour)
    return (
        AIFS_CACHE_DIR / f"{prefix}_cf.grib2",
        AIFS_CACHE_DIR / f"{prefix}_pf.grib2",
    )


def _latest_cache_paths() -> tuple[Path, Path]:
    return (
        AIFS_CACHE_DIR / "latest_cf.grib2",
        AIFS_CACHE_DIR / "latest_pf.grib2",
    )


def _latest_expected_run(now_utc: datetime | None = None) -> tuple[str, int]:
    """Return the newest 00z/12z run that should realistically be available."""
    now_utc = now_utc or datetime.now(timezone.utc)
    ready_cutoff = now_utc - timedelta(hours=AIFS_RUN_READY_DELAY_HOURS)
    rounded = ready_cutoff.replace(minute=0, second=0, microsecond=0)
    run_hour = max(h for h in DEFAULT_RUN_HOURS if h <= rounded.hour)
    run_time = rounded.replace(hour=run_hour)
    return run_time.strftime("%Y-%m-%d"), run_hour


def _copy_if_exists(src_path: str | Path | None, dst_path: Path) -> None:
    if src_path and Path(src_path).exists():
        shutil.copy2(src_path, dst_path)


def _replace_with_link(src_path: str | Path | None, dst_path: Path) -> None:
    """Point dst_path at src_path without duplicating bytes on disk."""
    if not src_path:
        return
    src = Path(src_path)
    if not src.exists():
        return
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if dst_path.exists() or dst_path.is_symlink():
            dst_path.unlink()
    except FileNotFoundError:
        pass
    try:
        os.link(src, dst_path)
        return
    except OSError:
        pass
    try:
        dst_path.symlink_to(src)
        return
    except OSError:
        pass
    # Last-resort fallback keeps behavior working even on odd filesystems.
    shutil.copy2(src, dst_path)


def _candidate_cached_runs(cfgrib_mod, max_age_hours: float | None = None,
                           now_utc: datetime | None = None) -> list[dict]:
    """Return cached runs newest-first, preferring explicit run-keyed files over latest aliases."""
    candidates: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for run_date, run_hour in _candidate_runs(now_utc=now_utc):
        cf_path, pf_path = _run_cache_paths(run_date, run_hour)
        info = _get_cache_info(cfgrib_mod, cf_path, max_age_hours=max_age_hours)
        if info and (info["run_date"], info["run_hour"]) not in seen:
            info["cf_path"] = str(cf_path)
            info["pf_path"] = str(pf_path) if pf_path.exists() else None
            candidates.append(info)
            seen.add((info["run_date"], info["run_hour"]))
    latest_cf, latest_pf = _latest_cache_paths()
    latest_info = _get_cache_info(cfgrib_mod, latest_cf, max_age_hours=max_age_hours)
    if latest_info and (latest_info["run_date"], latest_info["run_hour"]) not in seen:
        latest_info["cf_path"] = str(latest_cf)
        latest_info["pf_path"] = str(latest_pf) if latest_pf.exists() else None
        candidates.append(latest_info)
    return candidates


def _refresh_backoff_active() -> bool:
    return _LAST_REFRESH_FAILURE_AT > 0 and (time.time() - _LAST_REFRESH_FAILURE_AT) < AIFS_REFRESH_RETRY_COOLDOWN_S


def _mark_refresh_failure() -> None:
    global _LAST_REFRESH_FAILURE_AT
    _LAST_REFRESH_FAILURE_AT = time.time()


def _clear_refresh_failure() -> None:
    global _LAST_REFRESH_FAILURE_AT
    _LAST_REFRESH_FAILURE_AT = 0.0


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

    # AWS S3 gives the right file size, but ECMWF's multiurl downloader defaults
    # to 500 HTTP retries with 120s sleeps. That traps interactive scans forever.
    client = Client(source="aws")
    import ecmwf.opendata.client as ecmwf_client_mod
    from multiurl import download as multiurl_download

    original_download = ecmwf_client_mod.download
    original_robust = ecmwf_client_mod.robust

    def _bounded_download(urls, target, **kwargs):
        kwargs.setdefault("maximum_retries", AIFS_HTTP_MAX_RETRIES)
        kwargs.setdefault("retry_after", AIFS_HTTP_RETRY_AFTER)
        return multiurl_download(urls, target, **kwargs)

    def _bounded_robust(call, maximum_tries=500, retry_after=120, mirrors=None):
        # ECMWF's index fetch path calls `robust()` directly before the actual
        # GRIB download starts. If we only patch `download()`, a 503 on the
        # `.index` request still falls back to multiurl's 500 x 120s default
        # retry loop and the whole scan hangs for hours.
        return original_robust(
            call,
            maximum_tries=min(int(maximum_tries), AIFS_HTTP_MAX_RETRIES),
            retry_after=AIFS_HTTP_RETRY_AFTER,
            mirrors=mirrors,
        )

    ecmwf_client_mod.download = _bounded_download
    ecmwf_client_mod.robust = _bounded_robust

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

    try:
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
    finally:
        ecmwf_client_mod.download = original_download
        ecmwf_client_mod.robust = original_robust


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
        ds = _open_grib_dataset(cfgrib_mod, grib_path)
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
    # Persistent cache: keep per-run GRIBs and a latest alias for quick inspection.
    AIFS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    latest_cf_cache, latest_pf_cache = _latest_cache_paths()

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

    desired_run_date, desired_run_hour = (
        (run_date, int(run_hour))
        if run_date and run_hour is not None
        else _latest_expected_run()
    )
    desired_cf_cache, desired_pf_cache = _run_cache_paths(desired_run_date, desired_run_hour)

    # Acquire lock before checking cache — prevents concurrent threads from each
    # detecting a stale cache and racing to write the same GRIB file simultaneously.
    with _GRIB_DOWNLOAD_LOCK:
        desired_info = _get_cache_info(cfgrib_mod, desired_cf_cache)
        fallback_candidates = _candidate_cached_runs(
            cfgrib_mod,
            max_age_hours=AIFS_STALE_FALLBACK_MAX_AGE_HOURS,
        )
        stale_info = fallback_candidates[0] if fallback_candidates else None

        if desired_info and desired_info["run_date"] == desired_run_date and desired_info["run_hour"] == desired_run_hour:
            download = {
                "cf_path": str(desired_cf_cache),
                "pf_path": str(desired_pf_cache) if desired_pf_cache.exists() else None,
                "cached": True,
                "run_date": desired_info["run_date"],
                "run_hour": desired_info["run_hour"],
                "stale": False,
                "stale_age_hours": round(desired_info["age_hours"], 1),
                "refresh_error": None,
            }
        elif stale_info:
            download = {
                "cf_path": stale_info["cf_path"],
                "pf_path": stale_info.get("pf_path"),
                "cached": True,
                "run_date": stale_info["run_date"],
                "run_hour": stale_info["run_hour"],
                "stale": True,
                "stale_age_hours": round(stale_info["age_hours"], 1),
                "refresh_error": "cache-only mode: using newest readable cached run",
            }
        else:
            import tempfile as _tmp
            with _tmp.TemporaryDirectory(prefix="aifs_ens_") as tmpdir:
                cf_tmp = str(Path(tmpdir) / _run_cache_prefix(desired_run_date, desired_run_hour) ) + "_cf.grib2"
                try:
                    download = _download_aifs_grib(
                        target_path=cf_tmp,
                        run_date=desired_run_date,
                        run_hour=desired_run_hour,
                        steps=DEFAULT_STEPS,
                    )
                except Exception as exc:
                    return {
                        "ensemble_mean": None,
                        "spread": None,
                        "agreement_pct": 0.0,
                        "member_count": 0,
                        "run_date": desired_run_date,
                        "run_hour": desired_run_hour,
                        "source": "aifs_ens",
                        "error": str(exc),
                        "stale": False,
                        "stale_age_hours": None,
                        "refresh_error": str(exc),
                    }
                else:
                    _clear_refresh_failure()
                    download["stale"] = False
                    download["stale_age_hours"] = 0.0
                    download["refresh_error"] = None
                    _copy_if_exists(download.get("cf_path"), desired_cf_cache)
                    _copy_if_exists(download.get("pf_path"), desired_pf_cache)
                    _replace_with_link(desired_cf_cache, latest_cf_cache)
                    _replace_with_link(desired_pf_cache, latest_pf_cache)

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
            "stale": bool(download.get("stale")),
            "stale_age_hours": download.get("stale_age_hours"),
            "refresh_error": download.get("refresh_error"),
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
        "stale": bool(download.get("stale")),
        "stale_age_hours": download.get("stale_age_hours"),
        "refresh_error": download.get("refresh_error"),
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
    latest_cf_cache, latest_pf_cache = _latest_cache_paths()

    cfgrib_mod = deps["cfgrib"]
    desired_run_date, desired_run_hour = (
        (run_date, int(run_hour))
        if run_date and run_hour is not None
        else _latest_expected_run()
    )
    desired_cf_cache, desired_pf_cache = _run_cache_paths(desired_run_date, desired_run_hour)

    with _GRIB_DOWNLOAD_LOCK:
        desired_info = _get_cache_info(cfgrib_mod, desired_cf_cache)
        fallback_candidates = _candidate_cached_runs(
            cfgrib_mod,
            max_age_hours=AIFS_STALE_FALLBACK_MAX_AGE_HOURS,
        )
        if desired_info and desired_info["run_date"] == desired_run_date and desired_info["run_hour"] == desired_run_hour:
            return True
        if fallback_candidates and _refresh_backoff_active():
            return True
        import tempfile as _tmp
        with _tmp.TemporaryDirectory(prefix="aifs_prewarm_") as tmpdir:
            try:
                download = _download_aifs_grib(
                    target_path=str(Path(tmpdir) / (_run_cache_prefix(desired_run_date, desired_run_hour) + "_cf.grib2")),
                    run_date=desired_run_date,
                    run_hour=desired_run_hour,
                    steps=DEFAULT_STEPS,
                )
            except Exception:
                if fallback_candidates:
                    _mark_refresh_failure()
                    return True
                return False
            _clear_refresh_failure()
            _copy_if_exists(download.get("cf_path"), desired_cf_cache)
            _copy_if_exists(download.get("pf_path"), desired_pf_cache)
            _replace_with_link(desired_cf_cache, latest_cf_cache)
            _replace_with_link(desired_pf_cache, latest_pf_cache)
    return _get_cache_info(cfgrib_mod, desired_cf_cache) is not None


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
