#!/usr/bin/env python3
"""
Custom scan formatter — Polymarket Weather Trader.
Produces Brandon's standard scan output format.

Runs weather_trader.py --dry-run (no --quiet) and reformats:
  - ALL AIFS ENS signals (not just entry opportunities)
  - Portfolio / P&L from Simmer
  - Position details
  - New entries and scan summary
"""
import subprocess
import sys
import re
import os
from datetime import datetime, timezone
from dateutil import tz

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(SKILL_DIR, ".env")


def get_awst_time():
    awst = tz.gettz("Australia/Perth")
    return datetime.now(awst).strftime("%Y-%m-%d %H:%M AWST")


def load_env():
    """Load .env into os.environ."""
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()


def run_scan():
    """Run weather_trader.py --dry-run (no --quiet) and return stdout+stderr."""
    cmd = [sys.executable, os.path.join(SKILL_DIR, "weather_trader.py"), "--dry-run"]
    env = dict(os.environ)
    # Ensure all major locations are scanned (not just the NYC default)
    LOCATIONS = (
        "NYC,Chicago,Seattle,Atlanta,Dallas,Miami,Houston,San Francisco,"
        "Phoenix,Los Angeles,Denver,Austin,Las Vegas,"
        "Tel Aviv,Munich,London,Tokyo,Seoul,Ankara,Lucknow,"
        "Wellington,Toronto,Paris,Milan,Sao Paulo,Warsaw,Singapore,"
        "Shanghai,Beijing,Shenzhen,Chengdu,Chongqing,Wuhan,Hong Kong"
    )
    env["SIMMER_WEATHER_LOCATIONS"] = env.get("SIMMER_WEATHER_LOCATIONS", LOCATIONS)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.stdout + result.stderr


def parse_signals(scan_output):
    """
    Extract all AIFS ENS signal blocks.
    Pattern:
      📍 LOC DATE (metric temp)
        AIFS ENS: TEMP°F | signal: STR | N models | [agree: X%] | spread: Y°

    Returns list of dicts.
    """
    signals = []
    # Split on 📍 to isolate each event block
    blocks = re.split(r"(?=📍\s+\w+)", scan_output)
    for block in blocks:
        if not block.strip():
            continue
        # Extract location, date, metric from header
        header = re.search(r"📍\s+(\S+)\s+(\d{4}-\d{2}-\d{2})\s+\((\w+)\s+temp\)", block)
        if not header:
            continue
        location, date, metric = header.groups()

        # Extract AIFS ENS line
        ens = re.search(
            r"AIFS ENS:\s*([\d.]+)°F\s*\|\s*signal:\s*(\w+)\s*\|\s*(\d+)\s+models\s*"
            r"(?:\|*\s*agree:\s*([\d.]+)%?\s*)?\|\s*spread:\s*([\d.]+)°",
            block,
        )
        if ens:
            temp, signal, models, agree, spread = ens.groups()
            signals.append({
                "location": location,
                "date": date,
                "metric": metric,
                "temp": temp,
                "signal": signal,
                "models": models,
                "agree": agree or "N/A",
                "spread": spread,
            })
    return signals


def parse_new_entries(scan_output):
    """Count new BUY opportunities this scan."""
    return len(re.findall(r"✅\s+BUY\s+opportunity", scan_output))


def parse_scan_stats(scan_output):
    """Extract events scanned and trades executed from Summary block."""
    stats = {"events": 0, "trades": 0, "entries": 0}
    m = re.search(r"Events scanned:\s*(\d+)", scan_output)
    if m:
        stats["events"] = int(m.group(1))
    m = re.search(r"Trades executed:\s*(\d+)", scan_output)
    if m:
        stats["trades"] = int(m.group(1))
    m = re.search(r"Entry opportunities:\s*(\d+)", scan_output)
    if m:
        stats["entries"] = int(m.group(1))
    return stats


