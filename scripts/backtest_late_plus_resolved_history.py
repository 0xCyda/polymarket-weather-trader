#!/usr/bin/env python3
"""Replay resolved CORE history for hypothetical LATE+ add-ons.

For each resolved CORE city/date where a CORE position was still open at the
local LATE entry window, reconstruct the observed bucket from TWC intraday data
and simulate LATE+ P&L across assumed entry prices. Historical LATE market
prices were not stored, so price is a sensitivity input, not a recovered fact.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from late_trader import (
    LATE_EDGE_BUFFER_C,
    LATE_ENTRY_HOUR,
    LATE_MAX_POSITION,
    LOCATIONS,
    STATIONS,
    _bucket_contains,
    _bucket_label,
    _edge_distance_c,
    _estimate_late_probability,
    _fetch_twc_intraday,
    _project_late_eod_extreme_c,
    _size_late_trade,
)
from weather_trader import parse_temperature_bucket


BASE = _HERE.parent
TRADES_FILE = BASE / "data" / "paper_trades.jsonl"
REPORT_DIR = BASE / "reports" / "audits"


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _unit_from_trade(trade: dict, city: str) -> str:
    for raw in (trade.get("question"), trade.get("bucket")):
        bucket = parse_temperature_bucket(str(raw or ""))
        if bucket:
            return bucket[2]
    return "F" if city in {
        "NYC", "Chicago", "Seattle", "Atlanta", "Dallas", "Miami", "Houston",
        "San Francisco", "Phoenix", "Los Angeles", "Denver", "Austin", "Las Vegas",
    } else "C"


def _fahrenheit_bucket_template(trades: list[dict], city: str, date_str: str) -> tuple[int, int] | None:
    for trade in trades:
        if trade.get("location") != city or trade.get("target_date") != date_str:
            continue
        for raw in (trade.get("question"), trade.get("bucket")):
            bucket = parse_temperature_bucket(str(raw or ""))
            if not bucket or bucket[2] != "F":
                continue
            lo, hi, _unit = bucket
            if lo in (-999, 999) or hi in (-999, 999):
                continue
            return int(lo), int(hi)
    return None


def _infer_bucket(temp_c: float, unit: str, template: tuple[int, int] | None) -> tuple[int, int, str]:
    if unit == "C":
        n = int(math.floor(temp_c + 0.5))
        return (n, n, "C")
    temp_f = temp_c * 9 / 5 + 32
    if template:
        lo, hi = template
        span = max(1, hi - lo + 1)
        rounded = int(math.floor(temp_f + 0.5))
        base = lo % span
        bucket_lo = rounded - ((rounded - base) % span)
        return (bucket_lo, bucket_lo + span - 1, "F")
    n = int(math.floor(temp_f + 0.5))
    return (n, n, "F")


def _late_window_dt(city: str, date_str: str) -> datetime:
    tz = ZoneInfo(LOCATIONS[city][2])
    local_date = datetime.fromisoformat(date_str).date()
    local_dt = datetime.combine(local_date, time(LATE_ENTRY_HOUR, 0), tzinfo=tz)
    return local_dt.astimezone(timezone.utc)


def _was_open_at(trade: dict, at_utc: datetime) -> bool:
    entered_at = _parse_dt(trade.get("entered_at"))
    resolved_at = _parse_dt(trade.get("resolved_at"))
    if entered_at and entered_at > at_utc:
        return False
    if resolved_at and resolved_at <= at_utc:
        return False
    return True


def _simulate_pnl(won: bool, price: float, edge_c: float, city: str, local_hour: int) -> tuple[float, float, float]:
    est_prob = _estimate_late_probability(city, edge_c, local_hour)
    model_edge = est_prob - price
    size = min(LATE_MAX_POSITION, _size_late_trade(model_edge))
    if size <= 0:
        return est_prob, model_edge, 0.0
    if won:
        return est_prob, model_edge, (size / price) - size
    return est_prob, model_edge, -size


def _load_obs(city: str, date_str: str, cache: dict[str, list[dict]]) -> list[dict]:
    key = city + "|" + date_str
    if key not in cache:
        cache[key] = _fetch_twc_intraday(STATIONS[city], date_str)
    return cache[key]


def run(prices: list[float]) -> dict:
    trades = _load_jsonl(TRADES_FILE)
    resolved_core = [
        t for t in trades
        if t.get("status") == "resolved"
        and t.get("strategy") == "core"
        and t.get("location") in STATIONS
        and t.get("location") in LOCATIONS
        and t.get("target_date")
    ]
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for trade in resolved_core:
        groups[(trade["location"], trade["target_date"], trade.get("metric") or "high")].append(trade)

    obs_cache: dict[str, list[dict]] = {}
    rows = []
    skipped = defaultdict(int)

    for (city, date_str, metric), group in sorted(groups.items()):
        at_utc = _late_window_dt(city, date_str)
        open_group = [t for t in group if _was_open_at(t, at_utc)]
        if not open_group:
            skipped["no_core_open_at_late_window"] += 1
            continue

        unit = _unit_from_trade(open_group[0], city)
        obs = _load_obs(city, date_str, obs_cache)
        if not obs:
            skipped["twc_empty"] += 1
            continue
        temps = [float(o["temp"]) for o in obs if o.get("temp") is not None]
        if not temps:
            skipped["no_temps"] += 1
            continue

        tz = ZoneInfo(LOCATIONS[city][2])
        local_late = at_utc.astimezone(tz)
        before = []
        for obs_row in obs:
            vt = obs_row.get("valid_time_gmt")
            if vt is None or obs_row.get("temp") is None:
                continue
            obs_local = datetime.fromtimestamp(int(vt), tz=timezone.utc).astimezone(tz)
            if obs_local.date().isoformat() == date_str and obs_local.hour <= local_late.hour:
                before.append(float(obs_row["temp"]))
        if not before:
            skipped["no_obs_before_late_window"] += 1
            continue

        running_c = max(before) if metric == "high" else min(before)
        actual_c = max(temps) if metric == "high" else min(temps)
        bucket = _infer_bucket(running_c, unit, _fahrenheit_bucket_template(group, city, date_str))
        edge_c = _edge_distance_c(running_c, bucket)
        projected = _project_late_eod_extreme_c(running_c, local_late.hour)
        projected_ok = not (
            projected["confidence"] >= 0.7 and not _bucket_contains(projected["projected_c"], bucket)
        )
        edge_ok = edge_c >= LATE_EDGE_BUFFER_C
        strict_qualified = edge_ok and projected_ok
        edge_qualified = edge_ok
        won = _bucket_contains(actual_c, bucket)

        pnl_by_price = {}
        edge_pnl_by_price = {}
        model_edges = {}
        est_prob = None
        for price in prices:
            est_prob, model_edge, pnl = _simulate_pnl(won, price, edge_c, city, local_late.hour)
            price_key = f"{price:.2f}"
            pnl_by_price[price_key] = round(pnl if strict_qualified else 0.0, 2)
            edge_pnl_by_price[price_key] = round(pnl if edge_qualified else 0.0, 2)
            model_edges[price_key] = round(model_edge, 4)

        rows.append({
            "city": city,
            "date": date_str,
            "metric": metric,
            "late_window_utc": at_utc.isoformat(),
            "late_window_local": local_late.strftime("%Y-%m-%d %H:%M"),
            "core_trade_ids": [t.get("trade_id") for t in open_group],
            "core_buckets": sorted({str(t.get("bucket")) for t in open_group}),
            "unit": unit,
            "running_c": round(running_c, 2),
            "actual_c": round(actual_c, 2),
            "late_bucket": _bucket_label(bucket),
            "edge_c": round(edge_c, 3),
            "projected_c": round(projected["projected_c"], 2),
            "edge_ok": edge_ok,
            "projected_ok": projected_ok,
            "strict_qualified": strict_qualified,
            "edge_qualified": edge_qualified,
            "won": won,
            "est_prob": round(est_prob or 0.0, 4),
            "model_edge_by_price": model_edges,
            "pnl_by_price": pnl_by_price,
            "edge_pnl_by_price": edge_pnl_by_price,
        })

    def scenario(rows_subset: list[dict], pnl_key: str) -> dict:
        wins = [r for r in rows_subset if r["won"]]
        losses = [r for r in rows_subset if not r["won"]]
        pnl_totals = {
            f"{price:.2f}": round(sum(r[pnl_key][f"{price:.2f}"] for r in rows_subset), 2)
            for price in prices
        }
        trades_by_price = {
            f"{price:.2f}": sum(1 for r in rows_subset if r[pnl_key][f"{price:.2f}"] != 0)
            for price in prices
        }
        return {
            "trades": len(rows_subset),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(rows_subset) * 100, 1) if rows_subset else None,
            "breakeven_price": round(len(wins) / len(rows_subset), 3) if rows_subset else None,
            "pnl_totals_by_price": pnl_totals,
            "trades_by_price": trades_by_price,
        }

    strict_rows = [r for r in rows if r["strict_qualified"]]
    edge_rows = [r for r in rows if r["edge_qualified"]]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(TRADES_FILE),
        "note": "Resolved CORE history replay. Historical LATE+ entry prices are not stored; P&L is simulated across assumed prices.",
        "resolved_core_trades": len(resolved_core),
        "resolved_core_city_dates": len(groups),
        "reconstructed": len(rows),
        "skip_counts": dict(skipped),
        "strict_scenario": scenario(strict_rows, "pnl_by_price"),
        "edge_locked_scenario": scenario(edge_rows, "edge_pnl_by_price"),
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", default="0.35,0.50,0.60,0.65,0.80", help="Comma-separated assumed entry prices")
    ap.add_argument("--out", help="Output JSON path")
    args = ap.parse_args()
    prices = [float(x.strip()) for x in args.prices.split(",") if x.strip()]
    report = run(prices)

    out = Path(args.out) if args.out else REPORT_DIR / f"late-plus-resolved-history-{datetime.now().date().isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True))

    print("Wrote " + str(out))
    print("Resolved CORE trades: " + str(report["resolved_core_trades"]))
    print("Resolved CORE city/dates: " + str(report["resolved_core_city_dates"]))
    print("Reconstructed with CORE open at LATE window: " + str(report["reconstructed"]))
    print("Skipped: " + json.dumps(report["skip_counts"], sort_keys=True))
    for name in ("strict_scenario", "edge_locked_scenario"):
        s = report[name]
        print(name + ": " + str(s["wins"]) + "W/" + str(s["losses"]) + "L from " + str(s["trades"]) + " setups")
        print("  win_rate=" + str(s["win_rate"]) + " breakeven=" + str(s["breakeven_price"]))
        for price, pnl in s["pnl_totals_by_price"].items():
            print("  @ " + price + ": trades=" + str(s["trades_by_price"][price]) + " pnl=" + str(pnl))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
