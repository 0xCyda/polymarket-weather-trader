#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent
if str(BASE / "scripts") not in sys.path:
    sys.path.insert(0, str(BASE / "scripts"))

from paper_journal import _MONTH_NAMES, _json_list, _location_to_slug_cities, _parse_outcome_prices  # noqa: E402
from weather_trader import LOCATION_BIAS_C, detect_event_market_unit  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com/events"
CLOB_HISTORY = "https://clob.polymarket.com/prices-history"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def original_cost(trade: dict) -> float:
    partial_cost = sum(float(p.get("cost") or 0) for p in (trade.get("partial_exits") or []))
    return float(trade.get("cost") or 0) + partial_cost


def parse_bucket_range(label: str | None) -> tuple[float, float, str, str] | None:
    if not label:
        return None
    text = str(label).strip()
    unit = "C" if re.search(r"°?\s*C\b", text, re.I) else "F"

    def conv(value: float) -> float:
        return value * 9 / 5 + 32 if unit == "C" else value

    match = re.search(r"(-?\d+(?:\.\d+)?)\s*°?\s*[FC]?\s*(?:or\s+)?(?:below|less|lower)", text, re.I)
    if match:
        return (-math.inf, conv(float(match.group(1))), unit, "below")
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*°?\s*[FC]?\s*(?:or\s+)?(?:above|higher|more)", text, re.I)
    if match:
        return (conv(float(match.group(1))), math.inf, unit, "above")
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:°?\s*[FC])?\s*(?:-|–|to)\s*(-?\d+(?:\.\d+)?)", text, re.I)
    if match:
        lo = float(match.group(1))
        hi = float(match.group(2))
        return (conv(min(lo, hi)), conv(max(lo, hi)), unit, "range")
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*°?\s*[FC]?\b", text, re.I)
    if match:
        temp = conv(float(match.group(1)))
        return (temp - 0.5, temp + 0.5, unit, "exact")
    return None


def bucket_center_f(label: str | None) -> float | None:
    bucket = parse_bucket_range(label)
    if not bucket:
        return None
    lo, hi, _unit, _kind = bucket
    if math.isinf(lo):
        return hi
    if math.isinf(hi):
        return lo
    return (lo + hi) / 2


def bucket_contains(label: str | None, temp_f: float | None) -> bool:
    bucket = parse_bucket_range(label)
    if not bucket or temp_f is None:
        return False
    lo, hi, _unit, _kind = bucket
    return lo <= temp_f <= hi


def yes_bucket_label(markets: list[dict]) -> str | None:
    best_market = None
    best_price = -1.0
    for market in markets or []:
        prices = _parse_outcome_prices(market.get("outcomePrices"))
        if not prices or len(prices) < 2:
            continue
        try:
            yes_price = float(prices[0])
        except Exception:
            continue
        if yes_price > best_price:
            best_price = yes_price
            best_market = market
    if best_market and best_price >= 0.99:
        return best_market.get("groupItemTitle") or best_market.get("question")
    return None


def market_label(market: dict | None) -> str | None:
    if not market:
        return None
    return market.get("groupItemTitle") or market.get("question") or None


def market_yes_token(market: dict | None) -> str | None:
    if not market:
        return None
    tokens = [str(x) for x in _json_list(market.get("clobTokenIds"))]
    return tokens[0] if tokens else None


def fetch_event(key: tuple[str, str, str]) -> tuple[tuple[str, str, str], list[dict]]:
    location, date_str, metric = key
    year, month_num, day = date_str.split("-")
    month_name = _MONTH_NAMES[int(month_num) - 1]
    prefix = "highest" if metric == "high" else "lowest"
    for slug_city in _location_to_slug_cities(location):
        slug = f"{prefix}-temperature-in-{slug_city}-on-{month_name}-{int(day)}-{year}"
        try:
            response = requests.get(GAMMA, params={"slug": slug}, timeout=8)
            if response.status_code == 200:
                events = response.json()
                if events:
                    return key, events[0].get("markets") or []
        except Exception:
            pass
    return key, []