def get_simmer_positions():
    """Fetch open positions from Simmer API with live current prices."""
    try:
        api_key = os.environ.get("SIMMER_API_KEY")
        if not api_key:
            return []
        import json
        from urllib.request import urlopen, Request
        from urllib.error import HTTPError, URLError
        url = "https://api.simmer.markets/api/sdk/positions"
        req = Request(url, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
        positions = result.get("positions", []) if isinstance(result, dict) else result
        return [p for p in positions if p.get("shares_yes", 0) > 0 or p.get("shares_no", 0) > 0]
    except Exception:
        return []


def compute_upnl(position):
    """
    Compute unrealized P&L from Simmer position data or paper journal data.
    Simmer: shares_yes/shares_no + entry_price + current_price
    Journal: shares + entry_price + current_price (all YES positions)
    uPNL = (current_price - entry_price) * shares  (YES side)
    uPNL = (entry_price - current_price) * shares (NO side)
    """
    current_price = position.get("current_price", 0)
    entry_price = position.get("entry_price", 0)

    # Simmer API positions
    shares_yes = position.get("shares_yes", 0)
    shares_no = position.get("shares_no", 0)
    if shares_yes > 0 or shares_no > 0:
        if shares_yes > 0:
            upnl = (current_price - entry_price) * shares_yes if entry_price > 0 else 0.0
        else:
            upnl = (entry_price - current_price) * shares_no if entry_price > 0 else 0.0
        return round(upnl, 2)

    # Paper journal positions (all YES, stored as 'shares')
    shares = position.get("shares", 0)
    if shares > 0 and entry_price > 0:
        upnl = (current_price - entry_price) * shares
        return round(upnl, 2)

    return 0.0


def _fetch_live_price(market_id, api_key):
    """Fetch current price for a market from Simmer API. Returns float or None."""
    try:
        import json
        from urllib.request import urlopen, Request
        from urllib.error import HTTPError, URLError
        url = f"https://api.simmer.markets/api/sdk/context/{market_id}"
        req = Request(url, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        with urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        if isinstance(result, dict):
            return float(result.get("market", {}).get("current_price", 0))
        return None
    except Exception:
        return None


def get_positions():
    """Fetch open positions from Simmer API with live uPNL; fall back to paper journal with live price enrichment."""
    simmer_positions = get_simmer_positions()
    if simmer_positions:
        return simmer_positions
    # Fall back to paper journal, but enrich with live current prices from Simmer API
    try:
        sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
        from paper_journal import get_open_positions
        api_key = os.environ.get("SIMMER_API_KEY", "")
        positions = get_open_positions()
        for pos in positions:
            market_id = pos.get("market_id", "")
            if market_id and api_key:
                current_price = _fetch_live_price(market_id, api_key)
                if current_price is not None:
                    pos["current_price"] = current_price
                    shares = pos.get("shares", 0)
                    entry_price = pos.get("entry_price", 0)
                    # uPNL = (current - entry) * shares  (YES side)
                    pos["upnl"] = round((current_price - entry_price) * shares, 2)
                else:
                    pos["current_price"] = 0.0
                    pos["upnl"] = 0.0
            else:
                pos["current_price"] = 0.0
                pos["upnl"] = 0.0
        return positions
    except Exception:
        return []


def get_portfolio_stats():
    """Get paper balance and P&L combining journal realized + Simmer unrealized."""
    try:
        sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))
        from paper_journal import get_stats, _load_trades
        stats = get_stats()
        trades = _load_trades()
        realized_pnl = stats.get("total_pnl", 0.0)
        # 24h realized P&L from resolved trades
        try:
            from datetime import timedelta
            now_utc = datetime.now(timezone.utc)
            cutoff = now_utc - timedelta(hours=24)
            pnl_24h = sum(
                t.get("pnl", 0)
                for t in trades
                if t.get("resolved_at")
                and datetime.fromisoformat(str(t["resolved_at"]).replace("Z", "+00:00")) > cutoff
            )
        except Exception:
            pnl_24h = 0.0
        # Unrealized P&L from Simmer open positions
        try:
            simmer_positions = get_simmer_positions()
            unrealized_pnl = sum(compute_upnl(p) for p in simmer_positions)
        except Exception:
            unrealized_pnl = 0.0
        # If Simmer returned no positions (paper mode), compute unrealized from paper journal
        if not simmer_positions:
            try:
                journal_positions = get_positions()
                unrealized_pnl = sum(compute_upnl(p) for p in journal_positions)
            except Exception:
                pass
        total_pnl = round(realized_pnl + unrealized_pnl, 2)
        return {
            "balance": stats.get("paper_balance", 10000.0),
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": total_pnl,
            "pnl_24h": pnl_24h,
            "open_trades": stats.get("open_trades", 0),
        }
    except Exception as e:
        return {"balance": 10000.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0, "total_pnl": 0.0, "pnl_24h": 0.0, "open_trades": 0}


