#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

BASE = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = BASE / "scripts"
REPORTS_DIR = BASE / "reports" / "x-daily"
AWST = ZoneInfo("Australia/Perth")
CHALLENGE_START_DATE = "2026-04-29"
CHALLENGE_TITLE = "$10,000 to $50,000 Weather Bot Challenge"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from format_scan import get_positions  # type: ignore
from paper_journal import get_stats, _load_trades  # type: ignore


def now_awst() -> datetime:
    return datetime.now(AWST)


def challenge_day(current: datetime) -> int:
    start = datetime.fromisoformat(CHALLENGE_START_DATE).replace(tzinfo=AWST)
    return ((current.date() - start.date()).days) + 1


def challenge_title(current: datetime) -> str:
    return f"{CHALLENGE_TITLE} - Day {challenge_day(current)}"


def awst_day_bounds(day: datetime | None = None) -> tuple[datetime, datetime]:
    local_now = day or now_awst()
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end


def money(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "N/A"
    return f"{value:+,.2f}" if signed else f"{value:,.2f}"


def compact_money(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.0f}" if signed else f"{value:.0f}"


def load_daily_resolved() -> dict[str, Any]:
    start, end = awst_day_bounds()
    trades = _load_trades()
    resolved = []
    for trade in trades:
        raw = trade.get("resolved_at")
        if not raw:
            continue
        try:
            resolved_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(AWST)
        except Exception:
            continue
        if start <= resolved_at < end:
            resolved.append(trade)
    pnl = round(sum(float(t.get("pnl") or 0) for t in resolved), 2)
    wins = sum(1 for t in resolved if float(t.get("pnl") or 0) > 0)
    losses = sum(1 for t in resolved if float(t.get("pnl") or 0) < 0)
    return {
        "count": len(resolved),
        "wins": wins,
        "losses": losses,
        "pnl": pnl,
    }


STOP_SUBJECT_PREFIXES = (
    "update errors.log",
    "backfill changelog",
    "update changelog",
    "chore: sync data logs",
    "prune weather skill docs",
)


def todays_commit_subjects(limit: int = 5) -> list[str]:
    start, _ = awst_day_bounds()
    cmd = [
        "git", "log",
        f"--since={start.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "--pretty=format:%s",
        "--reverse",
    ]
    res = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        return []
    out = []
    for raw in res.stdout.splitlines():
        subject = raw.strip()
        if not subject:
            continue
        lower = subject.lower()
        if any(lower.startswith(prefix) for prefix in STOP_SUBJECT_PREFIXES):
            continue
        if subject not in out:
            out.append(subject)
        if len(out) >= limit:
            break
    return out


LEAD_REWRITES = (
    ("fix:", "Fixed"),
    ("feat:", "Added"),
    ("chore:", "Updated"),
)


def normalize_update(subject: str) -> str:
    text = subject.strip()
    for prefix, replacement in LEAD_REWRITES:
        if text.lower().startswith(prefix):
            text = replacement + text[len(prefix):]
            break
    text = text[0].upper() + text[1:] if text else text
    return text.rstrip(".")


def build_update_line(subjects: list[str]) -> str:
    if not subjects:
        return "No code changes shipped today, just operation and position management."
    cleaned = [normalize_update(s) for s in subjects[:3]]
    if len(cleaned) == 1:
        return cleaned[0] + "."
    if len(cleaned) == 2:
        return f"{cleaned[0]}; {cleaned[1]}."
    return f"{cleaned[0]}; {cleaned[1]}; {cleaned[2]}."


def summarize_positions(positions: list[dict[str, Any]], limit: int = 3) -> list[str]:
    ranked = sorted(
        positions,
        key=lambda p: abs(float(p.get("upnl") or 0.0)),
        reverse=True,
    )
    lines = []
    for pos in ranked[:limit]:
        location = pos.get("location") or "Unknown"
        date = pos.get("target_date") or "?"
        side = str(pos.get("side") or "yes").upper()
        strategy = str(pos.get("strategy") or "core").upper()
        upnl = pos.get("upnl")
        lines.append(
            f"{strategy} {location} {date} {side} ({money(float(upnl), signed=True) if upnl is not None else 'N/A'})"
        )
    return lines


def build_x_post(title: str, portfolio: dict[str, Any], daily: dict[str, Any], update_line: str) -> str:
    total_pnl = portfolio.get("total_pnl")
    balance = portfolio.get("balance")
    pnl_24h = portfolio.get("pnl_24h")
    open_trades = portfolio.get("open_trades")
    parts = [
        title,
        "",
        "Daily weather bot snapshot.",
        f"Paper balance: ${money(balance)}",
        f"24h P&L: ${money(pnl_24h, signed=True)}",
        f"Total P&L: ${money(total_pnl, signed=True) if total_pnl is not None else 'N/A'}",
        f"Open positions: {open_trades}",
    ]
    if daily["count"]:
        parts.append(f"Closed today: {daily['count']} ({daily['wins']}W/{daily['losses']}L)")
    parts.append(f"Today: {update_line}")
    parts.append("Paper trading, not live capital.")
    return "\n".join(parts)


def build_x_reply(portfolio: dict[str, Any], stats: dict[str, Any], daily: dict[str, Any], positions: list[dict[str, Any]]) -> str:
    realized = portfolio.get("realized_pnl")
    unrealized = portfolio.get("unrealized_pnl")
    lines = [
        f"Breakout: realized ${money(realized, signed=True)}, unrealized ${money(unrealized, signed=True) if unrealized is not None else 'N/A'}, win rate {stats.get('win_rate') if stats.get('win_rate') is not None else 'N/A'}%.",
    ]
    if daily["count"]:
        lines.append(f"Resolved today: {daily['count']} trades for ${money(daily['pnl'], signed=True)}.")
    top = summarize_positions(positions)
    if top:
        lines.append("Biggest open swings: " + " | ".join(top) + ".")
    return "\n".join(lines)


def build_portfolio_snapshot(positions: list[dict[str, Any]], stats: dict[str, Any]) -> dict[str, Any]:
    trades = _load_trades()
    realized_pnl = round(float(stats.get("total_pnl", 0.0) or 0.0), 2)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    pnl_24h = 0.0
    for trade in trades:
        raw = trade.get("resolved_at")
        if not raw:
            continue
        try:
            resolved_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            continue
        if resolved_at > cutoff:
            pnl_24h += float(trade.get("pnl", 0) or 0)

    upnls: list[float] = []
    has_missing_upnl = False
    for pos in positions:
        upnl = pos.get("upnl")
        if upnl is None:
            has_missing_upnl = True
            continue
        upnls.append(float(upnl))

    unrealized = None if has_missing_upnl and not upnls else round(sum(upnls), 2)
    total_pnl = None if unrealized is None else round(realized_pnl + unrealized, 2)
    return {
        "balance": round(10000.0 + realized_pnl, 2),
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized,
        "total_pnl": total_pnl,
        "pnl_24h": round(pnl_24h, 2),
        "open_trades": int(stats.get("open_trades", 0) or 0),
    }


def build_markdown(snapshot: dict[str, Any]) -> str:
    p = snapshot["portfolio"]
    stats = snapshot["stats"]
    daily = snapshot["daily_resolved"]
    lines = [
        f"# Daily X Draft — {snapshot['snapshot_date']}",
        f"## {snapshot['challenge_title']}",
        "",
        f"Generated: {snapshot['generated_at_awst']}",
        "",
        "## Snapshot",
        f"- Paper balance: ${money(p.get('balance'))}",
        f"- Realized P&L: ${money(p.get('realized_pnl'), signed=True)}",
        f"- Unrealized P&L: ${money(p.get('unrealized_pnl'), signed=True) if p.get('unrealized_pnl') is not None else 'N/A'}",
        f"- Total P&L: ${money(p.get('total_pnl'), signed=True) if p.get('total_pnl') is not None else 'N/A'}",
        f"- 24h P&L: ${money(p.get('pnl_24h'), signed=True)}",
        f"- Open positions: {p.get('open_trades')}",
        f"- Resolved today: {daily['count']} ({daily['wins']}W/{daily['losses']}L) for ${money(daily['pnl'], signed=True)}",
        f"- Lifetime win rate: {stats.get('win_rate') if stats.get('win_rate') is not None else 'N/A'}%",
        "",
        "## Progress updates",
    ]
    updates = snapshot["progress_updates"] or ["No meaningful code updates captured today."]
    lines.extend(f"- {u}" for u in updates)
    lines.extend([
        "",
        "## X draft",
        snapshot["x_post"],
        "",
        "## Reply draft",
        snapshot["x_reply"],
    ])
    if snapshot["top_open_positions"]:
        lines.extend([
            "",
            "## Biggest open swings",
            *[f"- {line}" for line in snapshot["top_open_positions"]],
        ])
    return "\n".join(lines) + "\n"


def generate_snapshot() -> dict[str, Any]:
    current = now_awst()
    stats = get_stats()
    positions = get_positions()
    portfolio = build_portfolio_snapshot(positions, stats)
    daily = load_daily_resolved()
    updates = todays_commit_subjects()
    update_line = build_update_line(updates)
    title = challenge_title(current)
    snapshot = {
        "snapshot_date": current.strftime("%Y-%m-%d"),
        "generated_at_awst": current.strftime("%Y-%m-%d %I:%M %p AWST").replace(" 0", " "),
        "challenge_title": title,
        "challenge_day": challenge_day(current),
        "portfolio": portfolio,
        "stats": stats,
        "daily_resolved": daily,
        "progress_updates": updates,
        "top_open_positions": summarize_positions(positions),
        "x_post": build_x_post(title, portfolio, daily, update_line),
        "x_reply": build_x_reply(portfolio, stats, daily, positions),
    }
    snapshot["markdown"] = build_markdown(snapshot)
    return snapshot


def save_snapshot(snapshot: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = snapshot["snapshot_date"]
    json_path = REPORTS_DIR / f"{stamp}.json"
    md_path = REPORTS_DIR / f"{stamp}.md"
    latest_json = REPORTS_DIR / "latest.json"
    latest_md = REPORTS_DIR / "latest.md"
    json_text = json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"
    md_text = snapshot["markdown"]
    json_path.write_text(json_text)
    md_path.write_text(md_text)
    latest_json.write_text(json_text)
    latest_md.write_text(md_text)
    return json_path, md_path, latest_json, latest_md


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a daily X draft from paper-trading performance.")
    parser.add_argument("--draft-only", action="store_true", help="Print only the markdown draft without save-path footer.")
    args = parser.parse_args()

    snapshot = generate_snapshot()
    json_path, md_path, latest_json, latest_md = save_snapshot(snapshot)
    print(snapshot["markdown"].strip())
    if not args.draft_only:
        print("")
        print(f"Saved: {json_path}")
        print(f"Saved: {md_path}")
        print(f"Updated: {latest_json}")
        print(f"Updated: {latest_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
