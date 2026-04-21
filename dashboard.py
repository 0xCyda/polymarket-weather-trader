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
  <title>AIFS ENS · Weather Trading Dashboard</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      color-scheme: dark;
      --bg-0: #060910;
      --bg-1: #0a1120;
      --bg-card: rgba(18, 28, 50, 0.55);
      --bg-card-hover: rgba(24, 36, 62, 0.75);
      --border-subtle: rgba(70, 95, 150, 0.18);
      --border-strong: rgba(90, 125, 195, 0.32);
      --text-primary: #e8edf7;
      --text-secondary: #a8b5d0;
      --text-muted: #6e7d9f;
      --text-faint: #4a5878;
      --accent-blue: #60a5fa;
      --accent-cyan: #22d3ee;
      --accent-green: #34d399;
      --accent-red: #f87171;
      --accent-amber: #fbbf24;
      --accent-violet: #a78bfa;
      --grad-1: linear-gradient(135deg, #60a5fa 0%, #a78bfa 100%);
      --grad-2: linear-gradient(135deg, #34d399 0%, #22d3ee 100%);
      --grad-text: linear-gradient(135deg, #e8edf7 0%, #8ab4ff 50%, #a78bfa 100%);
      --shadow-card: 0 4px 12px rgba(0,0,0,.15), 0 20px 60px rgba(0,0,0,.3);
    }
    * { box-sizing: border-box; }
    body {
      font-family: 'Inter', system-ui, sans-serif;
      background: var(--bg-0);
      background-image:
        radial-gradient(1200px 600px at 15% -10%, rgba(96, 165, 250, 0.08), transparent 60%),
        radial-gradient(1000px 500px at 85% 10%, rgba(167, 139, 250, 0.06), transparent 60%),
        radial-gradient(800px 400px at 50% 100%, rgba(34, 211, 238, 0.04), transparent 60%);
      color: var(--text-primary);
      margin: 0;
      padding: 28px 32px 48px;
      font-feature-settings: 'cv11', 'ss03';
      -webkit-font-smoothing: antialiased;
    }
    h1 {
      margin: 0;
      font-size: 28px;
      font-weight: 800;
      letter-spacing: -0.02em;
      background: var(--grad-text);
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
    }
    h2 {
      margin: 0 0 14px;
      color: var(--text-primary);
      font-size: 14px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    h2::before {
      content: '';
      width: 3px;
      height: 14px;
      background: var(--grad-1);
      border-radius: 2px;
    }
    .muted { color: var(--text-muted); font-size: 13px; }
    .faint { color: var(--text-faint); font-size: 11px; }

    /* Header */
    .header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      flex-wrap: wrap;
      gap: 16px;
      margin-bottom: 24px;
    }
    .header-left .subtitle {
      color: var(--text-secondary);
      font-size: 13px;
      margin-top: 4px;
      letter-spacing: 0.01em;
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 14px;
      border-radius: 999px;
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      font-size: 12px;
      color: var(--text-secondary);
      backdrop-filter: blur(12px);
    }
    .status-dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--accent-green);
      box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.6);
      animation: pulse 2.2s ease-in-out infinite;
    }
    .status-dot.warning { background: var(--accent-amber); box-shadow: 0 0 0 0 rgba(251, 191, 36, 0.5); }
    .status-dot.error { background: var(--accent-red); box-shadow: 0 0 0 0 rgba(248, 113, 113, 0.5); animation: none; }
    @keyframes pulse {
      0%, 100% { box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.5); }
      50% { box-shadow: 0 0 0 6px rgba(52, 211, 153, 0); }
    }
    .last-updated {
      font-size: 11px;
      color: var(--text-faint);
      margin-top: 6px;
      text-align: right;
      font-family: 'JetBrains Mono', monospace;
    }

    /* Cards */
    .card {
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: 14px;
      padding: 18px 20px;
      box-shadow: var(--shadow-card);
      backdrop-filter: blur(12px);
      transition: border-color 0.2s, transform 0.2s;
    }
    .card:hover { border-color: var(--border-strong); }

    /* Summary cards grid */
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(175px, 1fr));
      gap: 12px;
      margin-bottom: 24px;
    }
    .metric-card {
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: 14px;
      padding: 14px 16px;
      position: relative;
      overflow: hidden;
      backdrop-filter: blur(12px);
      transition: all 0.2s;
    }
    .metric-card:hover {
      border-color: var(--border-strong);
      transform: translateY(-1px);
    }
    .metric-card::before {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0; height: 2px;
      background: var(--grad-1);
      opacity: 0.4;
    }
    .metric-card.positive::before { background: var(--grad-2); opacity: 0.6; }
    .metric-card.negative::before { background: linear-gradient(135deg, #f87171, #fb923c); opacity: 0.6; }
    .metric-label {
      font-size: 10.5px;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: var(--text-muted);
      margin-bottom: 6px;
      font-weight: 500;
    }
    .metric-value {
      font-size: 26px;
      font-weight: 700;
      letter-spacing: -0.02em;
      line-height: 1.1;
      font-variant-numeric: tabular-nums;
    }
    .metric-sub {
      font-size: 11px;
      color: var(--text-faint);
      margin-top: 4px;
    }

    /* Layouts */
    .layout-main { display: grid; grid-template-columns: 2fr 1fr; gap: 20px; }
    .layout-split { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 20px; }
    @media (max-width: 960px) {
      .layout-main, .layout-split { grid-template-columns: 1fr; }
      body { padding: 20px 16px; }
    }

    /* Tables */
    .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
    table {
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      font-size: 13px;
      font-variant-numeric: tabular-nums;
    }
    thead th {
      text-align: left;
      padding: 10px 10px;
      color: var(--text-muted);
      font-weight: 600;
      font-size: 10.5px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      border-bottom: 1px solid var(--border-subtle);
      position: sticky;
      top: 0;
      background: linear-gradient(180deg, rgba(18,28,50,0.95) 0%, rgba(18,28,50,0.8) 100%);
      backdrop-filter: blur(8px);
    }
    tbody td {
      padding: 12px 10px;
      border-bottom: 1px solid var(--border-subtle);
      white-space: nowrap;
      color: var(--text-primary);
    }
    tbody tr {
      transition: background 0.15s;
    }
    tbody tr:hover {
      background: rgba(96, 165, 250, 0.04);
    }
    tbody tr:last-child td { border-bottom: none; }

    /* Colors */
    .good { color: var(--accent-green); font-weight: 600; }
    .bad  { color: var(--accent-red);   font-weight: 600; }
    .neutral { color: var(--text-secondary); }
    .med { color: var(--accent-amber); font-weight: 600; }

    /* Badges */
    .badge {
      display: inline-block;
      padding: 3px 9px;
      border-radius: 6px;
      font-size: 10.5px;
      font-weight: 600;
      letter-spacing: 0.03em;
      text-transform: uppercase;
      border: 1px solid transparent;
    }
    .badge-yes       { background: rgba(52, 211, 153, 0.12);  color: var(--accent-green); border-color: rgba(52, 211, 153, 0.25); }
    .badge-no        { background: rgba(248, 113, 113, 0.12); color: var(--accent-red); border-color: rgba(248, 113, 113, 0.25); }
    .badge-win       { background: rgba(52, 211, 153, 0.15);  color: var(--accent-green); }
    .badge-loss      { background: rgba(248, 113, 113, 0.15); color: var(--accent-red); }
    .badge-neutral   { background: rgba(120, 130, 160, 0.15); color: var(--text-secondary); }
    .badge-strategy-core { background: rgba(96, 165, 250, 0.14); color: var(--accent-blue); border-color: rgba(96, 165, 250, 0.25); }
    .badge-strategy-punt { background: rgba(167, 139, 250, 0.14); color: var(--accent-violet); border-color: rgba(167, 139, 250, 0.25); }
    .badge-source-simmer    { background: rgba(96, 165, 250, 0.12); color: var(--accent-blue); }
    .badge-source-historical{ background: rgba(251, 191, 36, 0.14); color: var(--accent-amber); }
    .badge-source-manual    { background: rgba(167, 139, 250, 0.14); color: var(--accent-violet); }

    /* Signal pills */
    .signal-str {
      padding: 3px 9px;
      border-radius: 999px;
      font-size: 10.5px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .sig-strong   { background: rgba(52, 211, 153, 0.15); color: var(--accent-green); }
    .sig-moderate { background: rgba(251, 191, 36, 0.12); color: var(--accent-amber); }
    .sig-weak     { background: rgba(148, 163, 184, 0.12); color: var(--text-secondary); }

    /* Polymarket link */
    .pm-link {
      color: #8ab4ff;
      text-decoration: none;
      border-bottom: 1px dashed rgba(138, 180, 255, 0.25);
      transition: all 0.15s;
      padding-bottom: 1px;
    }
    .pm-link:hover {
      color: var(--accent-cyan);
      border-bottom-color: var(--accent-cyan);
    }
    .pm-link::after {
      content: ' ↗';
      color: var(--text-faint);
      font-size: 11px;
    }

    /* P&L breakdown rows */
    .pnl-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 10px 0;
      border-bottom: 1px solid var(--border-subtle);
      font-size: 13px;
    }
    .pnl-row:last-child { border-bottom: none; }
    .pnl-row .label { color: var(--text-muted); font-size: 12px; }
    .pnl-row .value { font-weight: 600; font-variant-numeric: tabular-nums; }
    .section-divider {
      margin: 12px 0 8px;
      padding: 8px 0 6px;
      font-size: 10.5px;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: var(--text-faint);
      border-bottom: 1px solid var(--border-subtle);
    }

    /* Signal cards */
    .signal-row {
      padding: 11px 0;
      border-bottom: 1px solid var(--border-subtle);
      transition: background 0.15s;
      margin: 0 -8px;
      padding-left: 8px;
      padding-right: 8px;
      border-radius: 6px;
    }
    .signal-row:hover { background: rgba(96, 165, 250, 0.04); }
    .signal-row:last-child { border-bottom: none; }

    /* Empty state */
    .empty-state {
      padding: 40px 20px;
      text-align: center;
      color: var(--text-muted);
      font-size: 13px;
    }
    .empty-state-icon {
      font-size: 28px;
      opacity: 0.35;
      margin-bottom: 8px;
    }

    /* Monospace for IDs and numeric */
    .mono { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--text-faint); }

    /* Fade-in on load */
    @keyframes fadeInUp {
      from { opacity: 0; transform: translateY(4px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .card, .metric-card { animation: fadeInUp 0.4s ease-out backwards; }
    .metric-card:nth-child(1) { animation-delay: 0.02s; }
    .metric-card:nth-child(2) { animation-delay: 0.04s; }
    .metric-card:nth-child(3) { animation-delay: 0.06s; }
    .metric-card:nth-child(4) { animation-delay: 0.08s; }
    .metric-card:nth-child(5) { animation-delay: 0.10s; }
    .metric-card:nth-child(6) { animation-delay: 0.12s; }
    .metric-card:nth-child(7) { animation-delay: 0.14s; }
    .metric-card:nth-child(8) { animation-delay: 0.16s; }
  </style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <h1>AIFS ENS · Weather Dashboard</h1>
    <div class="subtitle">Paper trading · Polymarket weather markets · Core + Punt strategies</div>
  </div>
  <div>
    <div class="status-pill" id="status-pill">
      <span class="status-dot" id="status-dot"></span>
      <span id="status-text">Connecting…</span>
    </div>
    <div class="last-updated">Last updated: <span id="last-updated">—</span></div>
  </div>
</div>

<div class="grid" id="summary-cards"></div>

<div class="layout-main">
  <div class="card">
    <h2>Equity Curve</h2>
    <div id="equity-chart" style="height:340px"></div>
  </div>
  <div class="card">
    <h2>Performance</h2>
    <div id="pnl-breakdown"></div>
  </div>
</div>

<div class="layout-split">
  <div class="card table-wrap">
    <h2>Open Positions</h2>
    <table id="positions-table"></table>
  </div>
  <div class="card">
    <h2>AIFS ENS Signals</h2>
    <div id="signals-list"></div>
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

function money(v, digits) {
  const n = Number(v || 0);
  const d = digits != null ? digits : 2;
  return '$' + n.toFixed(d);
}

function toCelsius(f) {
  const n = Number(f || 0);
  return n === 0 ? '—' : (n - 32) * 5 / 9;
}

function fmtCelsius(f) {
  const c = toCelsius(f);
  return c === '—' ? '—°C' : c.toFixed(1) + '°C';
}

function fmtCelsiusDelta(f) {
  // Spread is a delta (°F difference), not an absolute temp — convert without offset
  const n = Number(f || 0);
  return n === 0 ? '—°C' : (n * 5 / 9).toFixed(1) + '°C';
}

function fmtPnl(v) {
  const n = Number(v || 0);
  if (n === 0) return '<span class="neutral">$0.00</span>';
  return n > 0
    ? `<span class="good">+$${n.toFixed(2)}</span>`
    : `<span class="bad">-$${Math.abs(n).toFixed(2)}</span>`;
}

function metricCard(label, value, opts) {
  opts = opts || {};
  const cls = opts.tone === 'positive' ? 'positive' : opts.tone === 'negative' ? 'negative' : '';
  const sub = opts.sub ? `<div class="metric-sub">${opts.sub}</div>` : '';
  return `<div class="metric-card ${cls}">
    <div class="metric-label">${label}</div>
    <div class="metric-value">${value}</div>
    ${sub}
  </div>`;
}

function signalBadge(s) {
  const cls = s === 'strong' ? 'sig-strong' : s === 'moderate' ? 'sig-moderate' : 'sig-weak';
  return `<span class="signal-str ${cls}">${s}</span>`;
}

function positionBadge(side) {
  const cls = side === 'YES' ? 'badge-yes' : 'badge-no';
  return `<span class="badge ${cls}">${side}</span>`;
}

function strategyBadge(strat) {
  const s = (strat || 'core').toLowerCase();
  const cls = s === 'punt' ? 'badge-strategy-punt' : 'badge-strategy-core';
  const label = s === 'punt' ? '🎯 PUNT' : 'CORE';
  return `<span class="badge ${cls}">${label}</span>`;
}

function sourceBadge(src) {
  if (!src) return '';
  const s = src.toLowerCase();
  const cls = s === 'historical_fallback' ? 'badge-source-historical'
           : s === 'manual' ? 'badge-source-manual'
           : 'badge-source-simmer';
  const label = s === 'historical_fallback' ? 'archive'
              : s === 'manual' ? 'manual'
              : 'simmer';
  return `<span class="badge ${cls}" title="Resolution source: ${s}">${label}</span>`;
}

function winBadge(pnl) {
  const n = Number(pnl || 0);
  if (n > 0) return `<span class="badge badge-win">+$${n.toFixed(2)}</span>`;
  if (n < 0) return `<span class="badge badge-loss">-$${Math.abs(n).toFixed(2)}</span>`;
  return `<span class="badge badge-neutral">$0.00</span>`;
}

function emptyState(icon, message) {
  return `<div class="empty-state">
    <div class="empty-state-icon">${icon}</div>
    <div>${message}</div>
  </div>`;
}

function renderCards(d) {
  const equity = Number(d.portfolio.realized_pnl || 0) + Number(d.portfolio.unrealized_pnl || 0);
  const winRate = d.stats.win_rate;
  const totalPnl = Number(d.portfolio.realized_pnl || 0);
  const unrealPnl = Number(d.portfolio.unrealized_pnl || 0);
  document.getElementById('summary-cards').innerHTML = [
    metricCard('Balance', money(d.portfolio.balance), {sub: 'paper · simulated'}),
    metricCard('Total P&L', fmtPnl(equity), {
      tone: equity > 0 ? 'positive' : equity < 0 ? 'negative' : null,
      sub: 'realized + unrealized',
    }),
    metricCard('Realized', fmtPnl(totalPnl), {
      tone: totalPnl > 0 ? 'positive' : totalPnl < 0 ? 'negative' : null,
    }),
    metricCard('Unrealized', fmtPnl(unrealPnl), {
      tone: unrealPnl > 0 ? 'positive' : unrealPnl < 0 ? 'negative' : null,
    }),
    metricCard('Win Rate', winRate != null ? winRate + '%' : '—', {
      tone: winRate >= 55 ? 'positive' : winRate < 45 && winRate != null ? 'negative' : null,
      sub: `${d.stats.wins || 0}W · ${d.stats.losses || 0}L`,
    }),
    metricCard('Open', String(d.stats.open_trades || 0), {sub: 'positions'}),
    metricCard('Resolved', String(d.stats.resolved_trades || 0), {sub: 'settled'}),
    metricCard('Total', String(d.stats.total_trades || 0), {sub: 'all trades'}),
  ].join('');
}

function renderChart(d) {
  const x = d.timeseries.map(r => {
    const [y, m, day] = r.date.split('-');
    const d = new Date(Number(y), Number(m) - 1, Number(day));
    return d.toLocaleDateString('en-AU', { timeZone: 'Australia/Perth', timeZoneName: 'short' }).replace(/\s+\w+$/, '');
  });
  const y = d.timeseries.map(r => r.cumulative_pnl);
  const container = document.getElementById('equity-chart');
  if (!x.length) {
    container.innerHTML = emptyState('📉', 'No equity data yet — trades will populate this chart');
    return;
  }
  const lastPnl = y[y.length - 1] || 0;
  const lineColor = lastPnl >= 0 ? '#34d399' : '#f87171';
  const fillColor = lastPnl >= 0 ? 'rgba(52, 211, 153, 0.10)' : 'rgba(248, 113, 113, 0.10)';
  const data = [{
    x, y,
    type: 'scatter',
    mode: 'lines+markers',
    fill: 'tozeroy',
    fillcolor: fillColor,
    line: { color: lineColor, width: 2.5, shape: 'spline', smoothing: 0.6 },
    marker: { color: lineColor, size: 5, line: { color: '#0a1120', width: 1 } },
    hovertemplate: '<b>%{x}</b><br>P&L: $%{y:.2f}<extra></extra>',
  }];
  const layout = {
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    margin: { l: 54, r: 12, t: 8, b: 44 },
    xaxis: {
      color: '#6e7d9f',
      gridcolor: 'rgba(70, 95, 150, 0.10)',
      zerolinecolor: 'rgba(70, 95, 150, 0.18)',
      tickfont: { color: '#6e7d9f', family: 'JetBrains Mono', size: 10 },
      showgrid: true,
    },
    yaxis: {
      color: '#6e7d9f',
      gridcolor: 'rgba(70, 95, 150, 0.10)',
      zerolinecolor: 'rgba(70, 95, 150, 0.25)',
      tickfont: { color: '#6e7d9f', family: 'JetBrains Mono', size: 10 },
      tickprefix: '$',
    },
    hoverlabel: {
      bgcolor: '#0a1120',
      bordercolor: '#60a5fa',
      font: { color: '#e8edf7', family: 'JetBrains Mono', size: 11 },
    },
  };
  Plotly.newPlot('equity-chart', data, layout, { displayModeBar: false, responsive: true });
}

function renderBreakdown(d) {
  const s = d.stats;
  const sections = [
    { header: 'Activity' },
    ['Total Trades', String(s.total_trades)],
    ['Open', String(s.open_trades)],
    ['Resolved', String(s.resolved_trades)],
    { header: 'Outcomes' },
    ['Wins', `<span class="good">${s.wins}</span>`],
    ['Losses', `<span class="bad">${s.losses}</span>`],
    ['Win Rate', s.win_rate != null ? `<span class="${s.win_rate >= 50 ? 'good' : 'bad'}">${s.win_rate}%</span>` : '—'],
    { header: 'P&L' },
    ['Total', fmtPnl(s.total_pnl)],
    ['Average', s.avg_pnl != null ? fmtPnl(s.avg_pnl) : '—'],
    ['Best Trade', s.best_trade != null ? `<span class="good">+${money(Math.abs(s.best_trade))}</span>` : '—'],
    ['Worst Trade', s.worst_trade != null ? `<span class="bad">-${money(Math.abs(s.worst_trade))}</span>` : '—'],
  ];
  document.getElementById('pnl-breakdown').innerHTML = sections.map(item => {
    if (item.header) {
      return `<div class="section-divider">${item.header}</div>`;
    }
    const [k, v] = item;
    return `<div class="pnl-row"><span class="label">${k}</span><span class="value">${v}</span></div>`;
  }).join('');
}

function renderPositions(d) {
  const container = document.getElementById('positions-table');
  if (!d.positions.length) {
    container.innerHTML = `<tr><td colspan="9">${emptyState('📭', 'No open positions')}</td></tr>`;
    return;
  }
  const headers = ['Market', 'Strategy', 'Side', 'Shares', 'Entry', 'Current', 'uPNL', 'Resolves', 'ID'];
  const rows = d.positions.map(p => {
    const q = p.question || '—';
    const truncQ = q.length > 58 ? q.substring(0, 55) + '…' : q;
    const side = p.side ? p.side.toUpperCase() : 'YES';
    const upnl = Number(p.upnl || 0);
    const upnlStr = upnl > 0
      ? `<span class="good">+$${upnl.toFixed(2)}</span>`
      : upnl < 0
      ? `<span class="bad">-$${Math.abs(upnl).toFixed(2)}</span>`
      : '<span class="neutral">$0.00</span>';
    const marketCell = p.polymarket_url
      ? `<a class="pm-link" href="${p.polymarket_url}" target="_blank" rel="noopener" title="${q}">${truncQ}</a>`
      : truncQ;
    return [
      marketCell,
      strategyBadge(p.strategy),
      positionBadge(side),
      `<span class="mono">${(p.shares || 0).toFixed(1)}</span>`,
      `<span class="mono">$${(p.entry_price || 0).toFixed(3)}</span>`,
      `<span class="mono">$${(p.current_price || 0).toFixed(3)}</span>`,
      upnlStr,
      `<span class="mono">${(p.target_date || '—').substring(0, 10)}</span>`,
      p.market_id ? `<span class="mono" title="${p.market_id}">${p.market_id.substring(0, 6)}…</span>` : '—',
    ];
  });
  container.innerHTML =
    `<thead><tr>${headers.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>` +
    rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('') +
    '</tbody>';
}

function renderSignals(d) {
  const container = document.getElementById('signals-list');
  if (!d.signals.length) {
    container.innerHTML = emptyState('📡', 'No signals in latest scan');
    return;
  }

  const SIGNAL_ORDER = { strong: 0, moderate: 1, weak: 2 };
  const sorted = [...d.signals].sort((a, b) =>
    (SIGNAL_ORDER[a.signal] ?? 3) - (SIGNAL_ORDER[b.signal] ?? 3)
  );

  const INITIAL = 5;
  let expanded = false;

  function render() {
    const visible = expanded ? sorted : sorted.slice(0, INITIAL);
    container.innerHTML = visible.map(s => `
      <div class="signal-row">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
          <span style="font-weight:600;color:var(--text-primary);font-size:14px">${s.location}</span>
          <span class="mono" style="font-size:11px">${s.date}</span>
        </div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:5px;gap:8px">
          <span style="color:var(--text-secondary);font-size:13px"><span class="mono">${fmtCelsius(s.temp)}</span> · ${s.metric}</span>
          ${signalBadge(s.signal)}
        </div>
        <div class="faint" style="margin-top:4px">
          ${s.models} models · spread <span class="mono">${fmtCelsiusDelta(s.spread)}</span>${s.agree !== 'N/A' ? ' · ' + s.agree + '% agree' : ''}
        </div>
      </div>
    `).join('');

    if (sorted.length > INITIAL) {
      container.innerHTML += `
        <button id="signal-toggle" onclick="window._signalExpanded=!window._signalExpanded;renderSignals(window._lastSignals)" style="
          width:100%;margin-top:10px;padding:7px;background:rgba(96,165,250,0.08);border:1px solid var(--border-subtle);
          border-radius:6px;color:var(--text-secondary);font-size:12px;cursor:pointer;
        ">Show ${window._signalExpanded ? 'less' : 'more'} (${sorted.length - INITIAL} more)</button>
      `;
    }
  }

  window._lastSignals = d.signals;
  window._signalExpanded = false;
  render();
}

function renderResolved(d) {
  const container = document.getElementById('resolved-table');
  if (!d.resolved.length) {
    container.innerHTML = `<tr><td colspan="8">${emptyState('✅', 'No resolved trades yet')}</td></tr>`;
    return;
  }
  const headers = ['Market', 'Strategy', 'Outcome', 'Entry', 'Exit', 'Shares', 'P&L', 'Resolved'];
  const rows = d.resolved.slice().reverse().map(t => {
    const exit = Number(t.exit_price || 0);
    const entry = Number(t.entry_price || 0);
    const shares = Number(t.shares || 0);
    const pnl = Number(t.pnl || 0);
    const outcome = t.outcome ? t.outcome.toUpperCase() : (exit > 0.5 ? 'YES' : 'NO');
    const resolvedDate = t.resolved_at ? t.resolved_at.substring(0, 10) : (t.resolution_date || '—');
    const locName = (t.location || '—').substring(0, 28);
    const locCell = t.polymarket_url
      ? `<a class="pm-link" href="${t.polymarket_url}" target="_blank" rel="noopener" title="Open on Polymarket">${locName}</a>`
      : locName;
    const srcBadge = t.resolution_source ? ' ' + sourceBadge(t.resolution_source) : '';
    return [
      locCell,
      strategyBadge(t.strategy),
      positionBadge(outcome),
      `<span class="mono">$${entry.toFixed(3)}</span>`,
      `<span class="mono">$${exit.toFixed(3)}</span>`,
      `<span class="mono">${shares.toFixed(1)}</span>`,
      winBadge(pnl),
      `<span class="mono">${resolvedDate.substring(0, 10)}</span>${srcBadge}`,
    ];
  });
  container.innerHTML =
    `<thead><tr>${headers.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>` +
    rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('') +
    '</tbody>';
}

function setStatus(state, msg) {
  const dot = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  dot.className = 'status-dot' + (state === 'error' ? ' error' : state === 'warning' ? ' warning' : '');
  text.textContent = msg;
}

async function refresh() {
  try {
    setStatus('warning', 'Refreshing…');
    const state = await loadJson('/api/state');
    renderCards(state);
    renderChart(state);
    renderBreakdown(state);
    renderPositions(state);
    renderSignals(state);
    renderResolved(state);
    const now = new Date();
    const t = now.toLocaleTimeString('en-AU', { timeZone: 'Australia/Perth' });
    document.getElementById('last-updated').textContent = t + ' AWST';
    setStatus('ok', 'Live · 30m refresh');
  } catch (e) {
    setStatus('error', 'Connection lost');
    console.error(e);
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
    """Cumulative P&L over time from resolved trades — one row per date (latest value wins)."""
    rows = []
    for t in trades:
        if t.get("status") == "resolved" and t.get("pnl") is not None:
            ts = t.get("resolved_at", "")[:10] if t.get("resolved_at") else t.get("entered_at", "")[:10]
            rows.append({"date": ts, "pnl": float(t["pnl"])})
    # Sort by date ascending
    rows.sort(key=lambda r: r["date"])
    # Aggregate: keep only the last (final cumulative) entry per date
    aggregated = {}
    cumulative = 0.0
    for r in rows:
        cumulative += r["pnl"]
        aggregated[r["date"]] = round(cumulative, 4)
    return [{"date": d, "cumulative_pnl": v} for d, v in aggregated.items()]


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
        # NO token settles at (1 - yes_price). Paid `entry` per NO share.
        return round(((1 - current) - entry) * shares_no, 2)
    # Paper journal fallback — respect side
    shares = float(pos.get("shares") or 0)
    side = (pos.get("side") or "yes").lower()
    if shares > 0 and entry > 0:
        if side == "yes":
            return round((current - entry) * shares, 2)
        return round(((1 - current) - entry) * shares, 2)
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
    signals.sort(key=lambda s: (
        {"strong": 0, "moderate": 1, "weak": 2}.get(s["signal"], 3),
        s.get("location", ""),
    ))
    return signals


def _get_portfolio_stats(enriched_positions: list[dict]) -> dict:
    """Compute balance, realized, unrealized P&L using pre-enriched positions."""
    trades = _load_trades_jsonl(PAPER_TRADES)
    resolved = [t for t in trades if t.get("status") == "resolved"]
    realized = round(sum(float(t.get("pnl") or 0) for t in resolved), 4)
    unrealized = round(sum(float(p.get("upnl") or 0) for p in enriched_positions), 2)
    return {
        "balance": round(10000.0 + realized, 2),
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
                "strategy": p.get("strategy") or "core",
                "market_id": p.get("market_id", ""),
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
                "resolution_source": t.get("resolution_source", ""),
                "strategy": t.get("strategy") or "core",
                "polymarket_url": polymarket_event_url(t.get("location", ""), t.get("target_date", ""), t.get("metric") or "high"),
            }
            for t in resolved[-20:]
        ],
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8414, log_level="warning")
