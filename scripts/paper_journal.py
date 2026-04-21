#!/usr/bin/env python3
"""
Paper Trading Journal — Polymarket Weather

Tracks paper trades locally, fetches resolution from Polymarket API,
and computes win/loss P&L without needing Simmer balance.

Usage:
    from paper_journal import log_paper_trade, get_open_positions, get_resolved_trades, get_stats
"""

import json
import os
import sys
import pathlib
from datetime import datetime, timezone
from pathlib import Path

# Loss log — one JSON line per losing trade, written to losses.log in the journal dir
_LOSSES_LOG: Path | None = None

def _losses_log_path() -> Path:
    global _LOSSES_LOG
    if _LOSSES_LOG is None:
        # Resolve relative to this file's directory (scripts/)
        _LOSSES_LOG = Path(__file__).parent.parent / "losses.log"
    return _LOSSES_LOG


def log_loss(trade: dict) -> None:
    """Append a losing trade to losses.log with full signal + execution context."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "trade_id": trade.get("trade_id"),
        "market_id": trade.get("market_id"),
        "question": trade.get("question"),
        "location": trade.get("location"),
        "target_date": trade.get("target_date"),
        "metric": trade.get("metric"),
        "bucket": trade.get("bucket"),
        "side": trade.get("side"),
        "strategy": trade.get("strategy", "core"),
        "signal_strength": trade.get("signal_strength"),
        "entry_price": trade.get("entry_price"),
        "exit_price": trade.get("exit_price"),
        "shares": trade.get("shares"),
        "cost": trade.get("cost"),
        "pnl": trade.get("pnl"),
        "forecast_temp": trade.get("forecast_temp"),
        "model_temps": trade.get("model_temps"),
        "models_used": trade.get("models_used"),
        "agreement_pct": trade.get("agreement_pct"),
        "spread": trade.get("spread"),
        "actual_temp": trade.get("actual_temp"),
        "outcome": trade.get("outcome"),
        "resolved_at": trade.get("resolved_at"),
        "entered_at": trade.get("entered_at"),
    }
    try:
        with open(_losses_log_path(), "a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # Never let logging break trade resolution
import requests
from datetime import datetime, timezone

# Force line-buffered stdout
sys.stdout.reconfigure(line_buffering=True)

JOURNAL_DIR = pathlib.Path(__file__).parent.parent / "data"
JOURNAL_FILE = JOURNAL_DIR / "paper_trades.jsonl"
JOURNAL_FILE.parent.mkdir(exist_ok=True)


def _load_trades() -> list:
    """Load all trades from JSONL, oldest first."""
    if not JOURNAL_FILE.exists():
        return []
    trades = []
    for line in JOURNAL_FILE.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"Warning: skipping corrupt trade line: {line[:80]}", file=sys.stderr)
    return trades


def _save_trades(trades: list) -> None:
    """
    Atomically rewrite JSONL with all trades.

    Writes to a sibling temp file then os.replace() — this is atomic on POSIX
    and NTFS, so a crash during write leaves either the old or new file intact,
    never a truncated one.
    """
    tmp = JOURNAL_FILE.with_suffix(JOURNAL_FILE.suffix + ".tmp")
    payload = "\n".join(json.dumps(t, default=str) for t in trades) + "\n"
    tmp.write_text(payload)
    os.replace(tmp, JOURNAL_FILE)


def log_paper_trade(
    market_id: str,
    question: str,
    side: str,          # "yes" or "no"
    entry_price: float,
    shares: float,
    cost: float,
    bucket: str,
    forecast_temp: float,
    signal_strength: str,
    location: str,
    date_str: str,
    metric: str,
    models_used: int,
    agreement_pct: float,
    spread: float,
    strategy: str = "core",   # "core" (default) or "punt"
    model_temps: dict | None = None,  # {model_name: temp} for all models in ensemble
) -> str:
    """
    Log a new paper trade. Returns the trade_id.
    """
    trade_id = f"paper_{market_id[:16]}_{int(datetime.now(timezone.utc).timestamp())}"
    trade = {
        "trade_id": trade_id,
        "market_id": market_id,
        "question": question,
        "side": side,
        "entry_price": entry_price,
        "shares": shares,
        "cost": cost,
        "bucket": bucket,
        "forecast_temp": forecast_temp,
        "signal_strength": signal_strength,
        "strategy": strategy,
        "location": location,
        "target_date": date_str,
        "metric": metric,
        "models_used": models_used,
        "agreement_pct": agreement_pct,
        "spread": spread,
        "model_temps": model_temps,
        "status": "open",
        "outcome": None,
        "exit_price": None,
        "pnl": None,
        "resolved_at": None,
        "entered_at": datetime.now(timezone.utc).isoformat(),
    }
    trades = _load_trades()
    trades.append(trade)
    _save_trades(trades)
    return trade_id


def _fetch_market_resolution(market_id: str) -> dict | None:
    """Fetch resolution state from Simmer API by market ID (clob.polymarket.com is 403-blocked)."""
    try:
        api_key = os.environ.get("SIMMER_API_KEY")
        if not api_key:
            return None
        resp = requests.get(
            f"https://api.simmer.markets/api/sdk/context/{market_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        m = data.get("market", {}) if isinstance(data, dict) else {}
        if not m:
            return None
        return {
            "resolved": m.get("status") == "resolved",
            "outcome": m.get("outcome"),
            "end_date_utc": m.get("resolves_at", ""),
            "question": m.get("question", ""),
        }
    except Exception:
        return None


# =============================================================================
# Open-Meteo historical archive fallback — settles trades when Simmer API
# returns resolved=true but outcome=None, or when target_date has passed and
# Simmer never confirms resolution. Lets us decide YES/NO ourselves.
# =============================================================================

# Location lat/lon for 34 supported cities — mirrored from ensemble_forecast.py
# Kept here to avoid circular imports. Keep in sync if locations are added.
_HISTORICAL_LOCATIONS = {
    "NYC": (40.7769, -73.8740, "America/New_York"),
    "Chicago": (41.9742, -87.9073, "America/Chicago"),
    "Seattle": (47.4502, -122.3088, "America/Los_Angeles"),
    "Atlanta": (33.6407, -84.4277, "America/New_York"),
    "Dallas": (32.8998, -97.0403, "America/Chicago"),
    "Miami": (25.7959, -80.2870, "America/New_York"),
    "Houston": (29.9902, -95.3368, "America/Chicago"),
    "San Francisco": (37.6213, -122.3790, "America/Los_Angeles"),
    "Phoenix": (33.4373, -112.0078, "America/Phoenix"),
    "Los Angeles": (33.9425, -118.4081, "America/Los_Angeles"),
    "Denver": (39.8617, -104.6732, "America/Denver"),
    "Austin": (30.1945, -97.6699, "America/Chicago"),
    "Las Vegas": (36.0840, -115.1537, "America/Los_Angeles"),
    "Tel Aviv": (32.0853, 34.7818, "Asia/Jerusalem"),
    "Munich": (48.1351, 11.5820, "Europe/Berlin"),
    "London": (51.5074, -0.1278, "Europe/London"),
    "Tokyo": (35.6762, 139.6503, "Asia/Tokyo"),
    "Seoul": (37.5665, 126.9780, "Asia/Seoul"),
    "Ankara": (39.9334, 32.8597, "Europe/Istanbul"),
    "Lucknow": (26.8467, 80.9462, "Asia/Kolkata"),
    "Wellington": (-41.2866, 174.7756, "Pacific/Auckland"),
    "Toronto": (43.6777, -79.6248, "America/Toronto"),
    "Paris": (48.8566, 2.3522, "Europe/Paris"),
    "Milan": (45.4642, 9.1900, "Europe/Rome"),
    "Sao Paulo": (-23.5505, -46.6333, "America/Sao_Paulo"),
    "Warsaw": (52.2297, 21.0122, "Europe/Warsaw"),
    "Singapore": (1.3521, 103.8198, "Asia/Singapore"),
    "Shanghai": (31.2304, 121.4737, "Asia/Shanghai"),
    "Beijing": (39.9042, 116.4074, "Asia/Shanghai"),
    "Shenzhen": (22.5431, 114.0579, "Asia/Shanghai"),
    "Chengdu": (30.5728, 104.0668, "Asia/Shanghai"),
    "Chongqing": (29.4316, 106.9123, "Asia/Shanghai"),
    "Wuhan": (30.5928, 114.3055, "Asia/Shanghai"),
    "Hong Kong": (22.3193, 114.1694, "Asia/Hong_Kong"),
    "Buenos Aires": (-34.6037, -58.3816, "America/Argentina/Buenos_Aires"),
}


def fetch_historical_temp(location: str, date_str: str, metric: str, unit: str = "F") -> float | None:
    """
    Fetch the actual observed high/low temperature for a past date via Open-Meteo
    archive API. Returns the temp in requested unit (°F default) or None on failure.
    """
    loc = _HISTORICAL_LOCATIONS.get(location)
    if not loc:
        return None
    lat, lon, tz = loc
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    tz_enc = tz.replace("/", "%2F")
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={date_str}&end_date={date_str}"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&temperature_unit={temp_unit}&timezone={tz_enc}"
    )
    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            return None
        data = resp.json().get("daily", {})
        key = "temperature_2m_max" if metric == "high" else "temperature_2m_min"
        temps = data.get(key, [])
        if not temps:
            return None
        val = temps[0]
        return round(float(val), 1) if val is not None else None
    except Exception:
        return None


def _parse_bucket_range(bucket_str: str) -> tuple | None:
    """
    Parse a bucket string into (lo, hi, unit) in Fahrenheit.
    Duplicates parse_temperature_bucket from weather_trader.py to keep
    paper_journal.py self-contained (avoids import cycle).

    Returns (lo_f, hi_f, unit) or None. -999/999 are open-ended sentinels.
    """
    import re
    if not bucket_str:
        return None
    unit = 'C' if re.search(r'°C', bucket_str, re.IGNORECASE) else 'F'

    def _to_f(lo, hi):
        if unit == 'C':
            lo = lo * 9 / 5 + 32 if lo != -999 else -999
            hi = hi * 9 / 5 + 32 if hi != 999 else 999
        return (lo, hi)

    m = re.search(r'(-?\d+)\s*°?[fFcC]?\s*(or below|or less)', bucket_str, re.IGNORECASE)
    if m:
        lo, hi = _to_f(-999, int(m.group(1)))
        return (lo, hi, 'F')
    m = re.search(r'(-?\d+)\s*°?[fFcC]?\s*(or higher|or above|or more)', bucket_str, re.IGNORECASE)
    if m:
        lo, hi = _to_f(int(m.group(1)), 999)
        return (lo, hi, 'F')
    m = re.search(r'(-?\d+)\s*(?:°?\s*[fFcC])?\s*(?:-|–|to)\s*(-?\d+)', bucket_str)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        lo, hi = _to_f(min(a, b), max(a, b))
        return (lo, hi, 'F')
    m = re.search(r'(-?\d+)\s*°[fFcC]', bucket_str)
    if m:
        t = int(m.group(1))
        lo, hi = _to_f(t, t)
        return (lo, hi, 'F')
    m = re.match(r'^\s*(-?\d+)\s*°?[cCfF]?\s*$', bucket_str.strip())
    if m:
        t = int(m.group(1))
        lo, hi = _to_f(t, t)
        return (lo, hi, 'F')
    return None


# Days past target_date before we fall back to historical-temp settlement
_FALLBACK_DAYS_PAST = 2


def _historical_fallback_settlement(trade: dict) -> dict | None:
    """
    Settle a stuck-open trade by checking the actual observed temperature
    against the bucket range via Open-Meteo archive.

    Returns {"outcome": "yes"/"no", "exit_price": 0.0|1.0, "actual_temp": float,
    "source": "historical_fallback"} or None if we can't determine.
    """
    target_date = trade.get("target_date")
    location = trade.get("location")
    metric = trade.get("metric", "high")
    bucket = trade.get("bucket") or trade.get("question", "")
    if not target_date or not location or not bucket:
        return None

    # Check target_date is at least _FALLBACK_DAYS_PAST old (UTC)
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if (datetime.now(timezone.utc) - target).days < _FALLBACK_DAYS_PAST:
        return None

    actual = fetch_historical_temp(location, target_date, metric, unit="F")
    if actual is None:
        return None

    bucket_range = _parse_bucket_range(bucket)
    if not bucket_range:
        return None
    lo_f, hi_f, _ = bucket_range

    yes_won = (lo_f <= actual <= hi_f)
    return {
        "outcome": "yes" if yes_won else "no",
        "exit_price": 1.0 if yes_won else 0.0,
        "actual_temp": actual,
        "source": "historical_fallback",
    }


def _compute_pnl(side: str, entry: float, exit_price: float, shares: float) -> float:
    """
    Compute realized P&L for a paper trade.

    exit_price is the YES token settlement price (0 or 1 at resolution).
    - YES position payoff at settlement: exit_price  →  P&L = (exit_price - entry) * shares
    - NO  position payoff at settlement: (1 - exit_price)
      Entry paid for NO tokens was `entry` per share.
      P&L = ((1 - exit_price) - entry) * shares
    """
    shares = float(shares or 0)
    entry = float(entry or 0)
    exit_price = float(exit_price or 0)
    if (side or "yes").lower() == "yes":
        return (exit_price - entry) * shares
    return ((1.0 - exit_price) - entry) * shares


def update_resolved_trades() -> list:
    """
    Check all open paper trades. Settlement flow:
      1. Query Simmer API for resolution status + outcome.
      2. If Simmer says resolved AND outcome surfaced → settle.
      3. If Simmer says resolved but outcome=None, OR target_date is >2 days
         past with no resolution, → fall back to Open-Meteo archive historical
         temperature and settle ourselves.

    Returns list of newly resolved trades.
    """
    trades = _load_trades()
    newly_resolved = []

    for trade in trades:
        if trade.get("status") == "resolved":
            continue

        market_id = trade.get("market_id")
        resolution = _fetch_market_resolution(market_id) if market_id else None

        outcome = None
        exit_price = None
        source = None

        # Path 1: Simmer reports resolved + outcome
        if resolution and resolution.get("resolved") and resolution.get("outcome"):
            outcome = resolution["outcome"]
            exit_price = 1.0 if outcome.lower() in ("yes", "true") else 0.0
            source = "simmer"
        else:
            # Path 2: historical fallback (Simmer outcome=None OR just expired)
            fb = _historical_fallback_settlement(trade)
            if fb:
                outcome = fb["outcome"]
                exit_price = fb["exit_price"]
                source = fb["source"]
                trade["actual_temp"] = fb["actual_temp"]

        if outcome is None or exit_price is None:
            continue  # Not ready yet

        side = trade.get("side", "yes")
        entry = trade.get("entry_price", 0)
        shares = trade.get("shares", 0)
        pnl = _compute_pnl(side, entry, exit_price, shares)

        old_status = trade.get("status")
        trade["status"] = "resolved"
        trade["outcome"] = outcome
        trade["exit_price"] = exit_price
        trade["pnl"] = round(pnl, 4)
        trade["resolved_at"] = datetime.now(timezone.utc).isoformat()
        trade["resolution_date"] = (
            (resolution or {}).get("end_date_utc", "")[:10] if resolution else trade.get("target_date", "")
        )
        trade["resolution_source"] = source

        # Write to losses.log if this was a losing trade
        if pnl < 0:
            log_loss(trade)

        if old_status == "open":
            newly_resolved.append(trade)

    _save_trades(trades)
    return newly_resolved


def manual_resolve(trade_id: str, outcome: str) -> dict | None:
    """
    Manually resolve a specific trade. outcome must be "yes" or "no".
    Returns the updated trade dict or None if not found.
    """
    outcome = (outcome or "").lower().strip()
    if outcome not in ("yes", "no"):
        raise ValueError("outcome must be 'yes' or 'no'")
    trades = _load_trades()
    target = None
    for t in trades:
        if t.get("trade_id") == trade_id:
            target = t
            break
    if target is None:
        return None
    exit_price = 1.0 if outcome == "yes" else 0.0
    target["status"] = "resolved"
    target["outcome"] = outcome
    target["exit_price"] = exit_price
    target["pnl"] = round(_compute_pnl(
        target.get("side", "yes"), target.get("entry_price", 0),
        exit_price, target.get("shares", 0)
    ), 4)
    target["resolved_at"] = datetime.now(timezone.utc).isoformat()
    target["resolution_source"] = "manual"
    if target.get("pnl", 0) < 0:
        log_loss(target)
    _save_trades(trades)
    return target


def get_open_positions() -> list:
    """Return all open paper trades without live CLOB prices (prices require CLOB lookup per-trade)."""
    trades = _load_trades()
    return [t for t in trades if t.get("status") == "open"]


def get_open_positions_by_event() -> dict:
    """
    Return open positions keyed by (location, date_str, metric) tuple.
    Values: {"side": str, "market_id": str, "bucket": str, "entry_price": float}.
    Used to detect opposing same-event positions before executing a new trade.
    """
    positions = get_open_positions()
    by_event = {}
    for t in positions:
        key = (t.get("location", ""), t.get("target_date", ""), t.get("metric", ""))
        if key not in by_event:
            by_event[key] = {
                "side": t.get("side", "yes"),
                "market_id": t.get("market_id", ""),
                "bucket": t.get("bucket", ""),
                "entry_price": t.get("entry_price", 0),
                "cost": t.get("cost", 0),
            }
    return by_event


def get_resolved_trades(limit: int = 20) -> list:
    """Return most recent resolved trades."""
    trades = _load_trades()
    resolved = [t for t in trades if t.get("status") == "resolved"]
    resolved.sort(key=lambda t: t.get("resolved_at", ""), reverse=True)
    return resolved[:limit]


def get_stats() -> dict:
    """Compute aggregate paper trading stats."""
    trades = _load_trades()
    resolved = [t for t in trades if t.get("status") == "resolved"]
    open_trades = [t for t in trades if t.get("status") == "open"]

    if not resolved:
        return {
            "total_trades": len(trades),
            "open_trades": len(open_trades),
            "resolved_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "best_trade": None,
            "worst_trade": None,
        }

    wins = [t for t in resolved if t.get("pnl", 0) > 0]
    losses = [t for t in resolved if t.get("pnl", 0) < 0]
    pnls = [t.get("pnl", 0) for t in resolved]

    return {
        "total_trades": len(trades),
        "open_trades": len(open_trades),
        "resolved_trades": len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(resolved) * 100, 1) if resolved else None,
        "total_pnl": round(sum(pnls), 4),
        "avg_pnl": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
        "best_trade": max(pnls) if pnls else None,
        "worst_trade": min(pnls) if pnls else None,
    }


def print_summary() -> None:
    """Print a human-readable summary to stdout."""
    stats = get_stats()
    open_pos = get_open_positions()

    print("\n📓 Paper Trading Journal — Polymarket Weather")
    print("=" * 50)
    print(f"  Total trades:  {stats['total_trades']}")
    print(f"  Open:          {stats['open_trades']}")
    print(f"  Resolved:      {stats['resolved_trades']}")

    if stats["resolved_trades"] > 0:
        print(f"\n  Win rate:      {stats['win_rate']}%")
        print(f"  Total P&L:    ${stats['total_pnl']:.4f}")
        print(f"  Avg P&L:      ${stats['avg_pnl']:.4f}")
        print(f"  Best trade:   ${stats['best_trade']:.4f}" if stats['best_trade'] is not None else "  Best trade:    —")
        print(f"  Worst trade:  ${stats['worst_trade']:.4f}" if stats['worst_trade'] is not None else "  Worst trade:   —")
    else:
        print(f"\n  No resolved trades yet.")

    if open_pos:
        print(f"\n  Open positions ({len(open_pos)}):")
        for t in open_pos:
            print(f"  • {t.get('location', '?')} {t.get('target_date', '')} {t.get('metric', '')} — {t.get('side', '?').upper()} | {t.get('shares', 0):.1f} shares @ ${t.get('entry_price', 0):.4f}")

    print()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Paper journal — summary, manual resolve, backfill")
    parser.add_argument("--resolve", metavar="TRADE_ID", help="Manually resolve a trade by trade_id")
    parser.add_argument("--outcome", choices=["yes", "no"], help="Outcome for --resolve (yes|no)")
    parser.add_argument("--backfill", action="store_true",
                        help="Run update_resolved_trades() to settle any resolvable open trades (incl. historical fallback)")
    parser.add_argument("--list-open", action="store_true", help="List open trade_ids + questions")
    args = parser.parse_args()

    if args.list_open:
        for t in get_open_positions():
            q = (t.get("question") or "")[:60]
            print(f"{t.get('trade_id', '?')}  |  {t.get('location', '?')}  {t.get('target_date', '')}  |  {q}")
    elif args.resolve:
        if not args.outcome:
            print("Error: --outcome yes|no is required with --resolve", file=sys.stderr)
            sys.exit(2)
        updated = manual_resolve(args.resolve, args.outcome)
        if updated is None:
            print(f"Trade {args.resolve} not found.", file=sys.stderr)
            sys.exit(1)
        print(f"Resolved {args.resolve} as {args.outcome.upper()}: exit=${updated['exit_price']:.2f} "
              f"pnl=${updated['pnl']:.4f}")
    elif args.backfill:
        newly = update_resolved_trades()
        print(f"Settled {len(newly)} trade(s):")
        for t in newly:
            src = t.get("resolution_source", "?")
            print(f"  • {t.get('location')} {t.get('target_date')} {t.get('outcome', '?').upper()} "
                  f"(source={src}) pnl=${t.get('pnl', 0):.4f}")
        if not newly:
            print("  (none ready yet)")
    else:
        print_summary()
