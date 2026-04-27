#!/usr/bin/env python3
"""
Format weather_trader.py scan output for Discord/cron delivery.

Reads raw scan output from stdin and prints a compact summary with:
- AWST timestamp
- Paper portfolio stats
- Open positions with live price enrichment when available
- Recent AIFS ENS signals parsed from the scan output
- New entries from the scan output
- Scan summary stats
- Failures block if present

Critical behavior:
- Loads .env before any live price lookup
- Uses N/A when live price lookup fails instead of faking $0.00 uPNL
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

sys.stdout.reconfigure(line_buffering=True)

SKILL_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = SKILL_DIR / ".env"
SCRIPTS_DIR = SKILL_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from paper_journal import get_open_positions, get_stats, _load_trades  # type: ignore
from weather_trader import city_tier  # type: ignore


def get_awst_time() -> str:
    return datetime.now(ZoneInfo("Australia/Perth")).strftime("%I:%M %p AWST").lstrip("0")


def load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip()


def run_scan() -> str:
    return sys.stdin.read()


def parse_signals(scan_output: str) -> list[str]:
    signals: list[str] = []
    for raw in scan_output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "| signal:" in line and re.search(r"\d{4}-\d{2}-\d{2}", line):
            signals.append(line)
    return signals


def parse_new_entries(scan_output: str) -> list[str]:
    entries: list[str] = []
    in_section = False
    for raw in scan_output.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if in_section:
                break
            continue
        if stripped.lower().startswith("new entries this scan"):
            in_section = True
            continue
        if in_section:
            if stripped in {"None", "none"}:
                return []
            if re.match(r"^(markets scanned|signals found|trades executed|\[failures\])", stripped, re.I):
                break
            entries.append(stripped)
    return entries


def parse_scan_stats(scan_output: str) -> dict[str, int]:
    stats = {"markets_scanned": 0, "signals_found": 0, "trades_executed": 0}
    patterns = {
        "markets_scanned": r"Markets scanned:\s*(\d+)",
        "signals_found": r"Signals found:\s*(\d+)",
        "trades_executed": r"Trades executed:\s*(\d+)",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, scan_output, re.I)
        if m:
            stats[key] = int(m.group(1))
    return stats


def get_failures(scan_output: str) -> list[str]:
    failures: list[str] = []
    in_section = False
    for raw in scan_output.splitlines():
        stripped = raw.rstrip()
        if stripped.strip() == "[FAILURES]":
            in_section = True
            continue
        if in_section:
            if not stripped.strip():
                continue
            failures.append(stripped)
    if failures:
        return failures
    for raw in scan_output.splitlines():
        if any(tok in raw for tok in ("Traceback", "FileNotFoundError", "Error:", "Exception:")):
            failures.append(raw.rstrip())
    return failures


def get_simmer_positions() -> list[dict[str, Any]]:
    load_env()
    api_key = os.environ.get("SIMMER_API_KEY")
    if not api_key:
        return []
    try:
        import requests
        resp = requests.get(
            "https://api.simmer.markets/api/sdk/positions",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        positions = data.get("positions", []) if isinstance(data, dict) else (data or [])
        return [p for p in positions if (p.get("shares_yes") or 0) > 0 or (p.get("shares_no") or 0) > 0]
    except Exception:
        return []


def compute_upnl(position: dict[str, Any]) -> float | None:
    current_price = position.get("current_price")
    entry_price = position.get("entry_price") or 0
    if current_price is None or not entry_price:
        return None

    shares_yes = position.get("shares_yes") or 0
    shares_no = position.get("shares_no") or 0
    if shares_yes > 0 or shares_no > 0:
        if shares_yes > 0:
            return round((current_price - entry_price) * shares_yes, 2)
        return round(((1 - current_price) - entry_price) * shares_no, 2)

    shares = position.get("shares") or 0
    side = str(position.get("side") or "yes").lower()
    if shares > 0:
        if side == "yes":
            return round((current_price - entry_price) * shares, 2)
        return round(((1 - current_price) - entry_price) * shares, 2)
    return None


def _fetch_live_price(market_id: str) -> float | None:
    def _mid_or_buy(token_id: str) -> float | None:
        try:
            import requests
            mid_resp = requests.get(
                "https://clob.polymarket.com/midpoint",
                params={"token_id": token_id},
                timeout=5,
            )
            if mid_resp.status_code == 200:
                mid = float(mid_resp.json().get("mid", 0) or 0)
                if mid > 0:
                    return mid
            clob_resp = requests.get(
                "https://clob.polymarket.com/price",
                params={"token_id": token_id, "side": "buy"},
                timeout=5,
            )
            if clob_resp.status_code == 200:
                return float(clob_resp.json().get("price", 0) or 0)
        except Exception:
            return None
        return None

    if not market_id:
        return None

    if str(market_id).isdigit():
        try:
            import json
            import requests
            gamma_resp = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=5)
            if gamma_resp.status_code != 200:
                return None
            market = gamma_resp.json()
            if isinstance(market, dict):
                ctids_raw = market.get("clobTokenIds", "")
                if ctids_raw:
                    ctids = json.loads(ctids_raw)
                    yes_token = ctids[0] if len(ctids) > 0 else None
                    if yes_token:
                        return _mid_or_buy(str(yes_token))
        except Exception:
            return None
        return None

    load_env()
    api_key = os.environ.get("SIMMER_API_KEY")
    if not api_key:
        return None
    try:
        import requests
        resp = requests.get(
            f"https://api.simmer.markets/api/sdk/context/{market_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        market = data.get("market", {})
        price = float(market.get("current_price", 0) or 0)
        token_id = market.get("polymarket_token_id") or market.get("yes_token_id")
        if token_id:
            mid = _mid_or_buy(str(token_id))
            if mid is not None:
                return mid
        if market.get("status") == "resolved" and 0.05 <= price <= 0.95:
            return None
        return price
    except Exception:
        return None


def get_positions() -> list[dict[str, Any]]:
    """
    Return all open positions merged from journal + Simmer.
    paper_trades.jsonl is the source of truth for what we've traded.
    Simmer is used to supplement with live prices for uPNL.
    """
    load_env()
    simmer_positions = get_simmer_positions()

    # Build journal positions (source of truth)
    try:
        journal_positions = [dict(p) for p in get_open_positions()]
    except Exception:
        journal_positions = []

    # Build market_id → simmer_pos map for live price enrichment
    simmer_by_mid = {str(p.get("market_id", "")): p for p in simmer_positions}

    # Use journal as the authoritative list; enrich with Simmer live prices
    out: list[dict[str, Any]] = []
    for pos in journal_positions:
        p = dict(pos)
        market_id = str(p.get("market_id", ""))
        # Try Simmer first for live price, fall back to HTTP fetch
        if market_id in simmer_by_mid:
            sp = simmer_by_mid[market_id]
            p["current_price"] = sp.get("current_price") or sp.get("price")
        else:
            p["current_price"] = _fetch_live_price(market_id) if market_id else None
        p["upnl"] = compute_upnl(p)
        p["tier"] = city_tier(p.get("location", ""))
        out.append(p)

    # If journal is empty but Simmer has positions (edge case: journal corrupt/missing),
    # fall back to Simmer as a last resort
    if not out and simmer_positions:
        for pos in simmer_positions:
            p = dict(pos)
            p["tier"] = city_tier(p.get("location", ""))
            p["upnl"] = compute_upnl(p)
            out.append(p)

    return out


def get_portfolio_stats() -> dict[str, Any]:
    try:
        stats = get_stats()
        trades = _load_trades()
        realized_pnl = float(stats.get("total_pnl", 0.0) or 0.0)

        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            pnl_24h = sum(
                float(t.get("pnl", 0) or 0)
                for t in trades
                if t.get("resolved_at")
                and datetime.fromisoformat(str(t["resolved_at"]).replace("Z", "+00:00")) > cutoff
            )
        except Exception:
            pnl_24h = 0.0

        unrealized_pnl: float | None = None
        simmer_positions = get_simmer_positions()
        if simmer_positions:
            vals = [compute_upnl(p) for p in simmer_positions]
            unrealized_pnl = round(sum(v for v in vals if v is not None), 2)
        else:
            try:
                journal_positions = get_positions()
                vals = [p.get("upnl") for p in journal_positions]
                if journal_positions and any(v is None for v in vals):
                    unrealized_pnl = None
                else:
                    unrealized_pnl = round(sum(float(v or 0) for v in vals), 2)
            except Exception:
                unrealized_pnl = None

        total_pnl = None if unrealized_pnl is None else round(realized_pnl + unrealized_pnl, 2)
        return {
            "balance": float(stats.get("paper_balance", 10000.0) or 10000.0),
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": total_pnl,
            "pnl_24h": round(float(pnl_24h), 2),
            "open_trades": int(stats.get("open_trades", 0) or 0),
        }
    except Exception:
        return {
            "balance": 10000.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": None,
            "total_pnl": None,
            "pnl_24h": 0.0,
            "open_trades": 0,
        }


def fmt_usd(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "N/A"
    if signed:
        return f"{v:+,.2f}"
    return f"{v:,.2f}"


def fmt_price(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:.2f}"


def _question_short(q: str, max_len: int = 58) -> str:
    q = (q or "Unknown").strip()
    return q if len(q) <= max_len else q[: max_len - 3] + "..."


def format_output(scan_output: str) -> str:
    portfolio = get_portfolio_stats()
    positions = get_positions()
    signals = parse_signals(scan_output)
    entries = parse_new_entries(scan_output)
    scan_stats = parse_scan_stats(scan_output)
    failures = get_failures(scan_output)

    lines: list[str] = []
    lines.append(f"**Weather Scan — {get_awst_time()}**")
    lines.append("")
    lines.append("📊 **Portfolio**")

    total_pnl = portfolio.get("total_pnl")
    unrealized = portfolio.get("unrealized_pnl")
    total_str = f"${fmt_usd(total_pnl)}" if total_pnl is not None else "N/A"
    upnl_str = f"${fmt_usd(unrealized, signed=True)}" if unrealized is not None else "N/A"

    lines.append(
        f"- Balance: ${fmt_usd(portfolio.get('balance', 0.0))} (paper) | Realized: ${fmt_usd(portfolio.get('realized_pnl', 0.0), signed=True)} | uPNL: {upnl_str} | Total: {total_str}"
    )
    lines.append(
        f"- 24h P&L: ${fmt_usd(portfolio.get('pnl_24h', 0.0), signed=True)} | Open positions: {portfolio.get('open_trades', 0)}"
    )
    lines.append("")
    lines.append("**Open Positions:**")
    if not positions:
        lines.append("- None")
    else:
        for p in positions:
            question = _question_short(p.get("question") or p.get("market_id") or "Unknown")
            strategy = str(p.get("strategy") or "core").upper()
            tier = str(p.get("tier") or "medium").upper()

            shares = p.get("shares")
            if shares in (None, 0):
                shares = p.get("shares_yes") or p.get("shares_no") or 0
            side = str(p.get("side") or ("YES" if (p.get("shares_yes") or 0) > 0 else "NO")).upper()
            entry = p.get("entry_price") or p.get("avg_price") or 0
            current = p.get("current_price")
            upnl = p.get("upnl")
            if upnl is None:
                upnl_txt = "N/A"
            else:
                upnl_txt = f"{upnl:+.2f}"
            lines.append(
                f"- [{strategy}] {question} | {side} | {float(shares):.1f} sh | entry ${fmt_price(float(entry))} | now {('N/A' if current is None else '$' + fmt_price(float(current)))} | uPNL {upnl_txt} | {tier}"
            )

    lines.append("")
    lines.append("**AIFS ENS Signals:**")
    if signals:
        for s in signals[:15]:
            lines.append(f"- {s}")
    else:
        lines.append("- None")

    lines.append("")
    lines.append("**New Entries This Scan:**")
    if entries:
        for e in entries:
            lines.append(f"- {e}")
    else:
        lines.append("- None")

    lines.append("")
    lines.append("🔍 **This Scan**")
    lines.append(f"- Markets scanned: {scan_stats['markets_scanned']}")
    lines.append(f"- Signals found: {scan_stats['signals_found']}")
    lines.append(f"- Trades executed: {scan_stats['trades_executed']}")

    if failures:
        lines.append("")
        lines.append("**Failures:**")
        for f in failures[:20]:
            lines.append(f"- {f.strip()}")

    return "\n".join(lines).strip() + "\n"


def main() -> None:
    load_env()
    scan_output = run_scan()
    output = format_output(scan_output)
    print(output, end="")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
