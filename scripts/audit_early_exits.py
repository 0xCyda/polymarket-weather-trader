#!/usr/bin/env python3
"""
Counterfactual backfill for trades closed by position_manager.py.

For each trade with resolution_source="early_exit_position_manager", look up
what the position WOULD HAVE settled at via the existing historical fallback
chain (Polymarket → Gamma → CLI → archive) and write:

  would_have_outcome    "yes" / "no"
  would_have_exit_price 1.0 / 0.0
  would_have_pnl        full-settlement P&L
  regret                would_have_pnl − realized_pnl
                        positive => cut a winner; negative => smart exit
  actual_temp           EOD temp from the resolution source
  audit_source          which source provided actual_temp
  audited_at            ISO timestamp

Run:
  python3 scripts/audit_early_exits.py            # backfill all unaudited
  python3 scripts/audit_early_exits.py --force    # re-audit even if regret already set
  python3 scripts/audit_early_exits.py --summary  # last-30-day rollup, no writes

Designed to run daily on a cron, after target dates have rolled past
_FALLBACK_DAYS_PAST. Idempotent — won't re-audit a trade with regret set
unless --force.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from paper_journal import (
    _load_trades, _save_trades, _historical_fallback_settlement,
    _compute_pnl,
)


EARLY_EXIT_SOURCE = "early_exit_position_manager"


def _audit_one(trade: dict) -> dict | None:
    """Compute counterfactual fields for one early-exited trade.

    The bot's stored bucket field has historical mismatches, so we override
    the bucket lookup by passing the question text (which always carries the
    correct threshold) into _historical_fallback_settlement via a shallow copy.
    """
    if trade.get("resolution_source") != EARLY_EXIT_SOURCE:
        return None
    question = trade.get("question") or ""

    # Pass question as bucket so _parse_bucket_range parses the source-of-truth
    # threshold rather than the (possibly wrong) stored bucket label.
    audit_trade = dict(trade)
    audit_trade["bucket"] = question

    fb = _historical_fallback_settlement(audit_trade, force=True)
    if fb is None:
        return None

    side = trade.get("side", "yes")
    entry = float(trade.get("entry_price") or 0)
    shares = float(trade.get("shares") or 0)
    realized_pnl = float(trade.get("pnl") or 0)
    would_pnl = _compute_pnl(side, entry, fb["exit_price"], shares)

    return {
        "would_have_outcome": fb["outcome"],
        "would_have_exit_price": float(fb["exit_price"]),
        "would_have_pnl": round(would_pnl, 4),
        "regret": round(would_pnl - realized_pnl, 4),
        "actual_temp": fb["actual_temp"],
        "audit_source": fb["source"],
        "audited_at": datetime.now(timezone.utc).isoformat(),
    }


def audit_early_exits(force: bool = False) -> list:
    """Backfill counterfactual fields on all early-exited trades. Returns updated trades."""
    trades = _load_trades()
    updated = []
    for trade in trades:
        if trade.get("resolution_source") != EARLY_EXIT_SOURCE:
            continue
        if trade.get("regret") is not None and not force:
            continue
        result = _audit_one(trade)
        if result is None:
            continue
        trade.update(result)
        updated.append(trade)
    if updated:
        _save_trades(trades)
    return updated


def summarize(days: int = 30) -> dict:
    """Aggregate stats over recent early-exits. Read-only."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    trades = _load_trades()
    rows = []
    for t in trades:
        if t.get("resolution_source") != EARLY_EXIT_SOURCE:
            continue
        try:
            ts = datetime.fromisoformat((t.get("resolved_at") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if ts < cutoff:
            continue
        rows.append(t)

    audited = [t for t in rows if t.get("regret") is not None]
    cut_winners = [t for t in audited if (t.get("regret") or 0) > 0]
    saved = sum(-(t.get("regret") or 0) for t in audited if (t.get("regret") or 0) <= 0)
    regret_total = sum((t.get("regret") or 0) for t in cut_winners)
    realized = sum(float(t.get("pnl") or 0) for t in audited)
    counterfactual = sum(float(t.get("would_have_pnl") or 0) for t in audited)
    return {
        "window_days": days,
        "total_exits": len(rows),
        "audited": len(audited),
        "pending": len(rows) - len(audited),
        "cut_winners": len(cut_winners),
        "smart_exits": len(audited) - len(cut_winners),
        "saved_usd": round(saved, 2),
        "regret_usd": round(regret_total, 2),
        "net_vs_hold_usd": round(realized - counterfactual, 2),
        "realized_pnl_usd": round(realized, 2),
        "would_have_pnl_usd": round(counterfactual, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill counterfactuals for early-exited trades")
    ap.add_argument("--force", action="store_true", help="Re-audit trades that already have regret set")
    ap.add_argument("--summary", action="store_true", help="Print last-30-day summary only (no writes)")
    ap.add_argument("--days", type=int, default=30, help="Summary window in days (default 30)")
    args = ap.parse_args()

    if args.summary:
        s = summarize(args.days)
        print(f"Early exits last {s['window_days']}d:")
        print(f"  total:         {s['total_exits']}")
        print(f"  audited:       {s['audited']}")
        print(f"  pending:       {s['pending']}")
        print(f"  cut winners:   {s['cut_winners']}")
        print(f"  smart exits:   {s['smart_exits']}")
        print(f"  $ saved:       ${s['saved_usd']:.2f}")
        print(f"  $ regret:      ${s['regret_usd']:.2f}")
        print(f"  net vs hold:   ${s['net_vs_hold_usd']:.2f}")
        print(f"    realized:    ${s['realized_pnl_usd']:.2f}")
        print(f"    counterfact: ${s['would_have_pnl_usd']:.2f}")
        return 0

    updated = audit_early_exits(force=args.force)
    print(f"audited {len(updated)} early-exit trade(s)")
    for t in updated:
        regret = t.get("regret") or 0
        tag = "CUT WINNER" if regret > 0 else "SMART EXIT" if regret < 0 else "BREAK EVEN"
        print(
            f"  {tag} {t.get('location')} {t.get('target_date')}: "
            f"realized=${float(t.get('pnl') or 0):.2f} "
            f"would_have=${float(t.get('would_have_pnl') or 0):.2f} "
            f"regret=${regret:+.2f} (actual={t.get('actual_temp')}°F)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
