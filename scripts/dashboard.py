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
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# Add scripts/ to path for paper_journal
_BASE = Path(__file__).resolve().parent
SKILL_DIR = _BASE
# Data files live at project root/data/, one level up from scripts/
_PROJECT_ROOT = _BASE.parent
DATA_DIR = _PROJECT_ROOT / "data"
SCAN_LOG = DATA_DIR / "forecast_history.jsonl"
PAPER_TRADES = DATA_DIR / "paper_trades.jsonl"
LATEST_CANDIDATES_FILE = SKILL_DIR / "data" / "latest_candidates.json"
SKIP_LOG = SKILL_DIR / "data" / "skip_events.jsonl"
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


def _coerce_price(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

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
      display: grid;
      grid-template-columns: 1fr auto auto auto;
      align-items: center;
      gap: 16px;
      margin-bottom: 28px;
    }
    .header-right {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }
    .header-scan {
      display: contents;
    }
    .status-line {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .action-btn {
      padding: 7px 16px;
      min-width: 112px;
      text-align: center;
      font-weight: 500;
    }
    .action-btn.scan-btn {
      border-color: rgba(52, 211, 153, 0.55);
      color: var(--accent-green);
      background: rgba(52, 211, 153, 0.06);
    }
    .action-btn.scan-btn:hover:not(:disabled) {
      border-color: var(--accent-green);
      color: var(--accent-green);
      background: rgba(52, 211, 153, 0.1);
    }
    .action-btn.refresh-btn {
      border-color: rgba(251, 191, 36, 0.55);
      color: var(--accent-amber);
      background: rgba(251, 191, 36, 0.06);
    }
    .action-btn.refresh-btn:hover:not(:disabled) {
      border-color: var(--accent-amber);
      color: var(--accent-amber);
      background: rgba(251, 191, 36, 0.1);
    }
    .action-btn:active { transform: translateY(1px); }
    .action-btn:disabled { opacity: 0.6; cursor: not-allowed; }
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
    .tab-btn {
      background: transparent; border: 1px solid rgba(255,255,255,0.12);
      color: var(--text-secondary); padding: 5px 14px; border-radius: 6px;
      cursor: pointer; font-size: 0.8rem; font-family: inherit;
    }
    .tab-btn:hover { border-color: var(--accent-blue); color: var(--accent-blue); }
    .tab-btn.active { background: rgba(96,165,250,0.12); border-color: var(--accent-blue); color: var(--accent-blue); }
    .pager-btn {
      background: rgba(96,165,250,0.08); border: 1px solid var(--border-subtle);
      color: var(--text-secondary); padding: 5px 12px; border-radius: 6px;
      cursor: pointer; font-size: 0.8rem; font-family: inherit;
    }
    .pager-btn:hover:not(:disabled) { border-color: var(--accent-blue); color: var(--accent-blue); }
    .pager-btn:disabled { opacity: 0.4; cursor: not-allowed; }
    .config-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }
    .config-section { background: rgba(255,255,255,0.03); border-radius: 8px; padding: 14px 16px; }
    .config-section h3 { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-secondary); margin: 0 0 10px 0; }
    .config-table { width: 100%; border-collapse: collapse; }
    .config-table tr:not(:last-child) td { border-bottom: 1px solid rgba(255,255,255,0.05); }
    .config-key { padding: 4px 0; color: var(--text-secondary); font-size: 0.82rem; }
    .config-val { padding: 4px 0; text-align: right; font-size: 0.82rem; }
    .modes-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 16px; }
    @media (max-width: 720px) { .modes-grid { grid-template-columns: 1fr; } }
    .mode-tile { background: rgba(255,255,255,0.03); border-radius: 8px; padding: 14px 16px; border: 1px solid rgba(255,255,255,0.06); display: flex; flex-direction: column; gap: 8px; }
    .mode-tile .mode-name { font-size: 0.78rem; letter-spacing: 0.08em; color: var(--text-secondary); text-transform: uppercase; }
    .mode-tile .mode-desc { font-size: 0.78rem; color: var(--text-faint); line-height: 1.35; }
    .mode-row { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
    .mode-status { font-size: 0.82rem; font-weight: 600; }
    .mode-status.on { color: var(--accent-green); }
    .mode-status.off { color: var(--accent-red); }
    .mode-toggle {
      background: rgba(52,211,153,0.08); border: 1px solid rgba(52,211,153,0.35);
      color: var(--accent-green); padding: 4px 12px; border-radius: 6px;
      cursor: pointer; font-size: 0.75rem; font-family: inherit;
    }
    .mode-toggle.off { background: rgba(248,113,113,0.08); border-color: rgba(248,113,113,0.35); color: var(--accent-red); }
    .mode-toggle:hover { filter: brightness(1.2); }
    .overview-card {
      background: linear-gradient(135deg, rgba(96,165,250,0.06), rgba(52,211,153,0.04));
      border: 1px solid rgba(96,165,250,0.18);
      border-radius: 10px; padding: 18px 22px; margin-bottom: 18px;
    }
    .overview-card h2 { margin: 0 0 4px 0; font-size: 1.05rem; color: var(--text-primary); }
    .overview-tag { display: inline-block; font-size: 0.7rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--accent-blue); margin-bottom: 10px; }
    .overview-card p { margin: 8px 0; font-size: 0.85rem; line-height: 1.5; color: var(--text-secondary); }
    .overview-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-top: 14px; }
    .overview-cell { background: rgba(0,0,0,0.18); border-radius: 8px; padding: 12px 14px; }
    .overview-cell h4 { margin: 0 0 6px 0; font-size: 0.78rem; color: var(--text-primary); letter-spacing: 0.04em; }
    .overview-cell ul { margin: 0; padding-left: 18px; font-size: 0.78rem; color: var(--text-secondary); line-height: 1.5; }
    .overview-cell ul li { margin-bottom: 2px; }
    @keyframes pulse {
      0%, 100% { box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.5); }
      50% { box-shadow: 0 0 0 6px rgba(52, 211, 153, 0); }
    }
    .last-updated {
      font-size: 11px;
      color: var(--text-faint);
      font-family: 'JetBrains Mono', monospace;
      white-space: nowrap;
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
      .header {
        grid-template-columns: 1fr auto;
      }
      .header-left {
        grid-column: 1 / -1;
      }
      .header-right {
        grid-column: 1;
        justify-self: start;
      }
      .header-scan {
        grid-column: 2;
        justify-self: end;
      }
      .status-line {
        grid-column: 1 / -1;
      }
    }
    @media (max-width: 600px) {
      .header {
        grid-template-columns: 1fr;
      }
      h1 { font-size: 22px; }
      body { padding: 16px 12px 40px; }
      .grid {
        grid-template-columns: repeat(2, 1fr);
      }
      .header-right,
      .status-line {
        grid-column: 1;
        justify-self: start;
      }
      .header-right {
        width: 100%;
        flex-wrap: nowrap;
        gap: 4px;
      }
      .tab-btn,
      .action-btn {
        min-width: 0;
        padding: 7px 12px;
        font-size: 0.78rem;
        white-space: nowrap;
        flex: 0 1 auto;
      }
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
    .badge-tp        { background: rgba(52, 211, 153, 0.16); color: var(--accent-green); border-color: rgba(52, 211, 153, 0.32); }
    .badge-sl        { background: rgba(248, 113, 113, 0.16); color: var(--accent-red); border-color: rgba(248, 113, 113, 0.32); }
    .badge-win       { background: rgba(52, 211, 153, 0.15);  color: var(--accent-green); }
    .badge-loss      { background: rgba(248, 113, 113, 0.15); color: var(--accent-red); }
    .badge-neutral   { background: rgba(120, 130, 160, 0.15); color: var(--text-secondary); }
    .badge-strategy-core { background: rgba(96, 165, 250, 0.14); color: var(--accent-blue); border-color: rgba(96, 165, 250, 0.25); }
    .badge-strategy-punt { background: rgba(167, 139, 250, 0.14); color: var(--accent-violet); border-color: rgba(167, 139, 250, 0.25); }
    .badge-strategy-late { background: rgba(251, 191, 36, 0.14); color: #fcd34d; border-color: rgba(251, 191, 36, 0.30); }
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
  </div>
  <div class="status-line">
    <div class="status-pill" id="status-pill">
      <span class="status-dot" id="status-dot"></span>
      <span id="status-text">Connecting…</span>
    </div>
    <div class="last-updated">Last Updated: <span id="last-updated">—</span></div>
  </div>
  <div class="header-right">
    <button class="tab-btn" id="btn-overview" onclick="showTab('overview')">Overview</button>
    <button class="tab-btn" id="btn-config" onclick="showTab('config')">Config</button>
    <button class="tab-btn action-btn scan-btn" id="btn-scan" onclick="triggerScan()">Scan Now</button>
    <button class="tab-btn action-btn refresh-btn" id="btn-refresh" onclick="manualRefresh()">Refresh</button>
  </div>
  <div class="header-scan"></div>
</div>

<div id="scan-toast" style="
  display:none;
  position:fixed;top:20px;right:20px;z-index:9999;
  background:var(--bg-card);border:1px solid var(--border-strong);
  border-radius:12px;padding:14px 18px;min-width:280px;max-width:400px;
  backdrop-filter:blur(16px);box-shadow:0 8px 32px rgba(0,0,0,0.4);
  font-size:13px;
">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <span id="scan-toast-title" style="font-weight:600;color:var(--text-primary)">Scanning…</span>
    <button onclick="document.getElementById('scan-toast').style.display='none'" style="background:none;border:none;color:var(--text-faint);cursor:pointer;font-size:16px;padding:0;line-height:1">×</button>
  </div>
  <div id="scan-toast-body" style="color:var(--text-secondary);line-height:1.5"></div>
</div>

<div id="tab-overview">
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
    <h2>Open Positions <span id="open-positions-count" style="font-size:14px;font-weight:500;margin-left:6px;color:var(--muted)"></span></h2>
    <table id="positions-table"></table>
  </div>
  <div class="card">
    <h2>AIFS ENS Signals <span id="signals-count" class="faint" style="font-size:13px;font-weight:500;text-transform:none;letter-spacing:0;margin-left:4px"></span><span id="signals-scan-time" class="faint" style="font-size:11px;font-weight:400;text-transform:none;letter-spacing:0;margin-left:4px"></span></h2>
    <div id="signals-list"></div>
  </div>
</div>

<div class="card table-wrap" style="margin-top:20px">
  <h2>Resolved Trades</h2>
  <table id="resolved-table"></table>
  <div id="resolved-pager" style="margin-top:12px;display:flex;align-items:center;justify-content:flex-end;gap:4px"></div>
</div>
</div><!-- end #tab-overview -->

<div class="card" id="config-card" style="margin-top:20px; display:none">
  <h2>Configuration</h2>
  <div id="config-content"></div>
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

// US cities use Fahrenheit on Polymarket; rest of the world uses Celsius.
// Forecast values are stored internally in °F, so US displays pass through
// and international displays are converted.
const US_CITIES = new Set([
  'NYC', 'New York', 'New York City', 'Chicago', 'Seattle', 'Atlanta',
  'Dallas', 'Miami', 'Houston', 'San Francisco', 'Phoenix',
  'Los Angeles', 'Denver', 'Austin', 'Las Vegas',
]);

function isUSLocation(loc) {
  if (!loc) return false;
  return US_CITIES.has(loc) || US_CITIES.has(loc.replace(/\s+/g, ' ').trim());
}

function toCelsius(f) {
  const n = Number(f || 0);
  return n === 0 ? '—' : (n - 32) * 5 / 9;
}

function fmtCelsius(f) {
  const c = toCelsius(f);
  return c === '—' ? '—°C' : Math.round(c) + '°C';
}

function fmtCelsiusDelta(f) {
  // Spread shown with the raw numeric value the data emits (standard rounding).
  // The underlying model unit is °F, but the display treats the spread as a
  // unitless ensemble disagreement magnitude and labels °C for non-US rows.
  const n = Number(f || 0);
  return n === 0 ? '—°C' : Math.round(n) + '°C';
}

function fmtFahrenheit(f) {
  const n = Number(f || 0);
  return n === 0 ? '—°F' : Math.round(n) + '°F';
}

function fmtFahrenheitDelta(f) {
  const n = Number(f || 0);
  return n === 0 ? '—°F' : Math.round(n) + '°F';
}

// Display helpers that pick unit based on location
function fmtTempForLoc(loc, f) {
  return isUSLocation(loc) ? fmtFahrenheit(f) : fmtCelsius(f);
}

function fmtSpreadForLoc(loc, f) {
  return isUSLocation(loc) ? fmtFahrenheitDelta(f) : fmtCelsiusDelta(f);
}

function fmtPnl(v) {
  const n = Number(v || 0);
  if (n === 0) return '<span class="neutral">$0.00</span>';
  return n > 0
    ? `<span class="good">+$${n.toFixed(2)}</span>`
    : `<span class="bad">-$${Math.abs(n).toFixed(2)}</span>`;
}

function fmtPrice(v, digits) {
  if (v == null || v === '') return '—';
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  const d = digits != null ? digits : 3;
  return '$' + n.toFixed(d);
}

function escapeHtml(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
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

function pmExitBadge(source, pnl, exitReason) {
  if ((source || '').toLowerCase() !== 'early_exit_position_manager') return '';
  const reason = String(exitReason || '').toLowerCase();
  const n = Number(pnl || 0);
  const titleSuffix = exitReason ? ` · ${escapeHtml(String(exitReason))}` : '';

  if (reason.includes('take_profit') || reason.includes('profit_target') || reason.includes('locked_in_profit')) {
    return `<span class="badge badge-tp" title="PM take profit${titleSuffix}">TP</span>`;
  }
  if (reason.includes('stop_loss')) {
    return `<span class="badge badge-sl" title="PM stop loss${titleSuffix}">SL</span>`;
  }
  if (n > 0) return `<span class="badge badge-tp" title="PM take profit${titleSuffix}">TP</span>`;
  return `<span class="badge badge-sl" title="PM stop loss${titleSuffix}">SL</span>`;
}

function shouldShowActualTemp(t) {
  return t.actual_temp != null;
}

function strategyBadge(strat) {
  const s = (strat || 'core').toLowerCase();
  let cls, label;
  if (s === 'punt')              { cls = 'badge-strategy-punt'; label = '🎯 PUNT'; }
  else if (s === 'late_add')     { cls = 'badge-strategy-late'; label = 'LATE+'; }
  else if (s.startsWith('late')) { cls = 'badge-strategy-late'; label = 'LATE'; }
  else                           { cls = 'badge-strategy-core'; label = 'CORE'; }
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
  const realized = Number(d.portfolio.realized_pnl || 0);
  const unrealPnl = Number(d.portfolio.unrealized_pnl || 0);
  const winRate = d.stats.win_rate;
  const missingMarks = Number(d.portfolio.missing_marks || 0);
  document.getElementById('summary-cards').innerHTML = [
    metricCard('Balance', money(d.portfolio.balance), {
      tone: realized > 0 ? 'positive' : realized < 0 ? 'negative' : null,
    }),
    metricCard('Unrealized', fmtPnl(unrealPnl), {
      tone: unrealPnl > 0 ? 'positive' : unrealPnl < 0 ? 'negative' : null,
      sub: missingMarks > 0 ? `${missingMarks} Mark${missingMarks === 1 ? '' : 's'} Missing` : '',
    }),
    metricCard('Win Rate', winRate != null ? winRate + '%' : '—', {
      tone: winRate >= 55 ? 'positive' : winRate < 45 && winRate != null ? 'negative' : null,
      sub: `${d.stats.wins || 0}W · ${d.stats.losses || 0}L`,
    }),
    metricCard('Today P&L', fmtPnl(d.stats.today_pnl || 0), {
      tone: (d.stats.today_pnl || 0) > 0 ? 'positive' : (d.stats.today_pnl || 0) < 0 ? 'negative' : null,
      sub: `${d.stats.today_trades || 0} Resolved Today`,
    }),
    metricCard('Open', String(d.stats.open_trades || 0), {sub: 'Positions'}),
    metricCard('Resolved', String(d.stats.resolved_trades || 0), {sub: 'Settled'}),
    metricCard('Total', String(d.stats.total_trades || 0), {sub: 'All Trades'}),
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
  const countEl = document.getElementById('open-positions-count');
  if (!d.positions.length) {
    if (countEl) countEl.textContent = '(0)';
    container.innerHTML = `<tr><td colspan="9">${emptyState('📭', 'No open positions')}</td></tr>`;
    return;
  }

  // Sort by target_date ascending (earliest resolution first)
  const sorted = [...d.positions].sort((a, b) => {
    const da = a.target_date || '9999';
    const db = b.target_date || '9999';
    return da.localeCompare(db);
  });
  if (countEl) countEl.textContent = `(${sorted.length})`;

  const headers = ['Market', 'Strategy', 'Side', 'Shares', 'Entry', 'Current', 'uPNL', 'Resolves', 'ID'];
  const rows = sorted.map(p => {
    const q = p.question || '—';
    const truncQ = q.length > 58 ? q.substring(0, 55) + '…' : q;
    const side = p.side ? p.side.toUpperCase() : 'YES';
    const hasMark = p.current_price != null && p.upnl != null;
    const currentStr = hasMark
      ? `<span class="mono">${fmtPrice(p.current_price, 3)}</span>`
      : `<span class="muted">—</span>`;
    let upnlStr = '<span class="muted">—</span>';
    if (p.upnl != null) {
      const upnl = Number(p.upnl);
      upnlStr = upnl > 0
        ? `<span class="good">+$${upnl.toFixed(2)}</span>`
        : upnl < 0
        ? `<span class="bad">-$${Math.abs(upnl).toFixed(2)}</span>`
        : '<span class="neutral">$0.00</span>';
    }
    const marketCell = p.polymarket_url
      ? `<a class="pm-link" href="${p.polymarket_url}" target="_blank" rel="noopener" title="${q}">${truncQ}</a>`
      : truncQ;
    return [
      marketCell,
      strategyBadge(p.strategy),
      positionBadge(side),
      `<span class="mono">${(p.shares || 0).toFixed(1)}</span>`,
      `<span class="mono">${fmtPrice(p.entry_price, 3)}</span>`,
      currentStr,
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
  // Accept either state object ({signals: [...]}) or bare array for re-render
  const signals = Array.isArray(d) ? d : (d && d.signals) || [];

  // Update last scan time badge
  const scanTimeEl = document.getElementById('signals-scan-time');
  if (scanTimeEl && !Array.isArray(d) && d.signals_last_scan) {
    const scanTime = new Date(d.signals_last_scan);
    const agoMs = Date.now() - scanTime.getTime();
    const agoMin = Math.round(agoMs / 60000);
    const agoStr = agoMin < 60 ? `${agoMin}m ago` : `${Math.round(agoMin / 60)}h ago`;
    scanTimeEl.textContent = `· scan ${agoStr}`;
  }

  // Update count next to the section title
  const countEl = document.getElementById('signals-count');
  if (countEl) countEl.textContent = signals.length ? `(${signals.length})` : '';

  if (!signals.length) {
    container.innerHTML = emptyState('📡', 'No signals in latest scan');
    return;
  }

  // Sort by spread ascending (tighter ensembles first); fall back to signal strength
  const SIGNAL_ORDER = { strong: 0, moderate: 1, weak: 2 };
  function spreadVal(s) {
    const n = parseFloat(s.spread);
    return Number.isFinite(n) ? n : Infinity;
  }
  const sorted = [...signals].sort((a, b) => {
    const da = spreadVal(a) - spreadVal(b);
    if (da !== 0) return da;
    return (SIGNAL_ORDER[a.signal] ?? 3) - (SIGNAL_ORDER[b.signal] ?? 3);
  });

  const INITIAL = 5;
  if (window._signalExpanded === undefined) window._signalExpanded = false;

  const visible = window._signalExpanded ? sorted : sorted.slice(0, INITIAL);
  const STATUS_COLORS = {
    positive: { bg: 'rgba(74,222,128,0.15)', fg: '#86efac', border: 'rgba(74,222,128,0.35)' },
    negative: { bg: 'rgba(248,113,113,0.15)', fg: '#fca5a5', border: 'rgba(248,113,113,0.35)' },
    info:     { bg: 'rgba(96,165,250,0.15)', fg: '#93c5fd', border: 'rgba(96,165,250,0.35)' },
    warning:  { bg: 'rgba(251,191,36,0.15)', fg: '#fcd34d', border: 'rgba(251,191,36,0.35)' },
    neutral:  { bg: 'rgba(148,163,184,0.10)', fg: 'var(--text-secondary)', border: 'rgba(148,163,184,0.30)' },
  };
  function statusBadge(label, color) {
    if (!label) return '';
    const c = STATUS_COLORS[color] || STATUS_COLORS.neutral;
    return `<span style="display:inline-block;padding:2px 7px;border-radius:4px;background:${c.bg};color:${c.fg};border:1px solid ${c.border};font-size:10px;font-weight:600;letter-spacing:0.3px">${label}</span>`;
  }
  container.innerHTML = visible.map(s => `
    <div class="signal-row">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <span style="font-weight:600;color:var(--text-primary);font-size:14px">${
          s.polymarket_url
            ? `<a class="pm-link" href="${s.polymarket_url}" target="_blank" rel="noopener">${s.location}</a>`
            : s.location
        }</span>
        <span class="mono" style="font-size:11px">${s.date}</span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:5px;gap:8px">
        <span style="color:var(--text-secondary);font-size:13px"><span class="mono">${fmtTempForLoc(s.location, s.temp)}</span> · ${s.metric}</span>
        ${signalBadge(s.signal)}
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px;gap:8px">
        <span class="faint">${s.models} models · spread <span class="mono">${fmtSpreadForLoc(s.location, s.spread)}</span>${s.agree !== 'N/A' ? ' · ' + s.agree + ' agree' : ''}</span>
        ${statusBadge(s.status_badge, s.status_color)}
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

  // Always store the array form so the toggle passes a consistent shape
  window._lastSignals = signals;
}

const RESOLVED_PAGE_SIZE = 10;

function fmtAwstTimestamp(iso) {
  if (!iso) return '—';
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return iso.substring(0, 16).replace('T', ' ');
  return dt.toLocaleString('en-AU', {
    timeZone: 'Australia/Perth',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).replace(',', '').replace(/\sat\s/, ' ') + ' AWST';
}

function resolvedSignature(all) {
  if (!Array.isArray(all) || !all.length) return 'empty';
  const first = all[0] || {};
  const last = all[all.length - 1] || {};
  return [
    all.length,
    first.resolution_date || first.target_date || '',
    first.resolved_at || '',
    first.location || '',
    last.resolution_date || last.target_date || '',
    last.resolved_at || '',
    last.location || '',
  ].join('|');
}

function renderResolved(d) {
  const container = document.getElementById('resolved-table');
  // Accept state object or bare array (for re-render on page change)
  const all = Array.isArray(d) ? d : (d && d.resolved) || [];
  if (!all.length) {
    container.innerHTML = `<tr><td colspan="10">${emptyState('✅', 'No resolved trades yet')}</td></tr>`;
    return;
  }

  const parseResolvedDate = (t) => {
    const dateStr = t.resolution_date || t.target_date || (t.resolved_at ? t.resolved_at.substring(0, 10) : '');
    return dateStr ? Date.parse(`${dateStr}T00:00:00+08:00`) : Number.NEGATIVE_INFINITY;
  };
  const parseResolvedAt = (t) => t.resolved_at ? Date.parse(t.resolved_at) : Number.NEGATIVE_INFINITY;
  const parseEnteredAt = (t) => t.entered_at ? Date.parse(t.entered_at) : Number.NEGATIVE_INFINITY;

  // Resolution date first, then actual settlement time, then entry time.
  const sorted = all.slice().sort((a, b) => (
    parseResolvedDate(b) - parseResolvedDate(a)
    || parseResolvedAt(b) - parseResolvedAt(a)
    || parseEnteredAt(b) - parseEnteredAt(a)
  ));
  const totalPages = Math.max(1, Math.ceil(sorted.length / RESOLVED_PAGE_SIZE));
  const sig = resolvedSignature(sorted);
  if (window._resolvedSignature !== sig) {
    window._resolvedPage = 0;
    window._resolvedSignature = sig;
  }
  if (window._resolvedPage === undefined) window._resolvedPage = 0;
  // Clamp page if data shrank between renders
  if (window._resolvedPage >= totalPages) window._resolvedPage = totalPages - 1;
  const page = window._resolvedPage;
  const start = page * RESOLVED_PAGE_SIZE;
  const slice = sorted.slice(start, start + RESOLVED_PAGE_SIZE);

  const headers = ['Location', 'Strategy', 'Outcome', 'Forecast', 'Actual', 'Entry', 'Exit', 'P&L', 'Entered', 'Resolved'];
  const rows = slice.map(t => {
    const exit = Number(t.exit_price || 0);
    const entry = Number(t.entry_price || 0);
    const shares = Number(t.shares || 0);
    const pnl = Number(t.pnl || 0);
    const outcome = t.outcome ? t.outcome.toUpperCase() : (exit > 0.5 ? 'YES' : 'NO');
    const enteredDate = fmtAwstTimestamp(t.entered_at);
    const resolutionDate = t.resolution_date || t.target_date || (t.resolved_at ? t.resolved_at.substring(0, 10) : '—');
    const resolvedStamp = t.resolved_at ? fmtAwstTimestamp(t.resolved_at) : '';
    const resolvedCell = resolvedStamp && resolvedStamp !== resolutionDate
      ? `<div><span class="mono">${resolutionDate}</span><div class="faint" style="font-size:10px">${resolvedStamp}</div></div>`
      : `<span class="mono">${resolutionDate}</span>`;
    const locName = (t.location || '—').substring(0, 28);
    const locCell = t.polymarket_url
      ? `<a class="pm-link" href="${t.polymarket_url}" target="_blank" rel="noopener" title="Open on Polymarket">${locName}</a>`
      : locName;
    const srcBadge = t.resolution_source ? ' ' + sourceBadge(t.resolution_source) : '';

    // Forecast temp
    let forecastCell = '—';
    if (t.forecast_temp != null) {
      forecastCell = `<span class="mono">${fmtTempForLoc(t.location, t.forecast_temp)}</span>`;
    }

    // Actual temp — show if recorded, otherwise blank
    let actualCell;
    if (shouldShowActualTemp(t)) {
      const delta = t.forecast_temp != null ? Number(t.actual_temp) - Number(t.forecast_temp) : null;
      const deltaStr = delta != null
        ? `<span style="color:${Math.abs(delta) <= 3 ? 'var(--accent-green)' : 'var(--accent-amber)'};font-size:10px"> (${delta > 0 ? '+' : ''}${isUSLocation(t.location) ? Math.round(delta) + '°F' : Math.round(delta * 5/9) + '°C'})</span>`
        : '';
      actualCell = `<span class="mono">${fmtTempForLoc(t.location, t.actual_temp)}</span>${deltaStr}`;
    } else {
      actualCell = `<span class="faint">—</span>`;
    }

    return [
      locCell,
      strategyBadge(t.strategy),
      `<div style="display:inline-flex;gap:6px;align-items:center;flex-wrap:nowrap;white-space:nowrap">${positionBadge(outcome)}${pmExitBadge(t.resolution_source, pnl, t.exit_reason)}</div>`,
      forecastCell,
      actualCell,
      `<span class="mono">$${entry.toFixed(3)}</span>`,
      `<span class="mono">$${exit.toFixed(3)}</span>`,
      winBadge(pnl),
      `<span class="mono">${enteredDate}</span>`,
      `${resolvedCell}${srcBadge}`,
    ];
  });
  container.innerHTML =
    `<thead><tr>${headers.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>` +
    rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('') +
    '</tbody>';

  // Pagination controls (rendered in sibling div #resolved-pager)
  const pager = document.getElementById('resolved-pager');
  if (pager) {
    if (totalPages <= 1) {
      pager.innerHTML = `<span class="faint">${sorted.length} trade${sorted.length === 1 ? '' : 's'}</span>`;
    } else {
      const prevDisabled = page === 0 ? 'disabled' : '';
      const nextDisabled = page >= totalPages - 1 ? 'disabled' : '';
      pager.innerHTML = `
        <button onclick="window._resolvedPage=Math.max(0,(window._resolvedPage||0)-1);renderResolved(window._lastResolved)"
          ${prevDisabled} class="pager-btn">← Prev</button>
        <span class="faint" style="margin:0 12px">Page ${page + 1} of ${totalPages} · ${sorted.length} trades</span>
        <button onclick="window._resolvedPage=Math.min(${totalPages - 1},(window._resolvedPage||0)+1);renderResolved(window._lastResolved)"
          ${nextDisabled} class="pager-btn">Next →</button>
      `;
    }
  }

  window._lastResolved = all;
}

function setStatus(state, msg) {
  const dot = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  dot.className = 'status-dot' + (state === 'error' ? ' error' : state === 'warning' ? ' warning' : '');
  text.textContent = msg;
}

function showTab(tab) {
  document.getElementById('tab-overview').style.display = tab === 'overview' ? '' : 'none';
  document.getElementById('config-card').style.display = tab === 'config' ? '' : 'none';
  document.getElementById('btn-overview').classList.toggle('active', tab === 'overview');
  document.getElementById('btn-config').classList.toggle('active', tab === 'config');
  if (tab === 'config') renderConfig();
}

function renderConfig() {
  const el = document.getElementById('config-content');
  if (!el || el.dataset.loaded) return;
  loadJson('/api/config').then(cfg => {
    el.dataset.loaded = '1';
    const sections = [
      {
        label: 'API Keys',
        rows: [
          { k: 'Simmer API', v: cfg.has_simmer_key ? '✓ configured' : '✗ missing' },
        ]
      },
      {
        label: 'Ensemble Models',
        rows: Object.entries(cfg.ensemble_models || {}).map(([m, w]) => ({
          k: m, v: `${(w * 100).toFixed(0)}%`
        }))
      },
      {
        label: `Cities (${cfg.locations ? cfg.locations.length : 0})`,
        rows: (cfg.locations && cfg.locations.length)
          ? cfg.locations.map((loc, i) => ({ k: String(i + 1).padStart(2, '0'), v: loc }))
          : [{ k: 'Locations', v: '—' }]
      },
      {
        label: 'Core Trading',
        rows: [
          { k: 'Entry threshold', v: cfg.entry_threshold },
          { k: 'Min edge', v: cfg.min_edge },
          { k: 'Exit threshold', v: cfg.exit_threshold },
          { k: 'Exit profit mult', v: cfg.exit_profit_multiplier },
          { k: 'Sizing', v: 'tiered (city difficulty)' },
          { k: 'Easy (3%)', v: `$${(cfg.paper_balance * 0.03).toFixed(0)}` },
          { k: 'Medium (2%)', v: `$${(cfg.paper_balance * 0.02).toFixed(0)}` },
          { k: 'Hard (1%)', v: `$${(cfg.paper_balance * 0.01).toFixed(0)}` },
          { k: 'Max trades/run', v: cfg.max_trades_per_run },
          { k: 'Order type', v: cfg.order_type },
          { k: 'Paper balance', v: `$${cfg.paper_balance.toLocaleString()}` },
          { k: 'Max daily loss', v: cfg.max_daily_loss_usd > 0 ? `$${cfg.max_daily_loss_usd}` : 'disabled' },
        ]
      },
      {
        label: 'Punt Mode',
        rows: [
          { k: 'Enabled', v: cfg.punt_mode ? 'Yes' : 'No' },
          { k: 'Max position', v: `$${cfg.punt_max_position_usd}` },
          { k: 'Price ceiling', v: `$${cfg.punt_price_ceiling}` },
          { k: 'Min edge', v: cfg.punt_min_edge },
          { k: 'Min confidence', v: `${(cfg.punt_min_confidence * 100).toFixed(0)}%` },
          { k: 'Daily budget', v: `$${cfg.punt_daily_budget_usd}` },
        ]
      },
      {
        label: 'Late Mode',
        rows: [
          { k: 'Enabled', v: cfg.late_mode ? 'Yes' : 'No' },
          { k: 'Entry hour (local)', v: `${cfg.late_entry_hour}:00` },
          { k: 'Price ceiling', v: `$${cfg.late_price_ceiling}` },
          { k: 'Edge buffer', v: `${cfg.late_edge_buffer_c}°C` },
          { k: 'Max position', v: `$${cfg.late_max_position_usd}` },
          { k: 'Sizing', v: 'edge-banded' },
          { k: 'Cities', v: (cfg.late_cities || '').split(',').filter(Boolean).length || '—' },
        ]
      },
      {
        label: 'Filters',
        rows: [
          { k: 'Slippage max', v: `${(cfg.slippage_max * 100).toFixed(0)}%` },
          { k: 'Min liquidity', v: cfg.min_liquidity > 0 ? `$${cfg.min_liquidity}` : 'disabled' },
          { k: 'Binary only', v: cfg.binary_only ? 'Yes' : 'No' },
        ]
      },
      {
        label: 'Vol Targeting',
        rows: [
          { k: 'Enabled', v: cfg.vol_targeting ? 'Yes' : 'No' },
          { k: 'Target vol', v: `${(cfg.target_vol * 100).toFixed(0)}%` },
          { k: 'Max leverage', v: `${cfg.vol_max_leverage}×` },
          { k: 'Min allocation', v: `${(cfg.vol_min_allocation * 100).toFixed(0)}%` },
          { k: 'EWMA span', v: cfg.vol_span },
        ]
      },
      {
        label: 'Ladder Exits',
        rows: [
          { k: 'First exit price', v: cfg.ladder_first_exit > 0 ? `$${cfg.ladder_first_exit}` : 'disabled' },
          { k: 'Fraction at first exit', v: cfg.ladder_first_exit > 0 ? `${(cfg.ladder_first_fraction * 100).toFixed(0)}%` : '—' },
        ]
      },
      {
        label: 'Discovery',
        rows: [
          { k: 'Cache TTL', v: `${cfg.discovery_cache_minutes} min` },
          { k: 'Disk forecast cache', v: cfg.forecast_cache_disk ? 'Yes' : 'No' },
          { k: 'Concurrent scans', v: cfg.concurrent_scans ? 'Yes' : 'No' },
          { k: 'Log level', v: cfg.log_level },
        ]
      },
    ];

    const overviewHtml = `
      <div class="overview-card">
        <div class="overview-tag">Overview</div>
        <h2>Polymarket Weather Trader</h2>
        <p>A rules-based weather trading system for Polymarket daily-temperature markets. It combines ensemble forecasts, live intraday observations, and structured risk controls to find mispriced buckets, size entries by confidence, and manage positions through the full trade lifecycle.</p>
        <div class="overview-grid">
          <div class="overview-cell">
            <h4>Data Sources</h4>
            <ul>
              <li>ECMWF AIFS ENS (18%)</li>
              <li>ECMWF IFS 0.25° (24%)</li>
              <li>NOAA GFS seamless (14%)</li>
              <li>Météo-France ARPEGE (10%)</li>
              <li>TWC / Wunderground intraday obs</li>
              <li>METAR live airport feeds (US, D+0)</li>
            </ul>
          </div>
          <div class="overview-cell">
            <h4>Three Trading Modes</h4>
            <ul>
              <li><strong>CORE</strong>: 4-hourly, buys bucket with highest model edge vs market price</li>
              <li><strong>PUNT</strong>: tail lottery, buys deeply-mispriced tail buckets (&le;14.9¢) with fixed small stakes</li>
              <li><strong>LATE</strong>: hourly, buys bucket containing observed daily max at 3pm local</li>
            </ul>
          </div>
          <div class="overview-cell">
            <h4>Risk &amp; Sizing</h4>
            <ul>
              <li>Per-mode daily budgets and max-position caps</li>
              <li>Volatility targeting available (EWMA-based)</li>
              <li>Laddered exits + dynamic profit multiplier</li>
              <li>Optional daily-loss hard stop</li>
            </ul>
          </div>
          <div class="overview-cell">
            <h4>Observability</h4>
            <ul>
              <li>Paper trade journal with strategy tags (CORE / PUNT / LATE)</li>
              <li>Forecast-accuracy history for post-hoc model scoring</li>
              <li>Skipped-trade log for funnel analysis</li>
              <li>4-hourly Discord scan report to #polymarket</li>
            </ul>
          </div>
        </div>
      </div>`;

    const modes = [
      { id: 'core', name: 'CORE', desc: 'Forecast-driven ensemble trader (CORE scan every 4h).', enabled: true },
      { id: 'punt', name: 'PUNT', desc: 'Tail-priced lottery buckets (runs alongside CORE).', enabled: cfg.punt_mode !== false },
      { id: 'late', name: 'LATE', desc: 'Day-of intraday entry from TWC obs at 3pm local (hourly scan).', enabled: cfg.late_mode !== false },
    ];
    const modesHtml = `<div class="modes-grid">${
      modes.map(m => `
        <div class="mode-tile" data-mode="${m.id}">
          <div class="mode-row">
            <span class="mode-name">${m.name}</span>
            <span class="mode-status ${m.enabled ? 'on' : 'off'}">${m.enabled ? 'Enabled' : 'Disabled'}</span>
          </div>
          <div class="mode-desc">${m.desc}</div>
          <div class="mode-row" style="margin-top:4px">
            <span style="font-size:0.72rem;color:var(--text-faint)">UI only — does not affect runtime</span>
            <button class="mode-toggle ${m.enabled ? '' : 'off'}" onclick="toggleMode('${m.id}')">${m.enabled ? 'Disable' : 'Enable'}</button>
          </div>
        </div>
      `).join('')
    }</div>`;

    el.innerHTML = overviewHtml + modesHtml + `<div class="config-grid">${
      sections.map(s => `
        <div class="config-section">
          <h3>${s.label}</h3>
          <table class="config-table">
            ${s.rows.map(r => `
              <tr>
                <td class="config-key">${r.k}</td>
                <td class="config-val mono">${r.v}</td>
              </tr>`).join('')}
          </table>
        </div>`).join('')
    }</div>`;
  }).catch(() => {
    document.getElementById('config-content').innerHTML = '<div class="faint">Failed to load config.</div>';
  });
}

// UI-only toggle: flips the visible status + button label on the tile.
// Does NOT hit the backend or change any runtime flag.
function toggleMode(id) {
  const tile = document.querySelector(`.mode-tile[data-mode="${id}"]`);
  if (!tile) return;
  const status = tile.querySelector('.mode-status');
  const btn = tile.querySelector('.mode-toggle');
  const on = status.classList.contains('on');
  status.classList.toggle('on', !on);
  status.classList.toggle('off', on);
  status.textContent = on ? 'Disabled' : 'Enabled';
  btn.classList.toggle('off', on);
  btn.textContent = on ? 'Enable' : 'Disable';
}

function setRefreshBtn(state) {
  const btn = document.getElementById('btn-refresh');
  if (!btn) return;
  if (state === 'running') {
    btn.disabled = true;
    btn.textContent = 'Refresh…';
  } else {
    btn.disabled = false;
    btn.textContent = 'Refresh';
  }
}

function scanCityCount() {
  const cfg = window._lastConfig || {};
  return Array.isArray(cfg.locations) && cfg.locations.length ? cfg.locations.length : null;
}

async function refresh() {
  try {
    setStatus('warning', 'Refreshing…');
    const state = await loadJson('/api/state');
    window._lastConfig = state.config || {};
    renderCards(state);
    renderChart(state);
    renderBreakdown(state);
    renderPositions(state);
    renderSignals(state);
    renderResolved(state);
    const now = new Date();
    const t = now.toLocaleTimeString('en-AU', { timeZone: 'Australia/Perth' });
    document.getElementById('last-updated').textContent = t + ' AWST';
    setStatus('ok', 'Live · 30m Refresh');
  } catch (e) {
    setStatus('error', 'Connection lost');
    console.error(e);
  } finally {
    setRefreshBtn('idle');
  }
}

async function manualRefresh() {
  setRefreshBtn('running');
  await refresh();
}

refresh();
setInterval(refresh, 1800000);

let _scanPollTimer = null;

function showToast(title, body, color) {
  const toast = document.getElementById('scan-toast');
  document.getElementById('scan-toast-title').textContent = title;
  document.getElementById('scan-toast-title').style.color = color || 'var(--text-primary)';
  document.getElementById('scan-toast-body').innerHTML = body;
  toast.style.display = 'block';
}

function renderScanProgressToast(s) {
  const elapsed = s.started_at ? Math.round((Date.now() - new Date(s.started_at).getTime()) / 1000) : '?';
  const cityCount = scanCityCount();
  const cityLabel = cityCount ? `${cityCount} cities` : 'configured cities';
  const progress = Math.max(2, Math.min(97, Number(s.progress || 0)));
  const stage = s.stage_label || 'Scanning';
  const detail = s.detail || `Working through ${cityLabel}`;
  showToast('Scanning…', `
    <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:8px">
      <span style="font-weight:600;color:var(--text-primary)">${escapeHtml(stage)}</span>
      <span style="font-family:'JetBrains Mono',monospace;color:var(--text-faint)">${progress}%</span>
    </div>
    <div style="height:10px;border-radius:999px;background:rgba(255,255,255,0.08);overflow:hidden;border:1px solid rgba(255,255,255,0.06)">
      <div style="height:100%;width:${progress}%;background:linear-gradient(90deg, var(--accent-blue), var(--accent-cyan), var(--accent-green));border-radius:999px;transition:width 900ms ease"></div>
    </div>
    <div style="display:flex;justify-content:space-between;gap:12px;margin-top:8px;color:var(--text-secondary)">
      <span>${escapeHtml(detail)}</span>
      <span style="font-family:'JetBrains Mono',monospace;color:var(--text-faint)">${elapsed}s</span>
    </div>
    <div style="margin-top:6px;color:var(--text-faint);font-size:11px">Scanning ${cityLabel}</div>
  `, 'var(--accent-amber)');
}

function setScanBtn(state) {
  const btn = document.getElementById('btn-scan');
  if (state === 'running') {
    btn.disabled = true;
    btn.textContent = 'Scanning…';
  } else {
    btn.disabled = false;
    btn.textContent = 'Scan Now';
  }
}

function pollScanStatus() {
  fetch('/api/scan/status', {cache: 'no-store'})
    .then(r => r.json())
    .then(s => {
      if (s.status === 'running') {
        renderScanProgressToast(s);
        _scanPollTimer = setTimeout(pollScanStatus, 1500);
      } else if (s.status === 'done') {
        clearTimeout(_scanPollTimer);
        setScanBtn('idle');
        const lines = (s.summary || ['No trades']).map(l => `<div style="margin:2px 0;font-family:monospace;font-size:11px">${l}</div>`).join('');
        showToast('Scan complete', lines, 'var(--accent-green)');
        setTimeout(refresh, 1000);
      } else if (s.status === 'error') {
        clearTimeout(_scanPollTimer);
        setScanBtn('idle');
        showToast('Scan failed', `<span style="color:var(--accent-red)">${s.error || 'Unknown error'}</span>`, 'var(--accent-red)');
      }
    })
    .catch(() => {
      clearTimeout(_scanPollTimer);
      setScanBtn('idle');
    });
}

async function triggerScan() {
  try {
    setScanBtn('running');
    showToast('Scan started', 'Initialising weather scan…', 'var(--accent-amber)');
    const r = await fetch('/api/scan', {method: 'POST', cache: 'no-store'});
    const data = await r.json();
    if (!data.ok) {
      showToast('Already running', data.error || '', 'var(--accent-amber)');
      setScanBtn('running');
      pollScanStatus();
      return;
    }
    pollScanStatus();
  } catch(e) {
    setScanBtn('idle');
    showToast('Error', String(e), 'var(--accent-red)');
  }
}

// Resume polling if a scan was running before page load
fetch('/api/scan/status', {cache: 'no-store'}).then(r => r.json()).then(s => {
  if (s.status === 'running') { setScanBtn('running'); pollScanStatus(); }
});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Env loading (must happen before API calls)
# ---------------------------------------------------------------------------

def _load_env():
    """Load .env from project root (preferred) or skill directory into os.environ."""
    for env_file in (_PROJECT_ROOT / ".env", SKILL_DIR / ".env"):
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()
            return

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


def _parse_sort_dt(value: str | None) -> datetime:
    """Best-effort parser for dashboard trade sorting."""
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    raw = str(value).strip()
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        if len(raw) == 10:
            return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)



def _resolved_trade_sort_key(trade: dict) -> tuple[datetime, datetime, datetime]:
    """Sort resolved history by market resolution date first, then settlement time."""
    resolution_date = _parse_sort_dt(
        trade.get("resolution_date") or trade.get("target_date") or (trade.get("resolved_at") or "")[:10]
    )
    resolved_at = _parse_sort_dt(trade.get("resolved_at"))
    entered_at = _parse_sort_dt(trade.get("entered_at"))
    return (resolution_date, resolved_at, entered_at)


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


def _fetch_price_via_clob(token_id: str) -> tuple[float | None, str | None]:
    """Hit Polymarket CLOB directly when we already know the YES token id.
    Prefer midpoint for mark-to-market so dashboard P&L reflects a fair live mark
    instead of the more punitive best-bid liquidation quote."""
    if not token_id:
        return None, None
    try:
        import requests
        mid_resp = requests.get(
            "https://clob.polymarket.com/midpoint",
            params={"token_id": token_id},
            timeout=5,
        )
        if mid_resp.status_code == 200:
            mid = _coerce_price(mid_resp.json().get("mid"))
            if mid is not None and mid > 0:
                return mid, "clob_midpoint"

        resp = requests.get(
            "https://clob.polymarket.com/price",
            params={"token_id": token_id, "side": "buy"},
            timeout=5,
        )
        if resp.status_code != 200:
            return None, None
        price = _coerce_price(resp.json().get("price"))
        return price, ("clob_buy" if price is not None else None)
    except Exception:
        return None, None


def _mark_missing_position(p: dict, reason: str = "live_price_unavailable") -> dict:
    p["current_price"] = None
    p["upnl"] = None
    p["price_source"] = None
    p["mark_status"] = "missing"
    p["price_error"] = reason
    return p


def _fetch_live_mark(market_id: str, polymarket_token_id: str | None = None) -> tuple[float | None, str | None]:
    """Fetch current mark and its source for a market.

    Priority:
      1. Stored polymarket_token_id (CLOB direct — no Simmer, no Gamma).
      2. Integer Gamma market id → Gamma → CLOB.
      3. UUID Simmer market id → Simmer fallback (legacy rows without token_id).

    Goal is to avoid Simmer for live-price refresh whenever possible. Once the
    backfill (scripts/backfill_token_ids.py) populates token_ids on legacy
    rows, branch (3) effectively goes dark.
    """
    if polymarket_token_id:
        price, source = _fetch_price_via_clob(polymarket_token_id)
        if price is not None:
            return price, source

    if not market_id:
        return None, None

    # Plain integer = Gamma ID (e.g. "2019315"). Simmer can't resolve these.
    # Route through Gamma API → extract clobTokenIds → CLOB.
    if market_id.isdigit():
        try:
            import requests
            gamma_resp = requests.get(
                f"https://gamma-api.polymarket.com/markets/{market_id}",
                timeout=5,
            )
            if gamma_resp.status_code != 200:
                return None, None
            market = gamma_resp.json()
            if isinstance(market, dict):
                ctids_raw = market.get("clobTokenIds", "")
                if ctids_raw:
                    import json
                    ctids = json.loads(ctids_raw)
                    yes_token = ctids[0] if len(ctids) > 0 else None
                    if yes_token:
                        price, source = _fetch_price_via_clob(yes_token)
                        if price is not None:
                            return price, f"gamma_{source}" if source else "gamma_clob"
        except Exception:
            pass
        return None, None

    # UUID-format market_id — use Simmer (works for CLOB UUIDs)
    try:
        api_key = os.environ.get("SIMMER_API_KEY")
        if not api_key:
            return None, None
        import requests
        resp = requests.get(
            f"https://api.simmer.markets/api/sdk/context/{market_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        if resp.status_code != 200:
            return None, None
        data = resp.json()
        if isinstance(data, dict):
            price = _coerce_price(data.get("market", {}).get("current_price"))
            if price is not None:
                return price, "simmer_context"
        return None, None
    except Exception:
        return None, None


def _extract_simmer_mark(pos: dict) -> tuple[float | None, str | None]:
    for key, source in (
        ("current_price", "simmer_positions"),
        ("price_yes", "simmer_price_yes"),
        ("price", "simmer_price"),
    ):
        if key in pos and pos.get(key) is not None and pos.get(key) != "":
            price = _coerce_price(pos.get(key))
            if price is not None:
                return price, source
    market_id = str(pos.get("market_id") or pos.get("id") or "")
    token_id = pos.get("polymarket_token_id") or pos.get("yes_token_id") or pos.get("token_id")
    token_id = str(token_id) if token_id else None
    return _fetch_live_mark(market_id, polymarket_token_id=token_id)


def _compute_upnl(pos: dict) -> float | None:
    shares_yes = float(pos.get("shares_yes") or 0)
    shares_no = float(pos.get("shares_no") or 0)
    shares = float(pos.get("shares") or 0)
    side = (pos.get("side") or "yes").lower()
    entry = _coerce_price(pos.get("entry_price"))
    current = _coerce_price(pos.get("current_price"))
    if entry is None or current is None:
        return None
    if shares_yes > 0:
        return round((current - entry) * shares_yes, 2)
    if shares_no > 0:
        # NO token settles at (1 - yes_price). Paid `entry` per NO share.
        return round(((1 - current) - entry) * shares_no, 2)
    # Paper journal fallback — respect side
    if shares > 0:
        if side == "yes":
            return round((current - entry) * shares, 2)
        return round(((1 - current) - entry) * shares, 2)
    return 0.0


def _enrich_positions(positions: list[dict]) -> list[dict]:
    """Add current_price and upnl to paper journal positions concurrently."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def enrich_one(p: dict) -> dict:
        p = dict(p)
        market_id = str(p.get("market_id") or "")
        token_id = p.get("polymarket_token_id")
        token_id = str(token_id) if token_id else None
        if token_id or market_id:
            cp, source = _fetch_live_mark(market_id, polymarket_token_id=token_id)
            if cp is not None:
                p["current_price"] = cp
                p["upnl"] = _compute_upnl(p)
                p["price_source"] = source
                p["mark_status"] = "live"
                p["price_error"] = ""
                return p
        return _mark_missing_position(p)

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(enrich_one, p): p for p in positions}
        enriched = []
        for future in as_completed(futures, timeout=40):
            try:
                enriched.append(future.result())
            except Exception:
                p = dict(futures[future])
                enriched.append(_mark_missing_position(p, reason="mark_lookup_error"))
    return enriched


def _parse_signals_from_history() -> list[dict]:
    """Return all strong/moderate signals from the latest scan window.

    Reads forecast_history.jsonl (every signal the bot evaluated, not just gate-passing
    ones), filters to entries within the latest scan window, dedupes by event, and
    keeps strong + moderate. Single source of truth for the dashboard panel.

    Past-date markets are excluded so resolved D-1 noise does not linger on the dashboard.
    """
    history = _load_trades_jsonl(SCAN_LOG)
    if not history:
        return []

    # Find the latest scan timestamp; window = anything within 15 minutes of it.
    # A single scan emits all its forecasts within a few minutes, so 15min is loose
    # enough to catch a slow scan without bleeding into the previous one (cron is
    # 4h apart for the main scan, so no risk of overlap).
    latest_ts: datetime | None = None
    for e in reversed(history):
        if isinstance(e, dict) and e.get("logged_at"):
            try:
                latest_ts = datetime.fromisoformat(e["logged_at"])
                break
            except ValueError:
                continue
    if latest_ts is None:
        return []
    window_start = latest_ts - timedelta(minutes=15)

    # Collect strong/moderate within window, dedup by (loc, date, metric) → keep latest
    by_key: dict[tuple, dict] = {}
    today_awst = datetime.now(ZoneInfo("Australia/Perth")).date()
    for e in history:
        if not isinstance(e, dict):
            continue
        ts_str = e.get("logged_at")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if ts < window_start:
            continue
        signal = e.get("signal_strength", "")
        if signal not in ("strong", "moderate"):
            continue
        loc = e.get("location", "")
        date = e.get("target_date", "")
        metric = e.get("metric", "")
        if not loc:
            continue
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if target_date < today_awst:
            continue
        key = (loc, date, metric)
        prev = by_key.get(key)
        if prev is None or ts > datetime.fromisoformat(prev["_ts"]):
            by_key[key] = {**e, "_ts": ts_str}

    # Build trade index keyed by (location, target_date, metric) → most informative trade
    trade_index: dict[tuple, dict] = {}
    try:
        for t in _load_trades_jsonl(PAPER_TRADES):
            if not isinstance(t, dict):
                continue
            tk = (t.get("location", ""), t.get("target_date", ""), t.get("metric", ""))
            if not all(tk):
                continue
            prev = trade_index.get(tk)
            # Prefer open over resolved; otherwise newest
            if prev is None:
                trade_index[tk] = t
                continue
            if prev.get("status") != "open" and t.get("status") == "open":
                trade_index[tk] = t
            elif prev.get("status") == t.get("status"):
                if (t.get("entered_at") or "") > (prev.get("entered_at") or ""):
                    trade_index[tk] = t
    except Exception:
        pass

    # Build skip-reason index keyed by (location, date, metric) → set of reasons in window
    skip_index: dict[tuple, set] = {}
    try:
        if SKIP_LOG.exists():
            with SKIP_LOG.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        s = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sk_ts = s.get("ts")
                    if not sk_ts:
                        continue
                    try:
                        ts = datetime.fromisoformat(sk_ts)
                    except ValueError:
                        continue
                    if ts < window_start:
                        continue
                    sk = (s.get("location", ""), s.get("date", ""), s.get("metric", ""))
                    reason = (s.get("reason") or "").split(":")[0]
                    if not reason or not all(sk):
                        continue
                    skip_index.setdefault(sk, set()).add(reason)
    except Exception:
        pass

    def _badge_for(loc: str, date: str, metric: str) -> tuple[str, str]:
        """Return (label, color_class) for a signal's status badge.
        Color classes mirror existing dashboard CSS conventions: positive (green),
        negative (red), neutral (gray), info (blue), warning (amber)."""
        tk = (loc, date, metric)
        trade = trade_index.get(tk)
        if trade:
            status = trade.get("status")
            strategy = (trade.get("strategy") or "core").upper()
            if status == "open":
                return (f"OPEN · {strategy}", "info")
            if status == "resolved":
                pnl = float(trade.get("pnl") or 0)
                if pnl > 0:
                    return (f"WIN · {strategy} +${pnl:.0f}", "positive")
                return (f"LOSS · {strategy} ${pnl:.0f}", "negative")
            return (f"{status or 'TRADED'} · {strategy}", "info")

        reasons = skip_index.get(tk, set())
        if not reasons:
            return ("EVAL", "neutral")
        # Precedence: D+0 (handed to LATE) > safeguards > price gates > bucket-math > stale
        if "d0_core_skip" in reasons:
            return ("D+0 → LATE", "info")
        if "safeguard" in reasons:
            return ("SAFEGUARD", "warning")
        if "high_spread" in reasons:
            return ("HIGH SPREAD", "warning")
        if "already_held" in reasons or "same_event_open" in reasons:
            return ("ALREADY HELD", "neutral")
        if "price_ceiling" in reasons or "no_bucket_price_extreme" in reasons:
            return ("PRICE EXTREME", "neutral")
        if "no_bucket_low_edge" in reasons:
            return ("LOW EDGE", "neutral")
        if "no_bucket_parseable" in reasons:
            return ("NO BUCKET", "neutral")
        if "stale_event" in reasons:
            return ("STALE", "neutral")
        if "punt_safeguard" in reasons:
            return ("PUNT SAFEGUARD", "warning")
        # Fallback: surface the first reason verbatim
        return (next(iter(reasons)).upper().replace("_", " "), "neutral")

    signals = []
    for entry in by_key.values():
        loc = entry.get("location", "")
        date = entry.get("target_date", "")
        metric = entry.get("metric", "")
        temp = entry.get("forecast_temp", "")
        signal = entry.get("signal_strength", "")
        models = entry.get("models_used", "")
        agree = entry.get("agreement_pct", "")
        spread = entry.get("spread", "")
        badge_label, badge_color = _badge_for(loc, date, metric)
        signals.append({
            "location": loc,
            "date": date,
            "metric": metric,
            "temp": str(temp) if temp not in ("", None) else "—",
            "signal": signal,
            "models": str(models) if models not in ("", None) else "—",
            "agree": str(round(float(agree), 1)) + "%" if agree not in ("", None) else "N/A",
            "spread": str(spread) if spread not in ("", None) else "—",
            "polymarket_url": polymarket_event_url(loc, date, metric),
            "status_badge": badge_label,
            "status_color": badge_color,
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
    marked = [p for p in enriched_positions if p.get("upnl") is not None]
    missing_marks = max(0, len(enriched_positions) - len(marked))
    unrealized = round(sum(float(p.get("upnl") or 0) for p in marked), 2)
    return {
        "balance": round(10000.0 + realized, 2),
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "marked_positions": len(marked),
        "missing_marks": missing_marks,
    }


def _get_stats() -> dict:
    trades = _load_trades_jsonl(PAPER_TRADES)
    resolved = [t for t in trades if t.get("status") == "resolved"]
    open_trades = [t for t in trades if t.get("status") == "open"]

    today = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
    today_resolved = [t for t in resolved if (t.get("resolved_at") or "")[:10] == today]
    today_pnl = round(sum(float(t.get("pnl") or 0) for t in today_resolved), 2)

    if not resolved:
        return dict(total_trades=len(trades), open_trades=len(open_trades),
                    resolved_trades=0, wins=0, losses=0, win_rate=None,
                    total_pnl=0.0, avg_pnl=0.0, best_trade=None, worst_trade=None,
                    today_pnl=today_pnl, today_trades=len(today_resolved))
    pnls = [float(t.get("pnl") or 0) for t in resolved]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return dict(
        total_trades=len(trades),
        open_trades=len(open_trades),
        resolved_trades=len(resolved),
        wins=len(wins),
        losses=len(losses),
        win_rate=round(len(wins) / (len(wins) + len(losses)) * 100, 1) if (wins or losses) else None,
        total_pnl=round(sum(pnls), 4),
        avg_pnl=round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
        best_trade=max(pnls) if pnls else None,
        worst_trade=min(pnls) if pnls else None,
        today_pnl=today_pnl,
        today_trades=len(today_resolved),
    )


def _get_last_scan_time() -> str | None:
    """Return most recent scan time — prefers forecast_history logged_at, falls back to forecast_cache mtime."""
    history = _load_trades_jsonl(SCAN_LOG)
    last_logged: datetime | None = None
    for e in reversed(history):
        if isinstance(e, dict) and e.get("logged_at"):
            try:
                last_logged = datetime.fromisoformat(e["logged_at"])
            except ValueError:
                pass
            break

    # Check forecast_cache mtime as fallback (written by weather_trader on every run)
    cache_path = SKILL_DIR / "data" / "forecast_cache.json"
    last_cache: datetime | None = None
    if cache_path.exists():
        mtime = cache_path.stat().st_mtime
        last_cache = datetime.fromtimestamp(mtime, tz=timezone.utc)

    if last_logged and last_cache:
        return max(last_logged, last_cache).isoformat()
    if last_cache:
        return last_cache.isoformat()
    if last_logged:
        return last_logged.isoformat()
    return None


# ---------------------------------------------------------------------------
# Scan runner (background thread)
# ---------------------------------------------------------------------------

import subprocess
import threading

_scan_lock = threading.Lock()
_scan_state: dict = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "summary": None,
    "error": None,
    "progress": 0,
    "stage": "idle",
    "stage_label": "Idle",
    "detail": None,
}

_LOCATIONS = (
    "NYC,Chicago,Seattle,Atlanta,Dallas,Miami,Houston,San Francisco,Phoenix,LA,"
    "Tel Aviv,Munich,London,Tokyo,Seoul,Ankara,Lucknow,Wellington,Toronto,Paris,"
    "Milan,Sao Paulo,Warsaw,Singapore,Hong Kong"
)
_TRADER_SCRIPT = str(Path(__file__).resolve().parent / "weather_trader.py")
_VENV_PYTHON = "/home/brandon/.openclaw/venv/bin/python3"
_SIMMER_KEY_FILE = "/home/brandon/.openclaw/workspace/SOLEBRACE/API/.simmer-key"

_SCAN_STAGE_LABELS = {
    "initializing": "Initialising scan",
    "discovering": "Discovering markets",
    "loading": "Loading market data",
    "forecasting": "Fetching forecasts",
    "ranking": "Ranking candidates",
    "trading": "Executing entries",
    "punt": "Running punt pass",
    "exits": "Checking exits",
    "finalizing": "Finalising",
    "done": "Complete",
    "error": "Scan failed",
}


def _set_scan_progress(progress: int | None = None, stage: str | None = None, detail: str | None = None) -> None:
    with _scan_lock:
        if progress is not None:
            _scan_state["progress"] = max(int(_scan_state.get("progress", 0)), int(progress))
        if stage is not None:
            _scan_state["stage"] = stage
            _scan_state["stage_label"] = _SCAN_STAGE_LABELS.get(stage, stage.replace("_", " ").title())
        if detail is not None:
            _scan_state["detail"] = detail


def _scan_progress_from_line(line: str) -> tuple[int | None, str | None, str | None]:
    stripped = line.strip()
    if not stripped:
        return None, None, None
    if "🔍 Discovering new weather markets" in stripped:
        return 10, "discovering", "Searching Polymarket weather events"
    if "📡 Fetching weather markets" in stripped:
        return 24, "loading", "Pulling live market books"
    if "Fetching ensemble forecast" in stripped:
        return 42, "forecasting", "Refreshing ensemble forecasts"
    if stripped.startswith("🏆 Ranked"):
        return 68, "ranking", stripped
    if " BUY " in stripped or " GTC pending " in stripped:
        return 78, "trading", stripped
    if stripped.startswith("🎯 Punt pass") or " PUNT " in stripped:
        return 86, "punt", stripped
    if "Checking " in stripped and "weather positions for exit" in stripped:
        return 92, "exits", stripped
    if "Events scanned:" in stripped or "Entry opportunities:" in stripped or "Trades executed:" in stripped or "Punts executed:" in stripped:
        return 96, "finalizing", stripped
    if "Error" in stripped or "error" in stripped:
        return 96, "finalizing", stripped
    return None, None, None


def _run_scan_bg():
    global _scan_state
    try:
        api_key = ""
        try:
            api_key = Path(_SIMMER_KEY_FILE).read_text().strip()
        except Exception:
            api_key = os.environ.get("SIMMER_API_KEY", "")

        env = dict(os.environ)
        env["SIMMER_API_KEY"] = api_key
        env["SIMMER_WEATHER_LOCATIONS"] = _LOCATIONS

        summary_lines: list[str] = []
        output_lines: list[str] = []
        _set_scan_progress(4, "initializing", "Booting weather trader")

        proc = subprocess.Popen(
            [_VENV_PYTHON, _TRADER_SCRIPT, "--smart-sizing", "--punt-mode"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(Path(_TRADER_SCRIPT).parent),
        )

        def _reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                output_lines.append(line)
                stripped = line.strip()
                progress, stage, detail = _scan_progress_from_line(line)
                if progress is not None or stage is not None or detail is not None:
                    _set_scan_progress(progress, stage, detail)
                keep = (
                    "Events scanned:" in stripped
                    or "Entry opportunities:" in stripped
                    or "Trades executed:" in stripped
                    or "Punts executed:" in stripped
                    or " BUY " in stripped
                    or " PUNT " in stripped
                    or " Sold " in stripped
                    or " GTC pending " in stripped
                    or "Error" in stripped
                    or "error" in stripped
                )
                if keep:
                    summary_lines.append(stripped)

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()

        started = time.time()
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if time.time() - started > 480:
                proc.kill()
                reader.join(timeout=2)
                raise subprocess.TimeoutExpired(proc.args, 480)
            time.sleep(0.25)

        reader.join(timeout=2)
        output = "".join(output_lines)
        if proc.returncode not in (0, None):
            err_line = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), f"Scan exited with code {proc.returncode}")
            with _scan_lock:
                _scan_state.update({
                    "status": "error",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "error": err_line,
                    "summary": summary_lines or None,
                    "stage": "error",
                    "stage_label": _SCAN_STAGE_LABELS["error"],
                    "detail": err_line,
                })
            return

        with _scan_lock:
            _scan_state.update({
                "status": "done",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "summary": summary_lines or ["Scan complete — no signals"],
                "error": None,
                "progress": 100,
                "stage": "done",
                "stage_label": _SCAN_STAGE_LABELS["done"],
                "detail": "Scan complete",
            })
    except subprocess.TimeoutExpired:
        with _scan_lock:
            _scan_state.update({
                "status": "error",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": "Timed out after 8 min",
                "summary": None,
                "progress": max(int(_scan_state.get("progress", 0)), 96),
                "stage": "error",
                "stage_label": _SCAN_STAGE_LABELS["error"],
                "detail": "Timed out after 8 min",
            })
    except Exception as exc:
        with _scan_lock:
            _scan_state.update({
                "status": "error",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
                "summary": None,
                "stage": "error",
                "stage_label": _SCAN_STAGE_LABELS["error"],
                "detail": str(exc),
            })


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="AIFS ENS Weather Scan Dashboard")

@app.get("/", response_class=HTMLResponse)
def home():
    return DASHBOARD_HTML


@app.post("/api/scan")
def api_scan_trigger():
    """Kick off a fresh weather scan in the background."""
    with _scan_lock:
        if _scan_state["status"] == "running":
            return JSONResponse({"ok": False, "error": "Scan already running"}, status_code=409)
        _scan_state.update({
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "summary": None,
            "error": None,
            "progress": 3,
            "stage": "initializing",
            "stage_label": _SCAN_STAGE_LABELS["initializing"],
            "detail": "Preparing scan",
        })
    t = threading.Thread(target=_run_scan_bg, daemon=True)
    t.start()
    return JSONResponse({"ok": True, "status": "running"})


@app.get("/api/scan/status")
def api_scan_status():
    with _scan_lock:
        return JSONResponse(dict(_scan_state))


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
            current_price, price_source = _extract_simmer_mark(p)
            pos_for_pnl = dict(p)
            pos_for_pnl["current_price"] = current_price
            upnl = _compute_upnl(pos_for_pnl)
            positions.append({
                "question": q,
                "side": "YES" if float(p.get("shares_yes") or 0) > 0 else "NO",
                "shares": float(p.get("shares_yes") or p.get("shares_no") or 0),
                "entry_price": float(p.get("entry_price") or 0),
                "current_price": current_price,
                "upnl": upnl,
                "target_date": tgt,
                "location": loc,
                "strategy": p.get("strategy") or "core",
                "market_id": p.get("market_id") or p.get("id") or "",
                "price_source": price_source,
                "mark_status": "live" if upnl is not None else "missing",
                "price_error": "" if upnl is not None else "live_price_unavailable",
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
                "current_price": p.get("current_price"),
                "upnl": p.get("upnl"),
                "target_date": tgt,
                "location": loc,
                "strategy": p.get("strategy") or "core",
                "market_id": p.get("market_id", ""),
                "price_source": p.get("price_source"),
                "mark_status": p.get("mark_status") or ("live" if p.get("upnl") is not None else "missing"),
                "price_error": p.get("price_error") or "",
                "polymarket_url": polymarket_event_url(loc, tgt, metric),
            })

    resolved = sorted(resolved, key=_resolved_trade_sort_key, reverse=True)

    return JSONResponse({
        "portfolio": _get_portfolio_stats(positions),
        "stats": _get_stats(),
        "timeseries": _build_timeseries(trades),
        "positions": positions,
        "signals": _parse_signals_from_history(),
        "signals_last_scan": _get_last_scan_time(),
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
                "target_date": t.get("target_date", ""),
                "entered_at": t.get("entered_at", ""),
                "resolved_at": t.get("resolved_at", ""),
                "resolution_date": t.get("resolution_date", ""),
                "resolution_source": t.get("resolution_source", ""),
                "exit_reason": t.get("exit_reason", ""),
                "strategy": t.get("strategy") or "core",
                "forecast_temp": t.get("forecast_temp"),
                "actual_temp": t.get("actual_temp"),
                "bucket": t.get("bucket", ""),
                "polymarket_url": polymarket_event_url(t.get("location", ""), t.get("target_date", ""), t.get("metric") or "high"),
            }
            for t in resolved
        ],
        "config": _get_config(),
    })


@app.get("/api/config")
def api_config():
    """Return the current bot configuration."""
    return JSONResponse(_get_config())


def _get_config() -> dict:
    """Load and format the bot config for the dashboard."""
    # Load config.json
    config = {}
    if CONFIG_FILE.exists():
        try:
            config = json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass

    # Load model weights from ensemble_forecast (single source of truth)
    try:
        from ensemble_forecast import ENSEMBLE_MODELS
        ensemble_models = dict(ENSEMBLE_MODELS)
    except Exception:
        ensemble_models = {}

    # Detect API key presence (masked)
    simmer_key = os.environ.get("SIMMER_API_KEY", "")
    has_simmer = bool(simmer_key)

    # Locations — pull the canonical scan list from weather_trader (single
    # source of truth). Fall back to env / config only if the import fails.
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from weather_trader import DEFAULT_LOCATIONS as _default_locs
    except Exception:
        _default_locs = "NYC"
    locations_raw = os.environ.get("SIMMER_WEATHER_LOCATIONS") or config.get("locations") or _default_locs
    locations = [l.strip() for l in locations_raw.split(",")] if locations_raw else []

    return {
        # Core trading params
        "entry_threshold":    config.get("entry_threshold", 0.50),
        "min_edge":           config.get("min_edge", 0.25),
        "exit_threshold":     config.get("exit_threshold", 0.45),
        "max_position_usd":   config.get("max_position_usd", 200.0),
        "sizing_pct":         config.get("sizing_pct", 0.05),
        "max_trades_per_run": config.get("max_trades_per_run", 10),
        "paper_balance":      config.get("paper_balance", 10000.0),
        "order_type":         config.get("order_type", "GTC"),
        # Vol targeting
        "vol_targeting":       config.get("vol_targeting", False),
        "target_vol":          config.get("target_vol", 0.20),
        "vol_max_leverage":    config.get("vol_max_leverage", 2.0),
        "vol_min_allocation": config.get("vol_min_allocation", 0.2),
        "vol_span":           config.get("vol_span", 10),
        "max_daily_loss_usd": config.get("max_daily_loss_usd", 0.0),
        # Profit taking
        "exit_profit_multiplier": config.get("exit_profit_multiplier", 4.0),
        "ladder_first_exit":      config.get("ladder_first_exit", 0.0),
        "ladder_first_fraction":  config.get("ladder_first_fraction", 0.5),
        # Filters
        "slippage_max":      config.get("slippage_max", 0.15),
        "min_liquidity":     config.get("min_liquidity", 0.0),
        "binary_only":       config.get("binary_only", False),
        # Punt mode
        "punt_mode":            config.get("punt_mode", True),
        "punt_max_position_usd": config.get("punt_max_position_usd", 15.0),
        "punt_price_ceiling":   config.get("punt_price_ceiling", 0.149),
        "punt_min_edge":        config.get("punt_min_edge", 0.50),
        "punt_min_confidence":  config.get("punt_min_confidence", 0.70),
        "punt_daily_budget_usd": config.get("punt_daily_budget_usd", 100.0),
        # Late mode (day-of intraday)
        "late_mode":             config.get("late_mode", True),
        "late_price_ceiling":    config.get("late_price_ceiling", 0.90),
        "late_max_position_usd": config.get("late_max_position_usd", 125.0),
        "late_entry_hour":       config.get("late_entry_hour", 15),
        "late_edge_buffer_c":    config.get("late_edge_buffer_c", 0.3),
        "late_cities":           config.get("late_cities", "London,Toronto,Singapore,Sao Paulo,Shanghai,Tokyo,Beijing,Los Angeles,Miami,Seattle,Chicago,Dallas"),
        # Discovery
        "discovery_cache_minutes": config.get("discovery_cache_minutes", 180),
        "forecast_cache_disk":     config.get("forecast_cache_disk", True),
        "concurrent_scans":        config.get("concurrent_scans", True),
        "log_level":              config.get("log_level", "INFO"),
        # Models
        "ensemble_models": ensemble_models,
        "models_count": len(ensemble_models),
        # Cities
        "locations": locations,
        # API keys (presence only, never the actual key)
        "has_simmer_key": has_simmer,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8414, log_level="warning")
