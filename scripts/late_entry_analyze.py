#!/usr/bin/env python3
"""
Post-process late_entry_backtest_results.jsonl.

Uses `predicted_correct` from the backtest as the win flag. That flag is
True iff running_max at 3pm local was inside the winning bucket's parsed
edges -- for contiguous uniform-width markets this is equivalent to
"buy pick_bucket at 3pm wins", which is the strategy we care about.

P&L nets Simmer's 10% winner fee.
"""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
RESULTS = BASE / "data" / "late_entry_backtest_results.jsonl"

SIMMER_FEE = 0.10
STAKE = 100.0
ENTRY_PRICES = [0.35, 0.45, 0.55, 0.65, 0.75, 0.85]


def net_pnl(ep: float, wins: int, losses: int, stake: float = STAKE) -> tuple[float, float]:
    shares = stake / ep
    gross_win = shares * 1.0 - stake
    net_win = gross_win * (1 - SIMMER_FEE)
    total = wins * net_win + losses * (-stake)
    n = wins + losses
    roi = total / (n * stake) * 100 if n else 0.0
    return total, roi


def breakeven_price(hit: float) -> float:
    """Entry price below which strategy is profitable, given Simmer fee.
    EV(ep) = hit * (1/ep - 1) * (1 - fee) - (1 - hit) = 0
    → ep = hit * (1 - fee) / ((1 - hit) + hit * (1 - fee))
    """
    return hit * (1 - SIMMER_FEE) / ((1 - hit) + hit * (1 - SIMMER_FEE))


def print_pnl(label: str, wins: int, losses: int):
    n = wins + losses
    if n == 0:
        print(f"\n{label}\n  (no trades)")
        return
    hit = wins / n
    be = breakeven_price(hit)
    print(f"\n{label}")
    print(f"  Trades: {n}  Wins: {wins}  Hit rate: {hit*100:.1f}%  Breakeven entry: ${be:.2f}")
    print(f"  {'Entry':>8} {'P&L':>14} {'ROI':>8}  EV/trade")
    for ep in ENTRY_PRICES:
        pnl, roi = net_pnl(ep, wins, losses)
        ev = pnl / n
        print(f"  {ep:>8.2f} {pnl:>14,.0f} {roi:>7.1f}%  ${ev:>6.2f}")


def main():
    rows = [json.loads(l) for l in RESULTS.read_text().splitlines() if l.strip()]
    print(f"Loaded {len(rows)} resolved markets (Jan 23 → Apr 23, 2026).")
    print(f"Fee model: Simmer {SIMMER_FEE*100:.0f}% on winner profit. Stake: ${STAKE:.0f}.")

    # === STRAT 1: vanilla ===
    w = sum(1 for r in rows if r["predicted_correct"])
    l = len(rows) - w
    print_pnl("STRAT 1 — Vanilla: buy pick_bucket at 3pm local (any city)", w, l)

    # === Per-city ===
    by_city = defaultdict(list)
    for r in rows:
        by_city[r["city"]].append(r)
    city_stats = []
    for c, rs in by_city.items():
        cw = sum(1 for r in rs if r["predicted_correct"])
        cn = len(rs)
        city_stats.append((c, cw, cn, cw / cn))
    city_stats.sort(key=lambda x: -x[3])

    print("\nPer-city hit rates:")
    print(f"  {'City':<15} {'W/N':>8} {'Hit%':>6}  Breakeven  P&L@$0.65")
    good = set()
    marginal = set()
    for c, w, n, hit in city_stats:
        be = breakeven_price(hit)
        pnl65, _ = net_pnl(0.65, w, n - w)
        tag = ""
        if hit >= 0.80:
            good.add(c); tag = " GOOD"
        elif hit >= 0.70:
            good.add(c); tag = " ok"
        elif hit >= 0.60:
            marginal.add(c); tag = " marginal"
        else:
            tag = " SKIP"
        print(f"  {c:<15} {w:>3}/{n:<3} {hit*100:>5.1f}%   ${be:.2f}    ${pnl65:>7,.0f}{tag}")

    # === STRAT 2: good-city filter ===
    g = [r for r in rows if r["city"] in good]
    w = sum(1 for r in g if r["predicted_correct"])
    l = len(g) - w
    print(f"\nGood cities (hit >= 70%): {sorted(good)}")
    print_pnl("STRAT 2 — Filter to good cities only", w, l)

    # === STRAT 3: adjacent-bucket NO (upper-bound proxy) ===
    # Approximation: adjacent_NO wins if temp did NOT rise into the bucket
    # immediately above pick. For high-temp markets with metric=high:
    #   - predicted_correct=True  → pick=winner, adjacent resolves NO → WIN
    #   - predicted_correct=False → actual landed somewhere else; we can't
    #     tell without reparsing the full bucket grid, so count as LOSS.
    # This is conservative (real results should be better).
    high_rows = [r for r in rows if r["metric"] == "high"]
    adj_wins = sum(1 for r in high_rows if r["predicted_correct"])
    adj_losses = len(high_rows) - adj_wins
    print_pnl(
        "STRAT 3 — Adjacent-bucket NO (conservative proxy, high-temp only)",
        adj_wins, adj_losses,
    )
    # On Simmer the NO side is priced as (1 - YES). Adjacent buckets typically
    # trade at $0.10-0.30 YES in the winning-bucket case, meaning NO trades at
    # $0.70-0.90 -- so apply lower entry prices to represent the upside here.


if __name__ == "__main__":
    main()
