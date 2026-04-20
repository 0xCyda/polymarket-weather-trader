#!/usr/bin/env python3.12
"""
Weather Scan Dashboard — Polymarket AIFS ENS Trader

Standalone FastAPI dashboard for the polymarket-weather-trader skill.
Accessible at http://127.0.0.1:8414/

Run:
    python3.12 dashboard.py
"""

from __future__ import annotations

import json
import os
import sys
import math
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# Add scripts/ to path for paper_journal
_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE / "scripts"))
from paper_journal import get_stats, get_open_positions, get_resolved_trades, _load_trades

SKILL_DIR = _BASE
DATA_DIR = SKILL_DIR / "data"
SCAN_LOG = DATA_DIR / "forecast_history.jsonl"
PAPER_TRADES = DATA_DIR / "paper_trades.jsonl"
CONFIG_FILE = SKILL_DIR / "config.json"

_MONTH_NAMES = {
    "01": "january", "02": "february", "03": "march", "04": "april",
    "05": "may", "06": "june", "07": "july", "08": "august",
    "09": "september", "10": "october", "11": "november", "12": "december",
}


def polymarket_event_url(location: str, target_date: str, metric: str) -> str | None:
    """
    Build a Polymarket event slug URL.

    Example: ("Wellington", "2026-04-20", "high") →
      https://polymarket.com/event/highest-temperature-in-wellington-on-april-20-2026
    """
    if not location or not target_date:
        return None
    try:
        y, m, d = target_date.split("-")
        month = _MONTH_NAMES[m]
    except (ValueError, KeyError):
        return None
    metric_word = "highest" if (metric or "high").lower().startswith("h") else "lowest"
    loc_slug = (location or "").lower().strip().replace(" ", "-")
    if not loc_slug:
        return None
    return f"https://polymarket.com/event/{metric_word}-temperature-in-{loc_slug}-on-{month}-{int(d)}-{y}"