def get_failures(scan_output):
    """Extract notable error/warning lines."""
    failures = []
    lines = scan_output.splitlines()
    for line in lines:
        ll = line.lower()
        if "error" in ll and "simmer_api_key" not in ll:
            failures.append(line.strip())
        elif "safeguard blocked" in ll:
            failures.append(line.strip())
        elif "❌" in line:
            failures.append(line.strip())
    return failures[:5]


def format_output(scan_output):
    """Build Brandon's standard scan report."""
    now_str = get_awst_time()
    signals = parse_signals(scan_output)
    stats = parse_scan_stats(scan_output)
    new_entries = parse_new_entries(scan_output)
    portfolio = get_portfolio_stats()
    positions = get_positions()
    failures = get_failures(scan_output)

    lines = []
    lines.append(f"Weather Scan — {now_str}")
    lines.append("")
    lines.append("📊 Portfolio")
    unrealized = portfolio.get("unrealized_pnl", 0)
    realized = portfolio.get("realized_pnl", 0)
    unrealized_emoji = "🟢" if unrealized >= 0 else "🔴"
    lines.append(
        f"Balance: ${portfolio['balance']:.2f} (sim) | "
        f"Paper P&L: ${realized:.2f} realized"
    )
    lines.append(
        f"24h P&L: ${portfolio['pnl_24h']:.2f} | "
        f"Total P&L: ${portfolio['total_pnl']:.2f} ({unrealized_emoji} {unrealized:.2f} unrealized)"
    )
    lines.append(f"Open positions: {len(positions)}")
    lines.append("")
    lines.append("Open Positions:")
    if not positions:
        lines.append("None")
    else:
        lines.append("| Market | Side | Shares | Entry | Now | Cost | uPNL |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | --- |")
        for pos in positions:
            question = pos.get("question", "Unknown")
            shares_yes = pos.get("shares_yes", 0)
            shares_no = pos.get("shares_no", 0)
            # Paper journal uses 'shares' + 'side'; Simmer uses shares_yes/shares_no
            if shares_yes > 0 or shares_no > 0:
                side = "YES" if shares_yes > 0 else "NO"
                shares = shares_yes if shares_yes > 0 else shares_no
            else:
                side = pos.get("side", "YES").upper()
                shares = pos.get("shares", 0)
            # Simmer uses cost_basis; paper journal uses cost
            cost = pos.get("cost_basis") or pos.get("cost", 0)
            entry_price = pos.get("entry_price", 0)
            current_price = pos.get("current_price", 0)
            upnl = compute_upnl(pos)
            upnl_str = f"🟢 +${upnl:.2f}" if upnl >= 0 else f"🔴 -${abs(upnl):.2f}"
            # Truncate question to 50 chars for table readability
            q_short = question[:50] + "..." if len(question) > 50 else question
            lines.append(f"| {q_short} | {side} | {shares:.1f} | {entry_price:.2f} | {current_price:.2f} | ${cost:.2f} | {upnl_str} |")
    lines.append("")
    lines.append("AIFS ENS Signals:")
    if not signals:
        lines.append("  None found this scan")
    else:
        for s in signals:
            lines.append(
                f"{s['location']} {s['date']} ({s['metric']} temp): "
                f"{s['temp']}°F | signal: {s['signal']} | "
                f"{s['models']} models | spread: {s['spread']}°"
            )
    lines.append("")
    lines.append("New Entries This Scan:")
    if new_entries > 0:
        lines.append(f"{new_entries} new position(s)")
    else:
        lines.append("None")
    lines.append("")
    lines.append("🔍 This Scan")
    lines.append(f"Markets scanned: {stats['events']}")
    lines.append(f"Signals found: {len(signals)}")
    lines.append(f"Trades executed: {stats['trades']}")
    if failures:
        lines.append("")
        lines.append("[FAILURES]")
        for f in failures:
            lines.append(f"  {f}")
    output = "\n".join(lines)
    # Wrap in code block for Discord markdown table rendering
    return f"```\n{output}\n```"


if __name__ == "__main__":
    load_env()
    try:
        import sys
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        scan_output = run_scan()
        output = format_output(scan_output)
        print(output)
    except Exception as e:
        import traceback
        print(f"Weather Scan — {get_awst_time()}")
        print(f"\n[FORMAT ERROR: {e}]")
        traceback.print_exc()
        sys.exit(1)