def load_raw_forecasts() -> dict[tuple[str, str, str], list[tuple[datetime | None, dict]]]:
    out: dict[tuple[str, str, str], list[tuple[datetime | None, dict]]] = defaultdict(list)
    with (BASE / "data" / "forecast_history.jsonl").open() as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("forecast_temp") is None:
                continue
            key = (event.get("location"), event.get("target_date"), event.get("metric", "high"))
            out[key].append((parse_ts(event.get("logged_at")), event))
    for key in out:
        out[key].sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=timezone.utc))
    return out


def raw_forecast_for_trade(trade: dict, forecasts: dict) -> float | None:
    key = (trade.get("location"), trade.get("target_date"), trade.get("metric", "high"))
    entered = parse_ts(trade.get("entered_at"))
    rows = forecasts.get(key, [])
    if rows and entered:
        before = [row for row in rows if row[0] and row[0] <= entered]
        if before:
            return float(before[-1][1]["forecast_temp"])
        best = min(rows, key=lambda row: abs(((row[0] or entered) - entered).total_seconds()) if row[0] else 10**12)
        return float(best[1]["forecast_temp"])
    if rows:
        return float(rows[-1][1]["forecast_temp"])
    return None


def pick_no_bias_market(markets: list[dict], raw_f: float | None, location: str, event_name: str = "") -> tuple[dict | None, float | None]:
    if raw_f is None:
        return None, None
    market_unit = detect_event_market_unit(markets, event_name)
    if market_unit == "C":
        target_f = round((raw_f - 32) * 5 / 9) * 9 / 5 + 32
    else:
        target_f = round(raw_f)

    containing = []
    nearest = []
    for market in markets or []:
        label = market_label(market)
        bucket = parse_bucket_range(label)
        if not bucket:
            continue
        lo, hi, _unit, kind = bucket
        width = (hi - lo) if not (math.isinf(lo) or math.isinf(hi)) else 9999
        if lo <= target_f <= hi:
            containing.append((width, 0 if kind == "exact" else 1, market))
        center = bucket_center_f(label)
        if center is not None:
            nearest.append((abs(center - target_f), market))
    if containing:
        containing.sort(key=lambda item: (item[0], item[1]))
        return containing[0][2], target_f
    if nearest:
        nearest.sort(key=lambda item: item[0])
        return nearest[0][1], target_f
    return None, target_f


def clob_price_at(token_id: str | None, entered: datetime | None) -> tuple[float | None, str]:
    if not token_id or not entered:
        return None, "missing_token_or_time"
    ts = int(entered.timestamp())
    try:
        response = requests.get(
            CLOB_HISTORY,
            params={"market": token_id, "startTs": ts - 21600, "endTs": ts + 21600, "fidelity": 10},
            timeout=10,
        )
        if response.status_code != 200:
            return None, f"http_{response.status_code}"
        points = []
        for point in (response.json() or {}).get("history") or []:
            try:
                points.append((abs(int(point["t"]) - ts), int(point["t"]), float(point["p"])))
            except Exception:
                continue
        if not points:
            return None, "no_history"
        points.sort(key=lambda item: item[0])
        if points[0][0] > 7200:
            return None, f"too_far_{points[0][0]}s"
        return points[0][2], "history"
    except Exception as exc:
        return None, type(exc).__name__


def pnl_for(win: bool, cost: float, price: float | None) -> float | None:
    if price is None or price <= 0 or price >= 1:
        return None
    return cost * (1 - price) / price if win else -cost


def summarize(rows: list[dict], name: str) -> dict:
    priced = [r for r in rows if r.get("no_bias_pnl") is not None and r.get("actual_settlement_pnl") is not None]
    return {
        "name": name,
        "rows": len(rows),
        "priced": len(priced),
        "actual_journal_green": sum(bool(r["actual_journal_green"]) for r in rows),
        "actual_journal_pnl": round(sum(float(r["actual_journal_pnl"]) for r in rows), 2),
        "actual_market_wins": sum(bool(r["actual_market_win"]) for r in rows),
        "actual_settlement_pnl_priced": round(sum(float(r["actual_settlement_pnl"]) for r in priced), 2),
        "no_bias_wins_priced": sum(bool(r["no_bias_win"]) for r in priced),
        "no_bias_pnl_priced": round(sum(float(r["no_bias_pnl"]) for r in priced), 2),
        "delta_vs_actual_settlement_priced": round(sum(float(r["no_bias_pnl"]) - float(r["actual_settlement_pnl"]) for r in priced), 2),
        "delta_vs_actual_journal_priced": round(sum(float(r["no_bias_pnl"]) - float(r["actual_journal_pnl"]) for r in priced), 2),
    }


