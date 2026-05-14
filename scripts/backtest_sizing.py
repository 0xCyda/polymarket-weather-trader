#!/usr/bin/env python3
"""Shared backtest position sizing for weather strategy audits.

Backtests must mirror live/paper trading sizing, not use a flat stake:
- EASY cities: 3% of balance
- MEDIUM cities: 2% of balance
- HARD cities: 1% of balance

For historical sweeps we use the configured starting paper balance by default.
A sequential runner can pass a changing balance if it wants compounding.
"""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path('/home/brandon/projects/polymarket-weather-trader')
CONFIG = BASE / 'scripts/config.json'

RISK_PCT_BY_TIER = {"easy": 0.03, "medium": 0.02, "hard": 0.01}

# Keep synced with scripts/weather_trader.py. Unknown cities default to medium.
CITY_DIFFICULTY = {
    "TEL AVIV": "easy",
    "SAN FRANCISCO": "easy",
    "LOS ANGELES": "easy",
    "CHENGDU": "easy",
    "MUNICH": "easy",
    "MILAN": "easy",
    "WARSAW": "easy",
    "HONG KONG": "easy",  # retained from CORE audit; no Hans/Cold sample
    "TOKYO": "hard",
    "SHANGHAI": "hard",
}


def configured_paper_balance(default: float = 10000.0) -> float:
    try:
        cfg = json.loads(CONFIG.read_text())
        return float(cfg.get('paper_balance', default) or default)
    except Exception:
        return float(default)


def city_tier(location: str | None) -> str:
    return CITY_DIFFICULTY.get((location or '').upper(), 'medium')


def city_risk_pct(location: str | None) -> float:
    return RISK_PCT_BY_TIER[city_tier(location)]


def stake_for_city(location: str | None, balance: float | None = None) -> float:
    bal = configured_paper_balance() if balance is None else float(balance)
    return max(bal * city_risk_pct(location), 1.0)


def pnl_for_yes(win: bool, price: float, stake: float) -> float:
    if price <= 0:
        return 0.0
    if win:
        return stake * (1 - price) / price
    return -stake
