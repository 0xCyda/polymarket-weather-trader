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

# Verified via: (a) our own polymarket_analyze run showing they actually trade
# weather markets (Hans323, ColdMath), or (b) polymarket.com/@handle redirect.
# Wallets marked "UNVERIFIED" had no weather trades in our first run — leaving
# them here so you can still run the script and confirm, or swap the address
# if you find the correct one via polymarket.com/@<handle>.
TOP_WEATHER_TRADERS = [
    # VERIFIED weather traders
    ("Hans323",   "0x0f37cb80dee49d55b5f6d9e595d52591d6371410",
     "VERIFIED — 2805 weather trades, +$80k, 56% win. Famous for $1M London bet."),
    ("ColdMath",  "0x594edb9112f526fa6a80b8f858a6379c8a2c1c11",
     "VERIFIED — 6271 weather trades, -$173k despite 66% win (losers 2x winners)."),
    # UNVERIFIED — wallet attribution from third-party sources is wrong or stale.
    # To find the real wallet: open polymarket.com/@<handle> in a browser,
    # copy the URL redirect to /profile/0x... and paste the address here.
    ("gopfan2",   "0xf2f6af4f27ec2dcf4072095ab804016e14cd5817",
     "UNVERIFIED — 0/1695 weather. Real wallet unknown; check polymarket.com/@gopfan2"),
    ("aenews2",   "0x44c1dfe43260c94ed4f1d00de2e1f80fb113ebc1",
     "CANDIDATE — from polymarket.com/@aenews2 profile. Untested."),
    ("aenews-r2", "0xc5f87c6bbef505ae18b5bf6fed1378e2e6a19db2",
     "CANDIDATE — from polymarketanalytics.com. Untested."),
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