def main() -> int:
    trades = []
    with (BASE / "data" / "paper_trades.jsonl").open() as handle:
        for line in handle:
            try:
                trade = json.loads(line)
            except Exception:
                continue
            if trade.get("status") == "resolved" and str(trade.get("side", "yes")).lower() == "yes":
                trades.append(trade)

    forecasts = load_raw_forecasts()
    keys = sorted({(t.get("location"), t.get("target_date"), t.get("metric", "high")) for t in trades})
    market_map: dict[tuple[str, str, str], list[dict]] = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(fetch_event, key) for key in keys]
        for future in as_completed(futures):
            key, markets = future.result()
            market_map[key] = markets

    rows = []
    price_jobs = []
    for idx, trade in enumerate(trades):
        location = trade.get("location")
        date_str = trade.get("target_date")
        metric = trade.get("metric", "high")
        markets = market_map.get((location, date_str, metric), [])
        yes_label = yes_bucket_label(markets)
        yes_center = bucket_center_f(yes_label)
        raw_f = raw_forecast_for_trade(trade, forecasts)
        no_bias_market, target_f = pick_no_bias_market(markets, raw_f, location, str(trade.get("question") or ""))
        no_bias_label = market_label(no_bias_market)
        no_bias_win = bool(no_bias_label and bucket_contains(no_bias_label, yes_center))
        applied_bias = bool(abs(float(LOCATION_BIAS_C.get(location, 0) or 0)) > 0)
        changed = bool(applied_bias and no_bias_label and str(no_bias_label).lower() != str(trade.get("bucket") or "").lower())
        cost = original_cost(trade)
        entry_price = float(trade.get("entry_price") or 0)
        row = {
            "idx": idx,
            "trade_id": trade.get("trade_id"),
            "location": location,
            "target_date": date_str,
            "strategy": trade.get("strategy"),
            "applied_bias": applied_bias,
            "changed": changed,
            "bias_c": float(LOCATION_BIAS_C.get(location, 0) or 0),
            "actual_bucket": trade.get("bucket"),
            "no_bias_bucket": no_bias_label,
            "yes_bucket": yes_label,
            "actual_journal_green": float(trade.get("pnl") or 0) > 0,
            "actual_journal_pnl": float(trade.get("pnl") or 0),
            "actual_market_win": trade.get("outcome") == "yes",
            "actual_settlement_pnl": pnl_for(trade.get("outcome") == "yes", cost, entry_price),
            "no_bias_win": no_bias_win,
            "no_bias_price": None,
            "no_bias_pnl": None,
            "price_status": None,
            "cost": cost,
            "actual_entry_price": entry_price,
            "raw_c": ((raw_f - 32) * 5 / 9) if raw_f is not None else None,
            "target_f": target_f,
        }
        rows.append(row)
        entered = parse_ts(trade.get("entered_at"))
        if no_bias_market and entered:
            price_jobs.append((idx, market_yes_token(no_bias_market), entered))

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(clob_price_at, token, entered): idx for idx, token, entered in price_jobs}
        for future in as_completed(futures):
            idx = futures[future]
            price, status = future.result()
            rows[idx]["no_bias_price"] = price
            rows[idx]["price_status"] = status
            rows[idx]["no_bias_pnl"] = pnl_for(rows[idx]["no_bias_win"], rows[idx]["cost"], price)

    applied = [row for row in rows if row["applied_bias"]]
    changed = [row for row in rows if row["changed"]]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "Actual journal PnL compared with no-city-bias bucket choice. Hypothetical no-bias PnL uses the same original stake and nearest sampled CLOB YES price within two hours of actual entry, then holds to settlement.",
        "summaries": [
            summarize(rows, "all_resolved_yes_side_trades"),
            summarize(applied, "trades_where_city_bias_actually_applied"),
            summarize(changed, "trades_where_no_bias_changed_bucket"),
        ],
        "price_status": dict(Counter(row.get("price_status") for row in rows)),
        "changed_rows": sorted(changed, key=lambda row: (row["no_bias_pnl"] if row["no_bias_pnl"] is not None else -10**9) - (row["actual_settlement_pnl"] if row["actual_settlement_pnl"] is not None else 0), reverse=True),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
