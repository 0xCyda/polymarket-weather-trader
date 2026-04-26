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
SKILL_DIR = _BASE
# Data files live at project root/data/, one level up from scripts/
_PROJECT_ROOT = _BASE.parent
DATA_DIR = _PROJECT_ROOT / "data"
SCAN_LOG = DATA_DIR / "forecast_history.jsonl"
PAPER_TRADES = DATA_DIR / "paper_trades.jsonl"
LATEST_CANDIDATES_FILE = SKILL_DIR / "data" / "latest_candidates.json"
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
  <div class="status-line">
    <div class="status-pill" id="status-pill">
      <span class="status-dot" id="status-dot"></span>
      <span id="status-text">Connecting…</span>
    </div>
    <div class="last-updated">Last updated: <span id="last-updated">—</span></div>
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
    <h2>Open Positions</h2>
    <table id="positions-table"></table>
  </div>
  <div class="card">
    <h2>AIFS ENS Signals <span id="signals-scan-time" class="faint" style="font-size:11px;font-weight:400;text-transform:none;letter-spacing:0;margin-left:4px"></span></h2>
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
    metricCard('Today P&L', fmtPnl(d.stats.today_pnl || 0), {
      tone: (d.stats.today_pnl || 0) > 0 ? 'positive' : (d.stats.today_pnl || 0) < 0 ? 'negative' : null,
      sub: `${d.stats.today_trades || 0} resolved today`,
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

  if (!signals.length) {
    container.innerHTML = emptyState('📡', 'No signals in latest scan');
    return;
  }

  const SIGNAL_ORDER = { strong: 0, moderate: 1, weak: 2 };
  const sorted = [...signals].sort((a, b) =>
    (SIGNAL_ORDER[a.signal] ?? 3) - (SIGNAL_ORDER[b.signal] ?? 3)
  );

  const INITIAL = 5;
  if (window._signalExpanded === undefined) window._signalExpanded = false;

  const visible = window._signalExpanded ? sorted : sorted.slice(0, INITIAL);
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
      <div class="faint" style="margin-top:4px">
        ${s.models} models · spread <span class="mono">${fmtSpreadForLoc(s.location, s.spread)}</span>${s.agree !== 'N/A' ? ' · ' + s.agree + ' agree' : ''}
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

const RESOLVED_PAGE_SIZE = 20;

function renderResolved(d) {
  const container = document.getElementById('resolved-table');
  // Accept state object or bare array (for re-render on page change)
  const all = Array.isArray(d) ? d : (d && d.resolved) || [];
  if (!all.length) {
    container.innerHTML = `<tr><td colspan="9">${emptyState('✅', 'No resolved trades yet')}</td></tr>`;
    return;
  }

  // Newest first
  const sorted = all.slice().reverse();
  const totalPages = Math.max(1, Math.ceil(sorted.length / RESOLVED_PAGE_SIZE));
  if (window._resolvedPage === undefined) window._resolvedPage = 0;
  // Clamp page if data shrank between renders
  if (window._resolvedPage >= totalPages) window._resolvedPage = totalPages - 1;
  const page = window._resolvedPage;
  const start = page * RESOLVED_PAGE_SIZE;
  const slice = sorted.slice(start, start + RESOLVED_PAGE_SIZE);

  const headers = ['Location', 'Strategy', 'Outcome', 'Forecast', 'Actual', 'Entry', 'Exit', 'P&L', 'Resolved'];
  const rows = slice.map(t => {
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

    // Forecast temp
    let forecastCell = '—';
    if (t.forecast_temp != null) {
      forecastCell = `<span class="mono">${fmtTempForLoc(t.location, t.forecast_temp)}</span>`;
    }

    // Actual temp — show if recorded, otherwise blank
    let actualCell;
    if (t.actual_temp != null) {
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
      positionBadge(outcome),
      forecastCell,
      actualCell,
      `<span class="mono">$${entry.toFixed(3)}</span>`,
      `<span class="mono">$${exit.toFixed(3)}</span>`,
      winBadge(pnl),
      `<span class="mono">${resolvedDate.substring(0, 10)}</span>${srcBadge}`,
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
          { k: 'Daily budget', v: `$${cfg.late_daily_budget_usd}` },
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
        <p>A paper-trading bot that takes positions in Polymarket daily-temperature markets via the Simmer API. It pulls weather forecasts and live intraday observations, finds mispriced buckets, and sizes entries based on model confidence. All fills are simulated; real fills require a wallet key.</p>
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
              <li><strong>PUNT</strong>: tail lottery, buys deeply-mispriced tail buckets (&le;6¢) with fixed small stakes</li>
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
        const elapsed = s.started_at ? Math.round((Date.now() - new Date(s.started_at).getTime()) / 1000) : '?';
        showToast('Scanning…', `Fetching forecasts across 25 cities… (${elapsed}s)`, 'var(--accent-amber)');
        _scanPollTimer = setTimeout(pollScanStatus, 4000);
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


def _fetch_price_via_clob(token_id: str) -> float | None:
    """Hit Polymarket CLOB directly when we already know the YES token id.
    Avoids the Gamma resolve and the Simmer fallback entirely."""
    if not token_id:
        return None
    try:
        import requests
        resp = requests.get(
            "https://clob.polymarket.com/price",
            params={"token_id": token_id, "side": "buy"},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        return float(resp.json().get("price", 0) or 0)
    except Exception:
        return None


def _fetch_live_price(market_id: str, polymarket_token_id: str | None = None) -> float | None:
    """Fetch current price for a market.

    Priority:
      1. Stored polymarket_token_id (CLOB direct — no Simmer, no Gamma).
      2. Integer Gamma market id → Gamma → CLOB.
      3. UUID Simmer market id → Simmer fallback (legacy rows without token_id).

    Goal is to avoid Simmer for live-price refresh whenever possible. Once the
    backfill (scripts/backfill_token_ids.py) populates token_ids on legacy
    rows, branch (3) effectively goes dark.
    """
    if polymarket_token_id:
        price = _fetch_price_via_clob(polymarket_token_id)
        if price is not None:
            return price

    if not market_id:
        return None

    # Plain integer = Gamma ID (e.g. "2019315"). Simmer can't resolve these.
    # Route through Gamma API → extract clobTokenIds → CLOB /price.
    if market_id.isdigit():
        try:
            import requests
            gamma_resp = requests.get(
                f"https://gamma-api.polymarket.com/markets/{market_id}",
                timeout=5,
            )
            if gamma_resp.status_code != 200:
                return None
            market = gamma_resp.json()
            if isinstance(market, dict):
                ctids_raw = market.get("clobTokenIds", "")
                if ctids_raw:
                    import json
                    ctids = json.loads(ctids_raw)
                    yes_token = ctids[0] if len(ctids) > 0 else None
                    if yes_token:
                        clob_resp = requests.get(
                            "https://clob.polymarket.com/price",
                            params={"token_id": yes_token, "side": "buy"},
                            timeout=5,
                        )
                        if clob_resp.status_code == 200:
                            return float(clob_resp.json().get("price", 0) or 0)
        except Exception:
            pass
        return None

    # UUID-format market_id — use Simmer (works for CLOB UUIDs)
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
        token_id = p.get("polymarket_token_id")
        # Stored token_id lets us hit CLOB directly without Simmer or Gamma.
        # Legacy rows without token_id fall back to the existing chain (which
        # may still need SIMMER_API_KEY for UUID-format ids).
        if (token_id or market_id) and (token_id or api_key):
            cp = _fetch_live_price(market_id, polymarket_token_id=token_id)
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
    """Return latest actionable candidates when available, else fall back."""
    if LATEST_CANDIDATES_FILE.exists():
        try:
            payload = json.loads(LATEST_CANDIDATES_FILE.read_text())
            signals = []
            for e in payload.get("signals", []):
                loc = e.get("location", "")
                date = e.get("date", "")
                metric = e.get("metric", "")
                if not loc:
                    continue
                signals.append({
                    "location": loc,
                    "date": date,
                    "metric": metric,
                    "temp": str(e.get("temp")) if e.get("temp") not in ("", None) else "—",
                    "signal": e.get("signal") or "—",
                    "models": str(e.get("models")) if e.get("models") not in ("", None) else "—",
                    "agree": str(round(float(e.get("agree")), 1)) + "%" if e.get("agree") not in ("", None, "") else "N/A",
                    "spread": str(e.get("spread")) if e.get("spread") not in ("", None) else "—",
                    "polymarket_url": polymarket_event_url(loc, date, metric),
                })
            signals.sort(key=lambda s: (
                {"strong": 0, "moderate": 1, "weak": 2}.get(s["signal"], 3),
                s.get("location", ""),
            ))
            return signals
        except Exception:
            pass

    history = _load_trades_jsonl(SCAN_LOG)
    if not history:
        return []
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
        if loc and signal and signal != "weak":
            signals.append({
                "location": loc,
                "date": date,
                "metric": metric,
                "temp": str(temp) if temp else "—",
                "signal": signal,
                "models": str(models) if models else "—",
                "agree": str(round(float(agree), 1)) + "%" if agree not in ("", None) else "N/A",
                "spread": str(spread) if spread not in ("", None) else "—",
                "polymarket_url": polymarket_event_url(loc, date, metric),
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
_scan_state: dict = {"status": "idle", "started_at": None, "finished_at": None, "summary": None, "error": None}

_LOCATIONS = (
    "NYC,Chicago,Seattle,Atlanta,Dallas,Miami,Houston,San Francisco,Phoenix,LA,"
    "Tel Aviv,Munich,London,Tokyo,Seoul,Ankara,Lucknow,Wellington,Toronto,Paris,"
    "Milan,Sao Paulo,Warsaw,Singapore,Hong Kong"
)
_TRADER_SCRIPT = str(Path(__file__).resolve().parent / "weather_trader.py")
_VENV_PYTHON = "/home/brandon/.openclaw/venv/bin/python3"
_SIMMER_KEY_FILE = "/home/brandon/.openclaw/workspace/SOLEBRACE/API/.simmer-key"


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

        result = subprocess.run(
            [_VENV_PYTHON, _TRADER_SCRIPT, "--smart-sizing", "--punt-mode"],
            env=env,
            capture_output=True,
            text=True,
            timeout=480,
            cwd=str(Path(_TRADER_SCRIPT).parent),
        )
        output = result.stdout + result.stderr

        # Extract summary lines
        summary_lines = []
        for line in output.splitlines():
            stripped = line.strip()
            if any(k in stripped for k in ("Events scanned:", "Entry opportunities:", "Trades executed:", "Punts executed:", "BUY", "SELL", "punt", "Bought", "Sold", "Error", "error")):
                summary_lines.append(stripped)

        with _scan_lock:
            _scan_state.update({
                "status": "done",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "summary": summary_lines or ["Scan complete — no signals"],
                "error": None,
            })
    except subprocess.TimeoutExpired:
        with _scan_lock:
            _scan_state.update({"status": "error", "finished_at": datetime.now(timezone.utc).isoformat(), "error": "Timed out after 8 min", "summary": None})
    except Exception as exc:
        with _scan_lock:
            _scan_state.update({"status": "error", "finished_at": datetime.now(timezone.utc).isoformat(), "error": str(exc), "summary": None})


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
        _scan_state.update({"status": "running", "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None, "summary": None, "error": None})
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
                "resolved_at": t.get("resolved_at", ""),
                "resolution_date": t.get("resolution_date", ""),
                "resolution_source": t.get("resolution_source", ""),
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
        "punt_price_ceiling":   config.get("punt_price_ceiling", 0.06),
        "punt_min_edge":        config.get("punt_min_edge", 0.50),
        "punt_min_confidence":  config.get("punt_min_confidence", 0.70),
        "punt_daily_budget_usd": config.get("punt_daily_budget_usd", 100.0),
        # Late mode (day-of intraday)
        "late_mode":             config.get("late_mode", True),
        "late_price_ceiling":    config.get("late_price_ceiling", 0.90),
        "late_max_position_usd": config.get("late_max_position_usd", 100.0),
        "late_daily_budget_usd": config.get("late_daily_budget_usd", 500.0),
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
