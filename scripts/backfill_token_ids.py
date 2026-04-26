#!/usr/bin/env python3
"""
One-time backfill: enrich paper_trades.jsonl with polymarket_token_id /
polymarket_no_token_id on rows that pre-date the token-id refactor.

Pulls the active Simmer markets list once (single Simmer call), builds a
market_id → (yes_token_id, no_token_id) lookup, and patches any open OR
resolved trade row that's missing token ids.

After this runs, the dashboard's live-price refresh path can hit Polymarket
CLOB directly via the stored token_id and skip Simmer for paper_trades.jsonl
rows entirely.

Run:
  python3 scripts/backfill_token_ids.py            # backfill all rows missing token_id
  python3 scripts/backfill_token_ids.py --force    # re-attach token_ids even if present
  python3 scripts/backfill_token_ids.py --dry-run  # no writes
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from paper_journal import _load_trades, _save_trades
from weather_trader import fetch_weather_markets


def main() -> int:
    ap = argparse.ArgumentParser(description="Attach polymarket_token_id to legacy paper trades")
    ap.add_argument("--force", action="store_true", help="Overwrite existing token ids")
    ap.add_argument("--dry-run", action="store_true", help="Show what would change, don't write")
    args = ap.parse_args()

    markets = fetch_weather_markets()
    by_id: dict[str, dict] = {}
    for m in markets:
        mid = m.get("id")
        if not mid:
            continue
        by_id[mid] = m
    print(f"loaded {len(by_id)} active markets from Simmer")

    trades = _load_trades()
    patched = 0
    skipped = 0
    no_match = 0
    already = 0

    for t in trades:
        mid = t.get("market_id")
        if not mid:
            skipped += 1
            continue
        has_yes = t.get("polymarket_token_id")
        has_no = t.get("polymarket_no_token_id")
        if (has_yes and has_no) and not args.force:
            already += 1
            continue
        m = by_id.get(mid)
        if not m:
            no_match += 1
            continue
        new_yes = m.get("polymarket_token_id")
        new_no = m.get("polymarket_no_token_id")
        # Only count as "patched" if at least one field actually changes
        changed = False
        if new_yes and (args.force or not has_yes):
            t["polymarket_token_id"] = new_yes
            changed = True
        if new_no and (args.force or not has_no):
            t["polymarket_no_token_id"] = new_no
            changed = True
        if changed:
            patched += 1

    print(f"summary: patched={patched} already={already} no_simmer_match={no_match} skipped_no_market_id={skipped} total={len(trades)}")

    if patched and not args.dry_run:
        _save_trades(trades)
        print("saved paper_trades.jsonl")
    elif patched:
        print("(dry run — no writes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