DASHBOARD_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AIFS ENS Weather Scan Dashboard</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root { color-scheme: dark; }
    body { font-family: Inter, system-ui, sans-serif; background:#080e1a; color:#d0ddef; margin:0; padding:24px; }
    h1, h2 { margin:0 0 12px; color:#e8edf7; }
    .muted { color:#7a89a8; font-size:13px; }
    .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap:14px; margin:18px 0 24px; }
    .card { background:#0f1a2e; border:1px solid #1e3054; border-radius:14px; padding:16px; box-shadow:0 8px 24px rgba(0,0,0,.25); }
    .label { font-size:11px; text-transform:uppercase; letter-spacing:.08em; color:#6070a0; margin-bottom:6px; }
    .value { font-size:26px; font-weight:700; }
    .layout { display:grid; grid-template-columns: 2fr 1fr; gap:20px; }
    .layout2 { display:grid; grid-template-columns: 1fr 1fr; gap:20px; margin-top:20px; }
    .table-wrap { overflow:auto; }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    th, td { text-align:left; padding:10px 8px; border-bottom:1px solid #1a2840; white-space:nowrap; }
    th { color:#5060a0; font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.06em; }
    .pill { display:inline-block; padding:4px 10px; border-radius:999px; font-size:12px; background:#141e30; color:#6070a0; border:1px solid #1e3054; }
    .good { color:#34d399; }
    .bad  { color:#f87171; }
    .neutral { color:#94a3b8; }
    .med { color:#fbbf24; }
    .badge { display:inline-block; padding:2px 7px; border-radius:4px; font-size:11px; font-weight:600; }
    .badge-yes { background:#0d3320; color:#34d399; }
    .badge-no  { background:#2d1010; color:#f87171; }
    .badge-open  { background:#1a2540; color:#60a5fa; }
    .badge-resolved { background:#1a2020; color:#34d399; }
    .badge-loss  { background:#2d1010; color:#f87171; }
    .badge-none { background:#1a1a1a; color:#606060; }
    .signal-str { padding:2px 8px; border-radius:999px; font-size:11px; font-weight:600; }
    .sig-strong { background:#0d3320; color:#34d399; }
    .sig-moderate { background:#2a1f00; color:#fbbf24; }
    .sig-weak { background:#1a1a2a; color:#94a3b8; }
    @media (max-width: 960px) { .layout, .layout2 { grid-template-columns: 1fr; } }
    .refresh-status { font-size:11px; color:#3a4a60; margin-top:4px; }
    .sep { border:none; border-top:1px solid #1a2840; margin:20px 0; }
    .section-title { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:#5060a0; margin:0 0 12px; }
  </style>
</head>
<body>

<div style="display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap;">
  <div>
    <h1>AIFS ENS Weather Scan</h1>
    <div class="muted">Paper trading dashboard — Polymarket weather markets</div>
  </div>
  <div style="text-align:right;">
    <div class="pill" id="refresh-status">Refreshing every 30m</div>
    <div class="refresh-status">Last updated: <span id="last-updated">—</span></div>
  </div>
</div>

<div class="grid" id="summary-cards"></div>

<div class="layout">
  <div class="card">
    <h2>Equity Curve</h2>
    <div id="equity-chart" style="height:320px"></div>
  </div>
  <div class="card">
    <h2>P&L Breakdown</h2>
    <div id="pnl-breakdown"></div>
  </div>
</div>

<div class="layout2">
  <div class="card table-wrap">
    <h2>Open Positions</h2>
    <table id="positions-table"></table>
  </div>
  <div class="card table-wrap">
    <h2>AIFS ENS Signals</h2>
    <div id="signals-list" style="font-size:13px;"></div>
  </div>
</div>

<div class="card table-wrap" style="margin-top:20px">
  <h2>Recent Resolved Trades</h2>
  <table id="resolved-table"></table>
</div>

<script>
const API_BASE = '';
let lastUpdated = null;

async function loadJson(path) {
  const r = await fetch(API_BASE + path, {cache: 'no-store'});
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function money(v) {
  const n = Number(v || 0);
  return '$' + n.toFixed(2);
}

function fmtPnl(v) {
  const n = Number(v || 0);
  if (n === 0) return '<span class="neutral">$0.00</span>';
  return n > 0
    ? `<span class="good">+$${n.toFixed(2)}</span>`
    : `<span class="bad">-$${Math.abs(n).toFixed(2)}</span>`;
}

function card(label, value, cls) {
  return `<div class="card"><div class="label">${label}</div><div class="value ${cls||''}">${value}</div></div>`;
}

function signalBadge(s) {
  const cls = s === 'strong' ? 'sig-strong' : s === 'moderate' ? 'sig-moderate' : 'sig-weak';
  return `<span class="signal-str ${cls}">${s}</span>`;
}

function positionBadge(side) {
  const cls = side === 'YES' ? 'badge-yes' : 'badge-no';
  return `<span class="badge ${cls}">${side}</span>`;
}

function winBadge(pnl) {
  const n = Number(pnl || 0);
  if (n > 0) return `<span class="badge badge-resolved">+$${n.toFixed(2)}</span>`;
  if (n < 0) return `<span class="badge badge-loss">-$${Math.abs(n).toFixed(2)}</span>`;
  return `<span class="badge badge-none">$0.00</span>`;
}

function renderCards(d) {
  const equity = Number(d.portfolio.realized_pnl) + Number(d.portfolio.unrealized_pnl);
  document.getElementById('summary-cards').innerHTML = [
    card('Balance', money(d.portfolio.balance)),
    card('Realized P&L', fmtPnl(d.portfolio.realized_pnl)),
    card('Unrealized P&L', fmtPnl(d.portfolio.unrealized_pnl)),
    card('Total P&L', fmtPnl(equity)),
    card('Win Rate', d.stats.win_rate != null ? d.stats.win_rate + '%' : '—'),
    card('Open Positions', String(d.stats.open_trades)),
    card('Resolved Trades', String(d.stats.resolved_trades)),
    card('Total Trades', String(d.stats.total_trades)),
  ].join('');
}

function renderChart(d) {
  const x = d.timeseries.map(r => r.date);
  const y = d.timeseries.map(r => r.cumulative_pnl);
  if (!x.length) {
    document.getElementById('equity-chart').innerHTML = '<div class="muted" style="padding:40px;text-align:center">No equity data yet</div>';
    return;
  }
  const data = [{x, y, type:'scatter', mode:'lines+markers', marker:{color:'#60a5fa', size:5}, line:{color:'#3b82f6',width:2}}];
  const layout = {
    paper_bgcolor:'#0f1a2e', plot_bgcolor:'#0f1a2e', margin:{l:50,r:10,t:10,b:40},
    xaxis:{color:'#6070a0', gridcolor:'#1a2840', tickfont:{color:'#6070a0'}},
    yaxis:{color:'#6070a0', gridcolor:'#1a2840', tickfont:{color:'#6070a0'}},
  };
  Plotly.newPlot('equity-chart', data, layout, {displayModeBar:false, responsive:true});
}

function renderBreakdown(d) {
  const s = d.stats;
  const sections = [
    ['Total Trades', String(s.total_trades)],
    ['Resolved', String(s.resolved_trades)],
    ['Open', String(s.open_trades)],
    ['Wins', String(s.wins)],
    ['Losses', String(s.losses)],
    ['Win Rate', s.win_rate != null ? s.win_rate + '%' : '—'],
    ['Total P&L', fmtPnl(s.total_pnl)],
    ['Avg P&L', money(s.avg_pnl)],
    ['Best Trade', money(s.best_trade)],
    ['Worst Trade', money(s.worst_trade)],
  ];
  document.getElementById('pnl-breakdown').innerHTML = sections.map(([k,v]) =>
    `<div style="display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid #1a2840;font-size:13px;">
       <span style="color:#6070a0">${k}</span>${v}
     </div>`
  ).join('');
}

function renderPositions(d) {
  if (!d.positions.length) {
    document.getElementById('positions-table').innerHTML = '<tr><td colspan="8" class="muted" style="padding:20px;text-align:center">No open positions</td></tr>';
    return;
  }
  const headers = ['Market', 'Side', 'Shares', 'Entry', 'Current', 'uPNL', 'Resolve Date', 'Market ID'];
  const rows = d.positions.map(p => {
    const q = p.question || '—';
    const side = p.side ? p.side.toUpperCase() : 'YES';
    const upnl = Number(p.upnl || 0);
    const upnlStr = upnl > 0 ? `<span class="good">+$${upnl.toFixed(2)}</span>` : upnl < 0 ? `<span class="bad">-$${Math.abs(upnl).toFixed(2)}</span>` : '$0.00';
    const marketCell = p.polymarket_url
      ? `<a href="${p.polymarket_url}" target="_blank" rel="noopener" style="color:#8ab4ff;text-decoration:none" title="Open on Polymarket">${q} ↗</a>`
      : q;
    return [
      marketCell,
      positionBadge(side),
      (p.shares || 0).toFixed(1),
      '$' + (p.entry_price || 0).toFixed(3),
      '$' + (p.current_price || 0).toFixed(3),
      upnlStr,
      (p.target_date || '—').substring(0, 10),
      p.market_id ? `<span style="color:#6070a0;font-size:11px">${p.market_id.substring(0, 8)}…</span>` : '—',
    ];
  });
  document.getElementById('positions-table').innerHTML =
    `<thead><tr>${headers.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>` +
    rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('') +
    '</tbody>';
}

function renderSignals(d) {
  if (!d.signals.length) {
    document.getElementById('signals-list').innerHTML = '<div class="muted" style="padding:16px;text-align:center">No signals in latest scan</div>';
    return;
  }
  document.getElementById('signals-list').innerHTML = d.signals.slice(0, 15).map(s => `
    <div style="padding:9px 0;border-bottom:1px solid #1a2840;">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <span style="font-weight:600;color:#d0ddef">${s.location}</span>
        <span style="color:#6070a0;font-size:12px">${s.date}</span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px;">
        <span style="color:#94a3b8">${s.temp}°F · ${s.metric}</span>
        ${signalBadge(s.signal)}
      </div>
      <div style="color:#3a4a60;font-size:11px;margin-top:3px">${s.models} models · spread ${s.spread}°${s.agree !== 'N/A' ? ' · agree ' + s.agree + '%' : ''}</div>
    </div>
  `).join('');
}

function renderResolved(d) {
  if (!d.resolved.length) {
    document.getElementById('resolved-table').innerHTML = '<tr><td colspan="7" class="muted" style="padding:20px;text-align:center">No resolved trades yet</td></tr>';
    return;
  }
  const headers = ['Location', 'Side', 'Entry', 'Exit', 'Shares', 'P&L', 'Resolved'];
  const rows = d.resolved.map(t => {
    const exit = Number(t.exit_price || 0);
    const entry = Number(t.entry_price || 0);
    const shares = Number(t.shares || 0);
    const pnl = Number(t.pnl || 0);
    const outcome = t.outcome ? t.outcome.toUpperCase() : (exit > 0.5 ? 'YES' : 'NO');
    const resolvedDate = t.resolved_at ? t.resolved_at.substring(0, 10) : (t.resolution_date || '—');
    const locName = (t.location || '—').substring(0, 30);
    const locCell = t.polymarket_url
      ? `<a href="${t.polymarket_url}" target="_blank" rel="noopener" style="color:#8ab4ff;text-decoration:none" title="Open on Polymarket">${locName} ↗</a>`
      : locName;
    return [
      locCell,
      positionBadge(outcome),
      '$' + entry.toFixed(3),
      '$' + exit.toFixed(3),
      shares.toFixed(1),
      winBadge(pnl),
      resolvedDate.substring(0, 10),
    ];
  });
  document.getElementById('resolved-table').innerHTML =
    `<thead><tr>${headers.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>` +
    rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('') +
    '</tbody>';
}

async function refresh() {
  try {
    const [state] = await Promise.all([loadJson('/api/state')]);
    renderCards(state);
    renderChart(state);
    renderBreakdown(state);
    renderPositions(state);
    renderSignals(state);
    renderResolved(state);
    const now = new Date();
    document.getElementById('last-updated').textContent = now.toLocaleTimeString('en-AU', {timeZone:'Australia/Perth'}) + ' AWST';
    document.getElementById('refresh-status').textContent = 'Refreshing every 30m';
  } catch(e) {
    document.getElementById('refresh-status').textContent = 'Refresh failed: ' + e.message;
  }
}

refresh();
setInterval(refresh, 1800000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Env loading (must happen before API calls)
# ---------------------------------------------------------------------------

def _load_env():
    """Load .env from skill directory into os.environ."""
    env_file = SKILL_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

_load_env()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_trades_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _build_timeseries(trades: list[dict]) -> list[dict]:
    """Cumulative P&L over time from resolved trades."""
    rows = []
    cumulative = 0.0
    for t in trades:
        if t.get("status") == "resolved" and t.get("pnl") is not None:
            cumulative += float(t["pnl"])
            ts = t.get("resolved_at", "")[:10] if t.get("resolved_at") else t.get("entered_at", "")[:10]
            rows.append({"date": ts, "cumulative_pnl": round(cumulative, 4)})
    return rows


def _get_simmer_positions() -> list[dict]:
    """Fetch open positions from Simmer API with live prices."""
    try:
        api_key = os.environ.get("SIMMER_API_KEY")
        if not api_key:
            return []
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


def _fetch_live_price(market_id: str) -> float | None:
    """Fetch current price for a market from Simmer context API."""
    try:
        api_key = os.environ.get("SIMMER_API_KEY")
        if not api_key:
            return None
        import requests
        resp = requests.get(
            f"https://api.simmer.markets/api/sdk/context/{market_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if isinstance(data, dict):
            return float(data.get("market", {}).get("current_price", 0) or 0)
        return None
    except Exception:
        return None


def _compute_upnl(pos: dict) -> float:
    shares_yes = float(pos.get("shares_yes") or 0)
    shares_no = float(pos.get("shares_no") or 0)
    entry = float(pos.get("entry_price") or 0)
    current = float(pos.get("current_price") or 0)
    if shares_yes > 0:
        return round((current - entry) * shares_yes, 2)
    elif shares_no > 0:
        return round((entry - current) * shares_no, 2)
    # Paper journal fallback
    shares = float(pos.get("shares") or 0)
    if shares > 0 and entry > 0:
        return round((current - entry) * shares, 2)
    return 0.0


def _enrich_positions(positions: list[dict]) -> list[dict]:
    """Add current_price and upnl to paper journal positions concurrently."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    api_key = os.environ.get("SIMMER_API_KEY")

    def enrich_one(p: dict) -> dict:
        p = dict(p)
        market_id = p.get("market_id", "")
        if market_id and api_key:
            cp = _fetch_live_price(market_id)
            if cp is not None:
                p["current_price"] = cp
                p["upnl"] = _compute_upnl(p)
                return p
        p["current_price"] = 0.0
        p["upnl"] = 0.0
        return p

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(enrich_one, p): p for p in positions}
        enriched = []
        for future in as_completed(futures, timeout=40):
            try:
                enriched.append(future.result())
            except Exception:
                p = futures[future]
                p["current_price"] = 0.0
                p["upnl"] = 0.0
                enriched.append(p)
    return enriched


def _parse_signals_from_history() -> list[dict]:
    """
    Read the most recent entries from forecast_history.jsonl — each line is a
    per-city AIFS ENS forecast, effectively a detected signal. Return the latest 15.
    """
    history = _load_trades_jsonl(SCAN_LOG)
    if not history:
        return []
    # Take the last 15 entries, reverse so most recent is first
    entries = history[-15:] if len(history) >= 15 else history
    signals = []
    for e in reversed(entries):
        if not isinstance(e, dict):
            continue
        loc = e.get("location", "")
        date = e.get("target_date", "")
        metric = e.get("metric", "")
        temp = e.get("forecast_temp", "")
        signal = e.get("signal_strength", "")
        models = e.get("models_used", "")
        agree = e.get("agreement_pct", "")
        spread = e.get("spread", "")
        if loc and e.get("signal_strength"):
            signals.append({
                "location": loc,
                "date": date,
                "metric": metric,
                "temp": str(temp) if temp else "—",
                "signal": signal,
                "models": str(models) if models else "—",
                "agree": str(round(float(agree), 1)) + "%" if agree not in ("", None) else "N/A",
                "spread": str(spread) if spread not in ("", None) else "—",
            })
    return signals


def _get_portfolio_stats(enriched_positions: list[dict]) -> dict:
    """Compute balance, realized, unrealized P&L using pre-enriched positions."""
    trades = _load_trades_jsonl(PAPER_TRADES)
    resolved = [t for t in trades if t.get("status") == "resolved"]
    realized = round(sum(float(t.get("pnl") or 0) for t in resolved), 4)
    unrealized = round(sum(float(p.get("upnl") or 0) for p in enriched_positions), 2)
    return {
        "balance": 10000.0,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
    }


def _get_stats() -> dict:
    trades = _load_trades_jsonl(PAPER_TRADES)
    resolved = [t for t in trades if t.get("status") == "resolved"]
    open_trades = [t for t in trades if t.get("status") == "open"]
    if not resolved:
        return dict(total_trades=len(trades), open_trades=len(open_trades),
                    resolved_trades=0, wins=0, losses=0, win_rate=None,
                    total_pnl=0.0, avg_pnl=0.0, best_trade=None, worst_trade=None)
    pnls = [float(t.get("pnl") or 0) for t in resolved]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return dict(
        total_trades=len(trades),
        open_trades=len(open_trades),
        resolved_trades=len(resolved),
        wins=len(wins),
        losses=len(losses),
        win_rate=round(len(wins) / len(resolved) * 100, 1) if resolved else None,
        total_pnl=round(sum(pnls), 4),
        avg_pnl=round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
        best_trade=max(pnls) if pnls else None,
        worst_trade=min(pnls) if pnls else None,
    )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="AIFS ENS Weather Scan Dashboard")

@app.get("/", response_class=HTMLResponse)
def home():
    return DASHBOARD_HTML


@app.get("/api/state")
def api_state():
    trades = _load_trades_jsonl(PAPER_TRADES)
    resolved = [t for t in trades if t.get("status") == "resolved"]
    open_trades = [t for t in trades if t.get("status") == "open"]
    simmer_pos = _get_simmer_positions()

    if simmer_pos:
        positions = []
        for p in simmer_pos:
            q = p.get("question") or ""
            loc = q.split(" in ")[1].split(" on ")[0] if " in " in q else ""
            tgt = p.get("end_date_utc", "")[:10] if p.get("end_date_utc") else ""
            metric = "high" if "highest" in q.lower() else ("low" if "lowest" in q.lower() else "high")
            positions.append({
                "question": q,
                "side": "YES" if float(p.get("shares_yes") or 0) > 0 else "NO",
                "shares": float(p.get("shares_yes") or p.get("shares_no") or 0),
                "entry_price": float(p.get("entry_price") or 0),
                "current_price": float(p.get("current_price") or 0),
                "upnl": _compute_upnl(p),
                "target_date": tgt,
                "location": loc,
                "polymarket_url": polymarket_event_url(loc, tgt, metric),
            })
    else:
        enriched = _enrich_positions(open_trades)
        positions = []
        for p in enriched:
            loc = p.get("location", "")
            tgt = p.get("target_date", "")
            metric = p.get("metric") or "high"
            positions.append({
                "question": p.get("question", ""),
                "side": p.get("side", "YES").upper(),
                "shares": float(p.get("shares") or 0),
                "entry_price": float(p.get("entry_price") or 0),
                "current_price": float(p.get("current_price") or 0),
                "upnl": float(p.get("upnl") or 0),
                "target_date": tgt,
                "location": loc,
                "polymarket_url": polymarket_event_url(loc, tgt, metric),
            })

    return JSONResponse({
        "portfolio": _get_portfolio_stats(positions),
        "stats": _get_stats(),
        "timeseries": _build_timeseries(trades),
        "positions": positions,
        "signals": _parse_signals_from_history(),
        "resolved": [
            {
                "question": t.get("question", ""),
                "location": t.get("location", ""),
                "side": t.get("side", "YES").upper(),
                "entry_price": float(t.get("entry_price") or 0),
                "exit_price": float(t.get("exit_price") or 0),
                "shares": float(t.get("shares") or 0),
                "pnl": float(t.get("pnl") or 0),
                "outcome": t.get("outcome", ""),
                "resolved_at": t.get("resolved_at", ""),
                "resolution_date": t.get("resolution_date", ""),
                "polymarket_url": polymarket_event_url(t.get("location", ""), t.get("target_date", ""), t.get("metric") or "high"),
            }
            for t in resolved[-20:]
        ],
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8414, log_level="warning")
