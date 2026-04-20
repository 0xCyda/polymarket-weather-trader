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
    """Rewrite JSONL file with all trades."""
    JOURNAL_FILE.write_text("\n".join(json.dumps(t, default=str) for t in trades) + "\n")


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
        "location": location,
        "target_date": date_str,
        "metric": metric,
        "models_used": models_used,
        "agreement_pct": agreement_pct,
        "spread": spread,
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
    """Fetch resolution state from Polymarket API by market ID."""
    try:
        resp = requests.get(
            f"https://clob.polymarket.com/markets/{market_id}",
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        m = resp.json()
        if not m:
            return None
        return {
            "resolved": m.get("resolved", False),
            "outcome": m.get("outcome"),          # e.g. "Yes" or "No"
            "end_date_utc": m.get("end_date_utc", ""),
            "question": m.get("question", ""),
        }
    except Exception:
        return None


def _outcome_price(market_id: str) -> float | None:
    """Get the YES token settlement price (0.00 or 1.00) for a resolved market."""
    try:
        resp = requests.get(
            f"https://clob.polymarket.com/markets/{market_id}",
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        m = resp.json()
        if not m.get("resolved"):
            return None
        # outcomePrices is ["1.00", "0.00"] if YES wins, ["0.00", "1.00"] if NO wins
        # prices[0] is always the YES token settlement price
        prices = m.get("outcomePrices", [])
        if not prices:
            return None
        try:
            return float(prices[0])
        except (ValueError, IndexError):
            return None
    except Exception:
        return None


def update_resolved_trades() -> list:
    """
    Check all open paper trades against Polymarket API.
    Updates status, outcome, exit_price, pnl for any that have resolved.
    Returns list of newly resolved trades.
    """
    trades = _load_trades()
    newly_resolved = []

    for trade in trades:
        if trade.get("status") == "resolved":
            continue

        market_id = trade.get("market_id")
        if not market_id:
            continue

        resolution = _fetch_market_resolution(market_id)
        if not resolution or not resolution.get("resolved"):
            continue

        # Market has resolved — derive YES token settlement price from resolution
        outcome = resolution.get("outcome", "")
        exit_price = 1.0 if outcome.lower() in ("yes", "true") else 0.0

        # Calculate P&L
        side = trade.get("side", "yes")
        entry = trade.get("entry_price", 0)
        shares = trade.get("shares", 0)

        if side == "yes":
            pnl = (exit_price - entry) * shares
        else:  # no
            pnl = (entry - exit_price) * shares

        old_status = trade.get("status")
        trade["status"] = "resolved"
        trade["outcome"] = resolution.get("outcome")
        trade["exit_price"] = exit_price
        trade["pnl"] = round(pnl, 4)
        trade["resolved_at"] = datetime.now(timezone.utc).isoformat()
        trade["resolution_date"] = resolution.get("end_date_utc", "")[:10]

        if old_status == "open":
            newly_resolved.append(trade)

    _save_trades(trades)
    return newly_resolved


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
    print_summary()
