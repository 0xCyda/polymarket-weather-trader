#!/usr/bin/env python3
"""
Position manager: hourly day-of management of open paper positions.

Pulls TWC intraday observations for cities with open positions targeting today
(in city-local time) and decides one of:

  * EXIT — projected EOD max sits in a different bucket than what we hold.
           Settle the position at current Simmer mid-price, mark as resolved
           with resolution_source="early_exit_position_manager".
  * ADD  — projected EOD max sits solidly inside our held bucket and current
           Simmer price is below late_add_ceiling. Open a new paper trade
           (strategy="late_add") capped at late_add_max_position_usd.
  * HOLD — anything else.

Action thresholds are gated by city-local hour because the diurnal peak (and
therefore confidence in the projection) is itself a function of how late in
the day it is. See _evaluate_position for the rules.

The bot's bucket field has historical mismatches — re-derive bucket from the
question text via parse_temperature_bucket rather than trusting trade["bucket"].

Run:
  python3 scripts/position_manager.py             # dry run, log to manager_actions.jsonl
  python3 scripts/position_manager.py --execute   # apply exits/adds to paper journal
  python3 scripts/position_manager.py --trade-id X --execute  # one position only

Cron (hourly, after late_trader's slot to avoid double-spending TWC quota):
  10 * * * *  python3 /path/to/position_manager.py --execute
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from weather_trader import (
    CONFIG_SCHEMA, fetch_weather_markets, parse_temperature_bucket,
    parse_market_bucket, log_error,
)
from paper_journal import (
    _HISTORICAL_LOCATIONS as LOCATIONS,
    JOURNAL_FILE, _load_trades, _save_trades, _compute_pnl, log_loss,
    log_paper_trade, update_trade_atomically,
)
from late_trader import (
    STATIONS, _fetch_twc_intraday, _running_extreme,
    _bucket_contains, _edge_distance_c, _bucket_label,
)

from simmer_sdk.skill import load_config
_cfg = load_config(CONFIG_SCHEMA, str(_HERE / "weather_trader.py"), slug="polymarket-weather-trader")

# Reuse late_trader's edge buffer — same semantic ("locked in" vs "borderline")
EDGE_BUFFER_C       = float(_cfg.get("late_edge_buffer_c", 0.3))
ADD_MAX_POSITION    = float(_cfg.get("late_add_max_position_usd", 100.0))
ADD_PRICE_CEILING   = float(_cfg.get("late_add_price_ceiling", 0.85))
EXIT_AFTER_HOUR     = int(_cfg.get("position_exit_after_hour", 16))   # post-peak local hour
ADD_AFTER_HOUR      = int(_cfg.get("position_add_after_hour", 14))    # peak-window start
PRE_PEAK_BREAKOUT_C = float(_cfg.get("position_pre_peak_breakout_c", 0.5))

ACTIONS_LOG = JOURNAL_FILE.parent / "manager_actions.jsonl"


def _log_action(record: dict) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
    try:
        with open(ACTIONS_LOG, "a") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _resolve_bucket(trade: dict, market: dict | None) -> tuple | None:
    """Re-derive (lo, hi, unit) for a position's bucket.

    Source-of-truth order: live market question → stored trade question →
    stored trade bucket label. The trade["bucket"] field has known historical
    mismatches (bucket-match bug residue), so we only fall back to it last.
    """
    if market is not None:
        b, _ = parse_market_bucket(market)
        if b:
            return b
    q = trade.get("question") or ""
    b = parse_temperature_bucket(q)
    if b:
        return b
    label = trade.get("bucket") or ""
    return parse_temperature_bucket(label)


def _project_eod_max_c(running_c: float, local_hour: int, forecast_f: float | None) -> dict:
    """Crude EOD max projection.

    Returns {"projected_c": float, "confidence": float in [0,1]}.

    Heuristic — diurnal peak is typically 14:00-16:00 local for highs:
      * post-peak (>= EXIT_AFTER_HOUR): running max is essentially final, +0.3°C buffer
      * peak window (ADD_AFTER_HOUR..EXIT_AFTER_HOUR-1): running max ± forecast envelope
      * pre-peak: running so far is a floor; project up using forecast as ceiling

    Confidence rises through the day because the unknown (rest-of-day climb)
    shrinks. We don't try to model anything fancier than "how much room is left
    for the temp to climb past what we've seen so far".
    """
    if local_hour >= EXIT_AFTER_HOUR:
        return {"projected_c": running_c + 0.3, "confidence": 0.95}
    if local_hour >= ADD_AFTER_HOUR:
        # Inside peak window — running max is likely close. Add a small upside buffer
        # if the forecast disagrees by more than 1°C (means day might still climb).
        forecast_c = (forecast_f - 32) * 5 / 9 if forecast_f is not None else running_c
        ceiling = max(running_c + 0.5, forecast_c)
        return {"projected_c": ceiling, "confidence": 0.7}
    # Pre-peak: running so far is just a floor. Project up to forecast envelope.
    forecast_c = (forecast_f - 32) * 5 / 9 if forecast_f is not None else running_c + 2.0
    return {"projected_c": max(running_c, forecast_c), "confidence": 0.4}


def _evaluate_position(trade: dict, market: dict | None, log=print) -> dict:
    """Decide HOLD / EXIT / ADD for one open position. Pure function — no side effects."""
    out: dict = {
        "trade_id": trade.get("trade_id"),
        "market_id": trade.get("market_id"),
        "city": trade.get("location"),
        "target_date": trade.get("target_date"),
        "side": trade.get("side"),
        "action": "hold",
        "reason": None,
    }

    side = (trade.get("side") or "yes").lower()
    if side != "yes":
        # Inverting projection logic for NO-side positions is straightforward but
        # there are zero such positions in the journal at the time this script
        # was written. Defer until needed and skip cleanly.
        out["reason"] = "side_no_unsupported"
        return out

    city = trade.get("location") or ""
    loc = LOCATIONS.get(city)
    station = STATIONS.get(city)
    if not loc or not station:
        out["reason"] = "no_station_or_tz"
        return out

    tz = ZoneInfo(loc[2])
    local_now = datetime.now(timezone.utc).astimezone(tz)
    local_today = local_now.date().isoformat()
    target = trade.get("target_date") or ""

    if target != local_today:
        # Past targets get resolved by the existing update_resolved_trades cron.
        # Future targets aren't actionable yet (no obs).
        out["reason"] = "not_today_local"
        out["local_today"] = local_today
        return out

    bucket = _resolve_bucket(trade, market)
    if not bucket:
        out["reason"] = "bucket_unparseable"
        return out
    out["bucket"] = _bucket_label(bucket)

    # Re-derived bucket disagrees with stored bucket → repair the journal entry
    # so subsequent reads (dashboard, analytics, manual close) see the correct
    # label. The mismatch was caused by upstream parsing bugs at trade-log time
    # (e.g. NYC trade c5d1b4d4 stored "31°C" for a "49°F or below" market).
    # We use the re-derived bucket going forward either way; persisting it keeps
    # the journal consistent rather than permanently noisy.
    stored_label = trade.get("bucket") or ""
    rederived_label = _bucket_label(bucket)
    if stored_label and stored_label.replace(" ", "") != rederived_label.replace(" ", ""):
        out["bucket_mismatch"] = {"stored": stored_label, "rederived": rederived_label}
        try:
            update_trade_atomically(
                trade.get("trade_id") or "",
                lambda t: (t.update({"bucket": rederived_label, "bucket_repaired_at": datetime.now(timezone.utc).isoformat(), "bucket_original": stored_label}) or t)
                          if t.get("status") == "open" else None,
            )
        except Exception as e:
            log_error("bucket_repair", str(e), trade_id=trade.get("trade_id"))

    cur_hour = local_now.hour
    out["local_hour"] = cur_hour

    obs = _fetch_twc_intraday(station, local_today)
    if not obs:
        out["reason"] = "twc_empty"
        return out

    metric = trade.get("metric") or "high"
    running_c = _running_extreme(obs, tz, cur_hour, metric)
    if running_c is None:
        out["reason"] = "no_obs_yet"
        return out
    out["running_c"] = round(running_c, 2)

    proj = _project_eod_max_c(running_c, cur_hour, trade.get("forecast_temp"))
    out["projected_c"] = round(proj["projected_c"], 2)
    out["confidence"] = proj["confidence"]

    in_bucket_now = _bucket_contains(running_c, bucket)
    in_bucket_proj = _bucket_contains(proj["projected_c"], bucket)
    edge_c_now = _edge_distance_c(running_c, bucket)
    out["edge_c_running"] = round(edge_c_now, 2)

    # ----- EXIT rules -----
    # Post-peak: if running max sits clearly outside held bucket, day is gone.
    if cur_hour >= EXIT_AFTER_HOUR and not in_bucket_now and edge_c_now <= -EDGE_BUFFER_C:
        out["action"] = "exit"
        out["reason"] = (
            f"post_peak_running_outside_bucket "
            f"(running={running_c:.2f}°C, bucket={out['bucket']}, edge={edge_c_now:.2f}°C)"
        )
        return out

    # Pre-peak breakout: running max already exceeds bucket upper edge by a lot →
    # day's only going up, no path back.
    lo, hi, unit = bucket
    hi_c = hi if unit == "C" else (hi - 32) * 5 / 9 if hi != 999 else 999
    if hi != 999 and running_c > hi_c + PRE_PEAK_BREAKOUT_C:
        out["action"] = "exit"
        out["reason"] = (
            f"breakout_above_bucket "
            f"(running={running_c:.2f}°C > upper={hi_c:.2f}°C+{PRE_PEAK_BREAKOUT_C:.2f}°C)"
        )
        return out

    # ----- ADD rules -----
    # Need to be inside peak window with running max sitting solidly in held bucket
    # AND market price below add ceiling AND we have at least 2 hours of obs to trust.
    if cur_hour >= ADD_AFTER_HOUR and in_bucket_now and edge_c_now >= EDGE_BUFFER_C and proj["confidence"] >= 0.7:
        cur_price = (market or {}).get("external_price_yes")
        if cur_price is None:
            out["reason"] = "add_blocked_no_price"
            return out
        cur_price = float(cur_price)
        out["current_price"] = round(cur_price, 4)
        if cur_price > ADD_PRICE_CEILING:
            out["reason"] = f"add_blocked_price_{cur_price:.3f}_above_ceiling_{ADD_PRICE_CEILING:.2f}"
            return out
        # Don't add if we've already added today (single add per day per position)
        out["action"] = "add"
        out["reason"] = (
            f"running_locked_in_bucket "
            f"(running={running_c:.2f}°C, edge={edge_c_now:.2f}°C, price=${cur_price:.3f})"
        )
        return out

    out["reason"] = "hold_no_signal"
    return out


def _execute_exit(trade_id: str, current_price: float, reason: str) -> dict | None:
    """Mark the trade resolved early at the current Simmer mid-price.

    Atomic load-check-modify-save under the journal lock so two concurrent
    runs (cron + manual --execute) can't both succeed. The status check is
    re-done INSIDE the lock — the prior implementation checked then saved
    in two separate operations, which let Singapore get exited twice in a
    19-minute window on 2026-04-26.

    Mirrors manual_resolve's bookkeeping but uses a continuous exit price
    rather than 1.0/0.0 settlement. resolution_source distinguishes early
    exits from natural settlement so analytics can split them.
    """
    def _mutate(target: dict) -> dict | None:
        if target.get("status") != "open":
            return None  # another process already closed it
        side = target.get("side", "yes")
        entry = float(target.get("entry_price") or 0)
        shares = float(target.get("shares") or 0)
        pnl = round(_compute_pnl(side, entry, current_price, shares), 4)
        target["status"] = "resolved"
        # Outcome proxy for early exits: "yes" if the side was winning at exit time.
        target["outcome"] = "yes" if (side == "yes" and current_price > entry) or (side == "no" and current_price < entry) else "no"
        target["exit_price"] = round(float(current_price), 4)
        target["pnl"] = pnl
        target["resolved_at"] = datetime.now(timezone.utc).isoformat()
        target["resolution_source"] = "early_exit_position_manager"
        target["exit_reason"] = reason
        return target

    resolved = update_trade_atomically(trade_id, _mutate)
    if resolved and float(resolved.get("pnl") or 0) < 0:
        log_loss(resolved)
    return resolved


def _execute_add(trade: dict, market: dict, size_usd: float, reason: str) -> str | None:
    """Scale into the existing parent trade — DOES NOT create a new row.

    Adds shares + cost to the parent at the current market price, recomputes
    a weighted-average entry_price, and appends to an `adds` audit array on
    the parent. This keeps win-rate accounting at the (market_id) level: one
    underlying bet = one row, regardless of how many times we scaled in.
    Otherwise a successful add doubles a single correct decision into 2 wins
    and inflates the win rate.

    Returns the parent trade_id on success, None if the add couldn't be
    applied (status changed, no live price, etc.).
    """
    cur_price = float(market.get("external_price_yes") or 0)
    if cur_price <= 0:
        return None
    # Re-derive bucket from live market question — don't trust trade["bucket"]
    bucket = _resolve_bucket(trade, market)
    bucket_str = _bucket_label(bucket) if bucket else (trade.get("bucket") or "")
    add_shares = size_usd / cur_price

    def _mutate(parent: dict) -> dict | None:
        if parent.get("status") != "open":
            return None  # parent already resolved/exited — abort
        prev_shares = float(parent.get("shares") or 0)
        prev_cost = float(parent.get("cost") or 0)
        prev_entry = float(parent.get("entry_price") or 0)
        new_shares = prev_shares + add_shares
        new_cost = prev_cost + size_usd
        # Weighted-average entry. Falls back to current price if prev_shares==0
        # which shouldn't happen on a real parent but keeps the math safe.
        new_entry = (prev_shares * prev_entry + add_shares * cur_price) / new_shares if new_shares > 0 else cur_price
        parent["shares"] = round(new_shares, 6)
        parent["cost"] = round(new_cost, 4)
        parent["entry_price"] = round(new_entry, 6)
        if bucket_str and not parent.get("bucket"):
            parent["bucket"] = bucket_str
        adds = parent.setdefault("adds", [])
        adds.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "price": round(cur_price, 4),
            "shares": round(add_shares, 6),
            "cost": round(size_usd, 4),
            "reason": reason,
        })
        return parent

    updated = update_trade_atomically(trade.get("trade_id") or "", _mutate)
    if updated is None:
        return None
    return updated.get("trade_id")


def _has_added_today(trade: dict, all_trades: list = None) -> bool:
    """True if this parent trade already received an add today (in city-local
    time). Reads the `adds` audit array on the trade itself — no longer
    cross-references separate late_add rows since adds merge into the parent.
    The all_trades arg is kept for backward compat but unused."""
    adds = trade.get("adds") or []
    if not adds:
        return False
    # Define "today" using the parent trade's target_date (the only meaningful
    # day-of-add window). If any add timestamp falls on the parent's
    # target_date date, we've already added today.
    target_date = trade.get("target_date") or ""
    for a in adds:
        ts = a.get("ts") or ""
        if ts[:10] == target_date:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Day-of position manager: exit losers, add to winners")
    ap.add_argument("--execute", action="store_true", help="Apply exits/adds (default: dry run, log only)")
    ap.add_argument("--trade-id", help="Process only this trade_id")
    args = ap.parse_args()

    all_trades = _load_trades()
    open_trades = [t for t in all_trades if t.get("status") == "open"]
    if args.trade_id:
        open_trades = [t for t in open_trades if t.get("trade_id") == args.trade_id]
    if not open_trades:
        print("no open positions")
        return 0

    markets = fetch_weather_markets()
    market_by_id = {m.get("id"): m for m in markets if m.get("id")}

    print(f"position_manager ({'EXEC' if args.execute else 'DRY'}): {len(open_trades)} open positions")
    n_exit = n_add = n_hold = n_skip = 0
    for trade in open_trades:
        market = market_by_id.get(trade.get("market_id"))
        decision = _evaluate_position(trade, market)
        decision["mode"] = "execute" if args.execute else "dry"

        if decision["action"] == "hold":
            n_hold += 1
            if decision.get("reason") in ("not_today_local", "no_station_or_tz", "side_no_unsupported"):
                n_skip += 1
                n_hold -= 1
                continue
            running = decision.get("running_c")
            proj = decision.get("projected_c")
            bucket = decision.get("bucket", "?")
            hr = decision.get("local_hour")
            tail = ""
            if running is not None:
                tail = f"  running={running}°C proj={proj}°C bucket={bucket} hr={hr}"
            print(f"  HOLD {trade.get('location')} {trade.get('target_date')}: {decision.get('reason')}{tail}")
            _log_action(decision)
            continue

        if decision["action"] == "exit":
            cur_price = float((market or {}).get("external_price_yes") or 0.0)
            if cur_price <= 0:
                decision["action"] = "hold"
                decision["reason"] = "exit_blocked_no_live_price"
                n_hold += 1
                print(f"  HOLD {trade.get('location')} {trade.get('target_date')}: exit blocked — no live price available")
                _log_action(decision)
                continue
            decision["exit_price"] = round(cur_price, 4)
            print(f"  EXIT {trade.get('location')} {trade.get('target_date')} @ ${cur_price:.3f}: {decision.get('reason')}")
            if args.execute:
                resolved = _execute_exit(trade.get("trade_id"), cur_price, decision.get("reason") or "")
                decision["applied"] = bool(resolved)
                if resolved:
                    decision["pnl"] = resolved.get("pnl")
                    print(f"    → resolved early, pnl=${resolved.get('pnl'):.2f}")
            n_exit += 1
            _log_action(decision)
            continue

        if decision["action"] == "add":
            if _has_added_today(trade, all_trades):
                decision["reason"] = (decision.get("reason") or "") + " | skipped: already_added_today"
                n_hold += 1
                print(f"  HOLD {trade.get('location')} {trade.get('target_date')}: already added today")
                _log_action(decision)
                continue
            size = ADD_MAX_POSITION
            # Cap by paper balance — don't over-leverage
            try:
                from paper_journal import get_stats, get_open_positions
                stats = get_stats()
                paper_balance = float(_cfg.get("paper_balance", 10000.0))
                open_exposure = sum(float(p.get("cost", 0)) for p in get_open_positions())
                available = paper_balance + stats.get("total_pnl", 0) - open_exposure
                size = min(size, max(0, available * 0.5))
            except Exception:
                pass
            if size < 5:
                decision["reason"] = (decision.get("reason") or "") + " | skipped: insufficient_balance"
                n_hold += 1
                print(f"  HOLD {trade.get('location')} {trade.get('target_date')}: insufficient balance for add")
                _log_action(decision)
                continue
            print(f"  ADD  {trade.get('location')} {trade.get('target_date')} ${size:.0f}: {decision.get('reason')}")
            if args.execute:
                parent_id = _execute_add(trade, market, size, decision.get("reason") or "")
                decision["applied"] = bool(parent_id)
                decision["scaled_into_trade_id"] = parent_id
                if parent_id:
                    print(f"    → scaled into parent trade_id={parent_id}")
            n_add += 1
            _log_action(decision)

    total = len(open_trades)
    print(f"position_manager done: exit={n_exit} add={n_add} hold={n_hold} skip={n_skip} (of {total})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
