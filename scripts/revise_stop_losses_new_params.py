#!/usr/bin/env python3
"""Replay exact-core stop-loss exits under current PM params and update paper journal.

One-off maintenance script for the May 2026 exact-core market-collapse guard.
Creates timestamped backups before modifying JSONL logs.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRADE_PATH = ROOT / "data" / "paper_trades.jsonl"
ACTIONS_PATH = ROOT / "data" / "manager_actions.jsonl"
LOSSES_PATH = ROOT / "data" / "losses.log"

FLOOR = 0.12
DROP = 0.65
WEAK_FLOOR = 0.08
ENTRY_FRAC = 0.50
WEAK_EDGE = -1.5
START_HOUR = 10
CORPSE_FLOOR = 0.05
CORPSE_ENTRY_FRAC = 0.35
TRADE_ID_DONE = "paper_149e8e76-93e0-43_1778405208"

STOP_REASONS = (
    "corpse_price_guard",
    "exact_core_weak_price_guard",
    "exact_core_market_collapse_guard",
    "repricing_guard_collapse",
    "trailing_stop",
)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def exact_bucket(label: str | None) -> bool:
    if not label:
        return False
    s = str(label).strip()
    if "or" in s.lower() or "-" in s:
        return False
    return bool(re.fullmatch(r"\d+(?:\.\d+)?\s*[°º]?[CF]", s))


def stopish(reason: str | None) -> bool:
    r = str(reason or "").lower()
    return any(part in r for part in STOP_REASONS)


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def dump_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows) + "\n")


def first_new_exit(trade: dict, actions: list[dict]) -> dict | None:
    entry = float(trade.get("entry_price") or 0.0)
    resolved_at = parse_dt(trade.get("resolved_at"))
    peak: float | None = None

    for action in sorted(actions, key=lambda a: parse_dt(a.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)):
        ts = parse_dt(action.get("ts"))
        if resolved_at and ts and ts > resolved_at:
            continue

        price = action.get("current_price")
        if price is None:
            price = action.get("exit_price")
        if price is None:
            continue
        price = float(price)
        if price <= 0:
            continue

        peak = price if peak is None else max(peak, price)

        hour = int(action.get("local_hour") if action.get("local_hour") is not None else -1)
        age = float(action.get("age_min") if action.get("age_min") is not None else 999999)
        if hour < START_HOUR or age < 45:
            continue

        edge_raw = action.get("edge_c_running")
        edge = float(edge_raw) if edge_raw is not None else None
        running = action.get("running_c")
        projected = action.get("projected_c")

        reason = None
        if price <= WEAK_FLOOR and (entry <= 0 or price <= entry * ENTRY_FRAC) and edge is not None and edge <= WEAK_EDGE:
            reason = (
                f"exact_core_weak_price_guard (price=${price:.3f} <= floor=${WEAK_FLOOR:.3f}, "
                f"entry=${entry:.3f}, entry_frac={ENTRY_FRAC:.2f}, "
                f"running_edge={edge:.2f}°C <= {WEAK_EDGE:.2f}°C"
            )
            if running is not None and projected is not None:
                reason += f", running={float(running):.2f}°C, projected={float(projected):.2f}°C"
            reason += ")"
        elif price <= CORPSE_FLOOR and (entry <= 0 or price <= entry * CORPSE_ENTRY_FRAC):
            reason = (
                f"corpse_price_guard (price=${price:.3f} <= floor=${CORPSE_FLOOR:.3f}, "
                f"entry=${entry:.3f}, entry_frac={CORPSE_ENTRY_FRAC:.2f}"
            )
            if running is not None and projected is not None:
                reason += f", running={float(running):.2f}°C, projected={float(projected):.2f}°C"
            reason += ")"
        elif price <= FLOOR and (entry <= 0 or price <= entry * ENTRY_FRAC):
            drawdown = (1.0 - price / peak) if peak and peak > 0 else None
            if drawdown is None or drawdown >= DROP:
                reason = (
                    f"exact_core_market_collapse_guard (price=${price:.3f} <= floor=${FLOOR:.3f}, "
                    f"entry=${entry:.3f}, entry_frac={ENTRY_FRAC:.2f}"
                )
                if peak is not None and drawdown is not None:
                    reason += f", peak=${peak:.3f} drawdown={drawdown * 100:.1f}% >= {DROP * 100:.1f}%"
                if running is not None and projected is not None:
                    reason += f", running={float(running):.2f}°C, projected={float(projected):.2f}°C"
                reason += ")"

        if reason:
            return {
                "ts": action.get("ts"),
                "price": price,
                "reason": reason,
                "source_action": action,
                "peak": peak,
                "drawdown": (1.0 - price / peak) if peak and peak > 0 else None,
            }
    return None


def main() -> None:
    trades = load_jsonl(TRADE_PATH)
    losses = load_jsonl(LOSSES_PATH)
    actions = load_jsonl(ACTIONS_PATH)

    action_by_trade: dict[str, list[dict]] = {}
    for row in actions:
        action_by_trade.setdefault(row.get("trade_id"), []).append(row)

    revisions: dict[str, dict] = {}
    for trade in trades:
        tid = trade.get("trade_id")
        if trade.get("status") != "resolved" or not stopish(trade.get("exit_reason")):
            continue
        is_exact_core = exact_bucket(trade.get("bucket")) and (
            str(trade.get("strategy") or "").lower() in {"core", "carveout"}
            or trade.get("core_low_edge_exact_carveout") is True
        )
        if not is_exact_core:
            continue
        replay = first_new_exit(trade, action_by_trade.get(tid, []))
        if not replay:
            continue
        same_price = abs(float(trade.get("exit_price") or 0.0) - replay["price"]) < 1e-9
        same_ts = str(trade.get("resolved_at")) == str(replay["ts"])
        same_reason_family = str(trade.get("exit_reason") or "").split(" ", 1)[0] == replay["reason"].split(" ", 1)[0]
        if same_price and same_ts and same_reason_family:
            continue
        revisions[tid] = replay

    if not revisions:
        print(json.dumps({"updated": 0, "revisions": []}, indent=2))
        return

    backup_dir = ROOT / "BACKUPS" / f"stop-loss-revision-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for path in (TRADE_PATH, LOSSES_PATH, ACTIONS_PATH):
        if path.exists():
            shutil.copy2(path, backup_dir / path.name)

    summary = []
    trade_lookup = {t.get("trade_id"): t for t in trades}

    for trade in trades:
        tid = trade.get("trade_id")
        replay = revisions.get(tid)
        if not replay:
            continue
        shares = float(trade.get("shares") or 0.0)
        cost = float(trade.get("cost") or shares * float(trade.get("entry_price") or 0.0))
        realized = float(trade.get("realized_pnl") or 0.0)
        old_pnl = float(trade.get("pnl") or 0.0)
        new_pnl = round(shares * replay["price"] - cost + realized, 4)
        summary.append({
            "trade_id": tid,
            "location": trade.get("location"),
            "target_date": trade.get("target_date"),
            "bucket": trade.get("bucket"),
            "old_exit_price": trade.get("exit_price"),
            "new_exit_price": replay["price"],
            "old_pnl": old_pnl,
            "new_pnl": new_pnl,
            "delta": round(new_pnl - old_pnl, 4),
            "old_resolved_at": trade.get("resolved_at"),
            "new_resolved_at": replay["ts"],
            "new_reason": replay["reason"],
        })
        trade["exit_price"] = replay["price"]
        trade["pnl"] = new_pnl
        trade["resolved_at"] = replay["ts"]
        trade["resolution_source"] = "early_exit_position_manager"
        trade["exit_reason"] = replay["reason"]
        trade["stop_loss_revised_at"] = datetime.now(timezone.utc).isoformat()
        trade["stop_loss_revision_note"] = "counterfactual replay under exact-core market-collapse params"

    for loss in losses:
        tid = loss.get("trade_id")
        replay = revisions.get(tid)
        if not replay:
            continue
        trade = trade_lookup[tid]
        shares = float(loss.get("shares") or trade.get("shares") or 0.0)
        cost = float(loss.get("cost") or trade.get("cost") or 0.0)
        realized = float(trade.get("realized_pnl") or 0.0)
        loss["ts"] = replay["ts"]
        loss["exit_price"] = replay["price"]
        loss["pnl"] = round(shares * replay["price"] - cost + realized, 4)
        loss["resolved_at"] = replay["ts"]
        loss["exit_reason"] = replay["reason"]
        loss["stop_loss_revised_at"] = datetime.now(timezone.utc).isoformat()

    for action in actions:
        tid = action.get("trade_id")
        replay = revisions.get(tid)
        if not replay:
            continue
        ts = action.get("ts")
        old_resolved = next(s["old_resolved_at"] for s in summary if s["trade_id"] == tid)
        if ts == replay["ts"]:
            new_pnl = next(s["new_pnl"] for s in summary if s["trade_id"] == tid)
            action["action"] = "exit"
            action["reason"] = replay["reason"]
            action["exit_price"] = replay["price"]
            action["applied"] = True
            action["pnl"] = new_pnl
            if replay.get("drawdown") is not None:
                action["peak_drawdown_frac"] = round(replay["drawdown"], 4)
            if replay.get("peak") is not None:
                action["peak_seen_price"] = round(replay["peak"], 4)
            action["stop_loss_revised_at"] = datetime.now(timezone.utc).isoformat()
        elif ts == old_resolved and ts != replay["ts"] and action.get("action") == "exit":
            action["action"] = "hold"
            action["applied"] = False
            action["superseded_by"] = "manual_stop_loss_replay_new_params"
            action["reason"] = "superseded_by_earlier_stop_loss_replay"
            action.pop("exit_price", None)
            action.pop("pnl", None)

    dump_jsonl(TRADE_PATH, trades)
    dump_jsonl(LOSSES_PATH, losses)
    dump_jsonl(ACTIONS_PATH, actions)
    print(json.dumps({"updated": len(summary), "backup_dir": str(backup_dir.relative_to(ROOT)), "revisions": summary}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
