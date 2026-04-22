#!/usr/bin/env python3
"""
Run polymarket_analyze.py against every verified top weather trader wallet
and save each report to reports/trader_<handle>_<date>.txt.

Usage:
    python scripts/analyze_top_traders.py
    python scripts/analyze_top_traders.py --all   # include unverified wallets
"""

import argparse
import pathlib
import subprocess
import sys
from datetime import datetime

# Verified via public sources (polymarketanalytics.com, Polymarket profiles, X posts).
# See the research agent report for citations.
TOP_WEATHER_TRADERS = [
    ("gopfan2",        "0xf2f6af4f27ec2dcf4072095ab804016e14cd5817",
     "Rank #1 weather leaderboard, +$343k profit, $4.57M volume. Our bot's inspiration."),
    ("Hans323",        "0x0f37cb80dee49d55b5f6d9e595d52591d6371410",
     "~$1.1M from a legendary 8% London weather bet. Top-5 weather."),
    ("gopfan",         "0x6af75d4e4aaf700450efbac3708cce1665810ff1",
     "gopfan2's separate account. +$118k weather P&L."),
    ("ColdMath",       "0x594edb9112f526fa6a80b8f858a6379c8a2c1c11",
     "Pure temp trader (NYC/LA/Atlanta/Cape Town/Istanbul). +$95-110k, $8M volume."),
]


def main():
    parser = argparse.ArgumentParser(description="Analyze all top weather traders")
    parser.add_argument("--all-categories", action="store_true",
                        help="Analyze all markets, not just weather")
    args = parser.parse_args()

    repo_root = pathlib.Path(__file__).parent.parent
    analyze_script = repo_root / "scripts" / "polymarket_analyze.py"
    reports_dir = repo_root / "reports"
    reports_dir.mkdir(exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    combined_file = reports_dir / f"top_traders_{today}.txt"

    print(f"Analyzing {len(TOP_WEATHER_TRADERS)} top weather traders...")
    print(f"Reports → {reports_dir}/\n")

    combined_parts = [
        f"Polymarket Weather Trader Analysis — {today}",
        "=" * 70,
        f"Mode: {'ALL CATEGORIES' if args.all_categories else 'WEATHER ONLY'}",
        "",
    ]

    for handle, wallet, note in TOP_WEATHER_TRADERS:
        print(f"━━━ {handle} ━━━")
        print(f"  {note}")
        print(f"  Wallet: {wallet}")

        cmd = [sys.executable, str(analyze_script), wallet]
        if args.all_categories:
            cmd.append("--all")

        per_trader_file = reports_dir / f"trader_{handle}_{today}.txt"
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            output = result.stdout
            if result.stderr:
                output += "\n---STDERR---\n" + result.stderr

            # Per-trader file
            per_trader_file.write_text(output)

            # Append to combined file
            combined_parts.append("=" * 70)
            combined_parts.append(f"TRADER: {handle}  ({note})")
            combined_parts.append(f"WALLET: {wallet}")
            combined_parts.append("=" * 70)
            combined_parts.append(output)
            combined_parts.append("")

            # Echo first lines to console
            for line in output.splitlines()[:6]:
                print(f"    {line}")
            print(f"  → {per_trader_file.relative_to(repo_root)}\n")
        except subprocess.TimeoutExpired:
            print(f"  ⏱️  Timed out after 120s — skipping\n")
            combined_parts.append(f"### {handle}: TIMED OUT\n")
        except Exception as e:
            print(f"  ❌  Error: {e}\n")
            combined_parts.append(f"### {handle}: ERROR — {e}\n")

    # Write combined file — paste this to share all 4 reports at once
    combined_file.write_text("\n".join(combined_parts))
    print(f"Done. Per-trader reports: {reports_dir}/trader_*_{today}.txt")
    print(f"Combined report (paste this):\n  {combined_file}")


if __name__ == "__main__":
    main()
