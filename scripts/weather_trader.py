#!/usr/bin/env python3
"""
Simmer Weather Trading Skill

Trades Polymarket weather markets using AIFS ENS + multi-model ensemble forecasts.
Inspired by gopfan2's $2M+ weather trading strategy.

Usage:
    python weather_trader.py              # Dry run (show opportunities, no trades)
    python weather_trader.py --live       # Execute real trades
    python weather_trader.py --positions  # Show current positions only
    python weather_trader.py --smart-sizing  # Use portfolio-based position sizing

Requires:
    SIMMER_API_KEY environment variable (get from simmer.markets/dashboard)
"""

import os
import sys
import time
import re
import json
import logging
import argparse
from datetime import datetime, timezone, timedelta
# urllib imports removed — legacy NOAA/Open-Meteo direct fetchers were deleted.
# Forecast data comes from ensemble_forecast.py which uses requests.

# Add scripts/ to path for ensemble modules
import pathlib as _p
_sys_path = _p.Path(__file__).parent / "scripts"
if str(_sys_path) not in sys.path:
    sys.path.insert(0, str(_sys_path))
from ensemble_forecast import get_ensemble_forecast
from aifs_forecast import prewarm_grib_cache

# Force line-buffered stdout so output is visible in non-TTY environments (cron, Docker, OpenClaw)
sys.stdout.reconfigure(line_buffering=True)

# Optional: Trade Journal integration for tracking
try:
    from tradejournal import log_trade
    JOURNAL_AVAILABLE = True
except ImportError:
    try:
        # Try relative import within skills package
        from skills.tradejournal import log_trade
        JOURNAL_AVAILABLE = True
    except ImportError:
        JOURNAL_AVAILABLE = False
        def log_trade(*args, **kwargs):
            pass  # No-op if tradejournal not installed

# Paper trading journal (local JSONL — no Simmer balance needed)
try:
    from paper_journal import log_paper_trade, update_resolved_trades, get_open_positions, get_stats, get_open_positions_by_event
    PAPER_JOURNAL_AVAILABLE = True
except ImportError:
    PAPER_JOURNAL_AVAILABLE = False
    def log_paper_trade(**kwargs): pass
    def update_resolved_trades(): return []
    def get_open_positions(): return []
    def get_stats(): return {}

# --------------------------------------------------------------------
# Error log — all non-200 responses, API errors, and unexpected
# exceptions are written here as structured JSON lines so the cron
# output stays clean and errors persist for post-mortem analysis.
# --------------------------------------------------------------------
_ERRORS_LOG = _p.Path(__file__).parent / "errors.log"

def log_error(kind: str, msg: str, **ctx):
    """Append a structured entry to errors.log."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "msg": msg,
        **ctx,
    }
    try:
        _ERRORS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_ERRORS_LOG, "a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # Never let logging itself break the bot

# Forecast accuracy history (observability only — doesn't affect trading)
try:
    from forecast_history import log_forecast
    FORECAST_HISTORY_AVAILABLE = True
except ImportError:
    FORECAST_HISTORY_AVAILABLE = False
    def log_forecast(**kwargs): pass

# =============================================================================
# Configuration (config.json > env vars > defaults)
# =============================================================================

from simmer_sdk.skill import load_config, update_config, get_config_path

# Canonical scan list — single source of truth. Kept here (early in module load)
# so CONFIG_SCHEMA below can reference it as the default for `locations`.
# Imported by format_scan.py and dashboard.py so every consumer stays in sync.
DEFAULT_LOCATIONS = (
    "NYC,Chicago,Seattle,Atlanta,Dallas,Miami,Houston,San Francisco,"
    "Phoenix,Los Angeles,Denver,Austin,Las Vegas,"
    "Tel Aviv,Munich,London,Tokyo,Seoul,Ankara,Lucknow,"
    "Wellington,Toronto,Paris,Milan,Sao Paulo,Warsaw,Singapore,"
    "Shanghai,Beijing,Shenzhen,Chengdu,Chongqing,Wuhan,Hong Kong,"
    "Buenos Aires"
)

# Configuration schema
# Note: env var names match autotune registry. Legacy aliases (SIMMER_WEATHER_ENTRY,
# SIMMER_WEATHER_EXIT, SIMMER_WEATHER_MAX_POSITION, SIMMER_WEATHER_MAX_TRADES) are
# resolved as fallbacks below for backwards compatibility.
CONFIG_SCHEMA = {
    "entry_threshold":   {"env": "SIMMER_WEATHER_ENTRY_THRESHOLD",   "default": 0.50,  "type": float,
                          "help": "Upper price ceiling for entry (sanity cap). Primary gate is min_edge."},
    "min_edge":          {"env": "SIMMER_WEATHER_MIN_EDGE",          "default": 0.25,  "type": float,
                          "help": "Min required edge = confidence - price. Primary entry gate (replaces pure price threshold)."},
    "exit_threshold":    {"env": "SIMMER_WEATHER_EXIT_THRESHOLD",    "default": 0.45,  "type": float},
    "max_position_usd":  {"env": "SIMMER_WEATHER_MAX_POSITION_USD",  "default": 200.00, "type": float},
    "sizing_pct":        {"env": "SIMMER_WEATHER_SIZING_PCT",        "default": 0.05,  "type": float},
    "max_trades_per_run":{"env": "SIMMER_WEATHER_MAX_TRADES_PER_RUN","default": 10,    "type": int},
    "paper_balance":     {"env": "SIMMER_WEATHER_PAPER_BALANCE",     "default": 10000.0,"type": float},
    "locations":         {"env": "SIMMER_WEATHER_LOCATIONS",         "default": DEFAULT_LOCATIONS, "type": str},
    "binary_only":       {"env": "SIMMER_WEATHER_BINARY_ONLY",       "default": False, "type": bool},
    "slippage_max":      {"env": "SIMMER_WEATHER_SLIPPAGE_MAX",      "default": 0.15,  "type": float},
    "min_liquidity":     {"env": "SIMMER_WEATHER_MIN_LIQUIDITY",     "default": 0.0,   "type": float},
    "order_type":        {"env": "SIMMER_WEATHER_ORDER_TYPE",        "default": "GTC", "type": str,
                          "help": "Order type: GTC (default, limit order that waits for fill) or FAK (cancel if not filled immediately). GTC recommended for illiquid weather markets."},
    "vol_targeting":     {"env": "SIMMER_WEATHER_VOL_TARGETING",     "default": False, "type": bool,
                          "help": "Enable volatility targeting: scale position sizes by target_vol / realized_vol."},
    "target_vol":        {"env": "SIMMER_WEATHER_TARGET_VOL",        "default": 0.20,  "type": float,
                          "help": "Target annualized volatility (0.20 = 20%). Used when vol_targeting is enabled."},
    "vol_max_leverage":  {"env": "SIMMER_WEATHER_VOL_MAX_LEVERAGE",  "default": 2.0,   "type": float,
                          "help": "Max leverage multiplier from vol targeting (caps scale-up in calm markets)."},
    "vol_min_allocation":{"env": "SIMMER_WEATHER_VOL_MIN_ALLOC",     "default": 0.2,   "type": float,
                          "help": "Min allocation floor from vol targeting (stay in market during high vol)."},
    "vol_span":          {"env": "SIMMER_WEATHER_VOL_SPAN",          "default": 10,    "type": int,
                          "help": "EWMA span for volatility calculation (lower = more responsive)."},
    "max_daily_loss_usd":{"env": "SIMMER_WEATHER_MAX_DAILY_LOSS_USD","default": 0.0,   "type": float,
                          "help": "Stop trading for the day once realized+unrealized loss exceeds this USD. 0 = disabled."},
    "exit_profit_multiplier": {"env": "SIMMER_WEATHER_EXIT_PROFIT_MULT", "default": 4.0, "type": float,
                          "help": "Dynamic exit: take profit at max(exit_threshold, entry_price * mult). 0 = use fixed exit_threshold only."},
    "ladder_first_exit": {"env": "SIMMER_WEATHER_LADDER_FIRST_EXIT", "default": 0.0,   "type": float,
                          "help": "Price threshold for first laddered exit (sells ladder_first_fraction). 0 = disabled."},
    "ladder_first_fraction": {"env": "SIMMER_WEATHER_LADDER_FIRST_FRAC", "default": 0.5, "type": float,
                          "help": "Fraction of position to sell at ladder_first_exit (0.5 = sell half)."},
    "discovery_cache_minutes": {"env": "SIMMER_WEATHER_DISCOVERY_CACHE_MIN", "default": 60, "type": int,
                          "help": "How long to cache per-location discovery results before rescanning (minutes)."},
    "log_level":         {"env": "SIMMER_WEATHER_LOG_LEVEL",         "default": "INFO","type": str,
                          "help": "Logging verbosity: DEBUG, INFO, WARNING, ERROR."},
    "forecast_cache_disk":{"env": "SIMMER_WEATHER_FORECAST_CACHE_DISK", "default": True, "type": bool,
                          "help": "Persist forecast cache to disk across runs (reduces API calls on cron schedule)."},
    "concurrent_scans":  {"env": "SIMMER_WEATHER_CONCURRENT_SCANS",  "default": True,  "type": bool,
                          "help": "Fetch market context + price history concurrently across events."},
    # Punt mode: separate side-strategy for tail-priced buckets (0.1¢-6¢ mispricings).
    # Runs AFTER core trades on the same forecast data. Its own budget + journal tag.
    # Does not affect core trade selection, sizing, exits, or budget.
    "punt_mode":         {"env": "SIMMER_WEATHER_PUNT_MODE",         "default": True,  "type": bool,
                          "help": "Enable punt mode: buy deeply-mispriced tail buckets with small stakes."},
    "punt_max_position_usd": {"env": "SIMMER_WEATHER_PUNT_POSITION_USD", "default": 15.0, "type": float,
                          "help": "Fixed USD per punt trade (small — these are lottery tickets)."},
    "punt_price_ceiling":{"env": "SIMMER_WEATHER_PUNT_PRICE_CEILING","default": 0.06,  "type": float,
                          "help": "Max price for a punt candidate (6¢ default — above this it's not a tail mispricing)."},
    "punt_min_edge":     {"env": "SIMMER_WEATHER_PUNT_MIN_EDGE",     "default": 0.50,  "type": float,
                          "help": "Min edge (model_prob - price) for a punt candidate. Higher than core min_edge."},
    "punt_min_confidence":{"env": "SIMMER_WEATHER_PUNT_MIN_CONFIDENCE","default": 0.70,"type": float,
                          "help": "Min model probability for a punt. Don't punt on weak tail signals."},
    "punt_daily_budget_usd":{"env": "SIMMER_WEATHER_PUNT_DAILY_BUDGET","default": 100.0,"type": float,
                          "help": "Max USD spent on punts per day. Safety cap."},
    # Late mode: day-of intraday strategy. At ~3pm local, buys the bucket
    # containing the observed running daily max/min from TWC. Orthogonal
    # to CORE (model forecast) and PUNT (tail lottery). Runs separately
    # via scripts/late_trader.py on an hourly cron.
    "late_mode":         {"env": "SIMMER_WEATHER_LATE_MODE",         "default": True,  "type": bool,
                          "help": "Enable LATE mode: day-of intraday entry based on TWC observations."},
    "late_price_ceiling":{"env": "SIMMER_WEATHER_LATE_PRICE_CEILING","default": 0.90,  "type": float,
                          "help": "Max entry price for a LATE candidate. Breakeven on good cities is ~0.91 after Simmer fee."},
    "late_max_position_usd":{"env": "SIMMER_WEATHER_LATE_POSITION_USD","default": 100.0,"type": float,
                          "help": "Max USD per LATE trade."},
    "late_daily_budget_usd":{"env": "SIMMER_WEATHER_LATE_DAILY_BUDGET","default": 500.0,"type": float,
                          "help": "Max USD spent across all LATE trades in a UTC day."},
    "late_entry_hour":   {"env": "SIMMER_WEATHER_LATE_ENTRY_HOUR",   "default": 15,    "type": int,
                          "help": "Local hour at which LATE mode takes its snapshot (15 = 3pm local)."},
    "late_edge_buffer_c":{"env": "SIMMER_WEATHER_LATE_EDGE_BUFFER_C","default": 0.3,   "type": float,
                          "help": "Min distance (°C) from running temp to bucket edges to count as locked in."},
    "late_cities":       {"env": "SIMMER_WEATHER_LATE_CITIES",
                          "default": "London,Toronto,Singapore,Sao Paulo,Shanghai,Tokyo,Beijing,Los Angeles,Miami,Seattle,Chicago,Dallas",
                          "type": str,
                          "help": "Comma-separated whitelist of cities eligible for LATE mode (>=70% hit rate in DST-corrected Jan-Apr 2026 backtest)."},
}

# Backwards-compatible env var aliases (old name -> new name)
_LEGACY_ENV_ALIASES = {
    "SIMMER_WEATHER_ENTRY":        "SIMMER_WEATHER_ENTRY_THRESHOLD",
    "SIMMER_WEATHER_EXIT":         "SIMMER_WEATHER_EXIT_THRESHOLD",
    "SIMMER_WEATHER_MAX_POSITION": "SIMMER_WEATHER_MAX_POSITION_USD",
    "SIMMER_WEATHER_MAX_TRADES":   "SIMMER_WEATHER_MAX_TRADES_PER_RUN",
}
for _old, _new in _LEGACY_ENV_ALIASES.items():
    if _old in os.environ and _new not in os.environ:
        os.environ[_new] = os.environ[_old]

# Load configuration
_config = load_config(CONFIG_SCHEMA, __file__, slug="polymarket-weather-trader")

ORDER_TYPE = (_config.get("order_type") or "GTC").upper()

# SimmerClient singleton
_client = None

def get_client(live=True):
    """Lazy-init SimmerClient singleton."""
    global _client
    if _client is not None:
        return _client
    try:
        from simmer_sdk import SimmerClient
    except ImportError:
        print("Error: simmer-sdk not installed. Run: pip install simmer-sdk")
        sys.exit(1)
    api_key = os.environ.get("SIMMER_API_KEY")
    if not api_key:
        print("Error: SIMMER_API_KEY environment variable not set")
        print("Get your API key from: simmer.markets/dashboard -> SDK tab")
        sys.exit(1)
    venue = os.environ.get("TRADING_VENUE", "polymarket")
    _client = SimmerClient(api_key=api_key, venue=venue, live=live)
    return _client

# =============================================================================
# Simmer request throttle + 429 backoff
# =============================================================================
# Every call to the Simmer API goes through `simmer_call()` which enforces:
#   - A minimum interval between requests across the whole process
#   - Automatic retry on 429 with jittered exponential backoff
#   - Short-term result cache for read endpoints (context, price history)
#   - Circuit breaker: pause all calls for a cooldown if 429s pile up
# This is the single lever for tuning Simmer request pressure.

import threading
import random

SIMMER_MIN_INTERVAL_SEC = 0.35   # ~3 req/sec ceiling across all threads
SIMMER_MAX_RETRIES = 4
SIMMER_BACKOFF_BASE = 1.0         # 1, 2, 4, 8 seconds (plus jitter)
SIMMER_BREAKER_429_WINDOW = 60    # seconds
SIMMER_BREAKER_THRESHOLD = 5       # N 429s in window → pause
SIMMER_BREAKER_COOLDOWN = 45      # pause duration (s)

_throttle_lock = threading.Lock()
_last_request_ts = 0.0
_recent_429_times = []
_breaker_until = 0.0


def _is_429(exc: Exception) -> bool:
    s = str(exc).lower()
    return "429" in s or "rate limit" in s or "too many requests" in s


def _parse_retry_after(exc: Exception) -> float | None:
    """Extract Retry-After seconds from the exception message if present."""
    s = str(exc)
    m = re.search(r"retry[- ]after[:\s]+(\d+(?:\.\d+)?)", s, re.IGNORECASE)
    if m:
        try:
            return min(float(m.group(1)), 60.0)
        except ValueError:
            return None
    return None


def simmer_call(fn, *args, _label: str = None, **kwargs):
    """Rate-limit + retry wrapper for any Simmer SDK call.

    Args:
        fn: The callable (e.g. client.trade, client.get_positions)
        _label: Optional tag used for structured logs
    """
    global _last_request_ts, _breaker_until

    # Circuit breaker check
    now = time.time()
    if now < _breaker_until:
        wait = _breaker_until - now
        logger.warning(f"Simmer circuit breaker open — sleeping {wait:.1f}s")
        time.sleep(wait)

    for attempt in range(1, SIMMER_MAX_RETRIES + 1):
        with _throttle_lock:
            elapsed = time.time() - _last_request_ts
            if elapsed < SIMMER_MIN_INTERVAL_SEC:
                time.sleep(SIMMER_MIN_INTERVAL_SEC - elapsed)
            _last_request_ts = time.time()

        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not _is_429(exc):
                log_error("api_error", str(exc), label=_label, fn=fn.__name__ if hasattr(fn, "__name__") else None)
                raise
            # Record the 429 and check the breaker
            with _throttle_lock:
                now = time.time()
                _recent_429_times[:] = [t for t in _recent_429_times if now - t < SIMMER_BREAKER_429_WINDOW]
                _recent_429_times.append(now)
                if len(_recent_429_times) >= SIMMER_BREAKER_THRESHOLD:
                    _breaker_until = now + SIMMER_BREAKER_COOLDOWN
                    _recent_429_times.clear()
                    logger.warning(f"Simmer 429 flood — breaker open for {SIMMER_BREAKER_COOLDOWN}s")

            if attempt >= SIMMER_MAX_RETRIES:
                logger.error(f"Simmer 429 exhausted retries ({_label or fn.__name__})")
                raise

            # Honor Retry-After if the SDK surfaces it, else exponential + jitter
            retry_after = _parse_retry_after(exc)
            if retry_after is None:
                retry_after = SIMMER_BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.info(f"Simmer 429 ({_label or fn.__name__}) — sleeping {retry_after:.1f}s then retry {attempt+1}/{SIMMER_MAX_RETRIES}")
            time.sleep(retry_after)


# Source tag for tracking
TRADE_SOURCE = "sdk:weather"
SKILL_SLUG = "polymarket-weather-trader"
_automaton_reported = False

# Polymarket constraints
MIN_SHARES_PER_ORDER = 5.0  # Polymarket requires minimum 5 shares
MIN_TICK_SIZE = 0.01        # Minimum tradeable price

# Strategy parameters - from config
ENTRY_THRESHOLD = _config["entry_threshold"]
MIN_EDGE = _config["min_edge"]
EXIT_THRESHOLD = _config["exit_threshold"]
MAX_POSITION_USD = _config["max_position_usd"]
_automaton_max = os.environ.get("AUTOMATON_MAX_BET")
if _automaton_max:
    MAX_POSITION_USD = min(MAX_POSITION_USD, float(_automaton_max))

# Smart sizing parameters
SMART_SIZING_PCT = _config["sizing_pct"]

# Rate limiting
MAX_TRADES_PER_RUN = _config["max_trades_per_run"]

# Market type filter
BINARY_ONLY = _config["binary_only"]

# Volatility targeting parameters
VOL_TARGETING = _config["vol_targeting"]
TARGET_VOL = _config["target_vol"]
VOL_MAX_LEVERAGE = _config["vol_max_leverage"]
VOL_MIN_ALLOCATION = _config["vol_min_allocation"]
VOL_SPAN = _config["vol_span"]

# Context safeguard thresholds
SLIPPAGE_MAX_PCT = _config["slippage_max"]  # Skip if slippage exceeds this (tunable)
MIN_LIQUIDITY_USD = _config["min_liquidity"]  # Skip markets with liquidity below this (0 = disabled)
TIME_TO_RESOLUTION_MIN_HOURS = 2  # Skip if resolving in < 2 hours

# Price trend detection
PRICE_DROP_THRESHOLD = 0.10  # 10% drop in last 24h = stronger signal

# Risk and execution improvements
MAX_DAILY_LOSS_USD = _config["max_daily_loss_usd"]  # 0 = disabled
EXIT_PROFIT_MULTIPLIER = _config["exit_profit_multiplier"]  # 0 = fixed exit only
LADDER_FIRST_EXIT = _config["ladder_first_exit"]  # 0 = disabled
LADDER_FIRST_FRACTION = _config["ladder_first_fraction"]
DISCOVERY_CACHE_MINUTES = _config["discovery_cache_minutes"]
FORECAST_CACHE_DISK = _config["forecast_cache_disk"]
CONCURRENT_SCANS = _config["concurrent_scans"]
LOG_LEVEL = _config["log_level"]

# Punt mode (side strategy — isolated from core trading)
PUNT_MODE = _config["punt_mode"]
PUNT_MAX_POSITION_USD = _config["punt_max_position_usd"]
PUNT_PRICE_CEILING = _config["punt_price_ceiling"]
PUNT_MIN_EDGE = _config["punt_min_edge"]
PUNT_MIN_CONFIDENCE = _config["punt_min_confidence"]
PUNT_DAILY_BUDGET_USD = _config["punt_daily_budget_usd"]

# Supported locations (matching Polymarket resolution sources)
LOCATIONS = {
    "NYC": {"lat": 40.7769, "lon": -73.8740, "name": "New York City (LaGuardia)", "station": "KLGA"},
    "Chicago": {"lat": 41.9742, "lon": -87.9073, "name": "Chicago (O'Hare)", "station": "KORD"},
    "Seattle": {"lat": 47.4502, "lon": -122.3088, "name": "Seattle (Sea-Tac)", "station": "KSEA"},
    "Atlanta": {"lat": 33.6407, "lon": -84.4277, "name": "Atlanta (Hartsfield)", "station": "KATL"},
    "Dallas": {"lat": 32.8998, "lon": -97.0403, "name": "Dallas (DFW)", "station": "KDFW"},
    "Miami": {"lat": 25.7959, "lon": -80.2870, "name": "Miami (MIA)", "station": "KMIA"},
}

# Active locations - from config
_locations_str = _config["locations"]
ACTIVE_LOCATIONS = [loc.strip().upper() for loc in _locations_str.split(",") if loc.strip()]

# Empirical city difficulty tiers derived from resolved weather trades across
# Hans323 (+$80k, n=2,684) and ColdMath (-$173k, n=6,386). EASY cities have
# ≥75% pro win rate; HARD cities ≤55%. These drive risk-based position sizing:
#   EASY   → 3% of paper balance per trade
#   MEDIUM → 2%
#   HARD   → 1%
# Keys are uppercase to match ACTIVE_LOCATIONS. Unknown cities default to MEDIUM.
# Last refresh: 2026-04-24 (see reports/top_traders_2026-04-24.txt).
CITY_DIFFICULTY = {
    # EASY — ≥75% pro win rate (stable climates, predictable)
    "TEL AVIV":      "easy",   # n=46,  93.5%
    "WARSAW":        "easy",   # n=70,  90.0%
    "SAN FRANCISCO": "easy",   # n=53,  86.8%
    "LOS ANGELES":   "easy",   # n=66,  86.4%
    "MILAN":         "easy",   # n=78,  85.9%
    "CHENGDU":       "easy",   # n=37,  83.8%
    "HOUSTON":       "easy",   # n=60,  76.7%
    "MUNICH":        "easy",   # n=89,  77.5%
    # HARD — ≤55% pro win rate (volatile, hard to forecast accurately)
    "TOKYO":         "hard",   # n=96,  54.2%
    "SHANGHAI":      "hard",   # n=50,  52.0%
    "BEIJING":       "hard",   # n=40,  60.0% — borderline; held HARD pending more data (same Asian basin as Tokyo/Shanghai)
    "WUHAN":         "hard",   # n=35,  57.1% — borderline; held HARD pending more data
    # Demoted from EASY/HARD on 2026-04-24 after expanded sample:
    #   SEOUL:      Hans n=30@86.7% diluted by ColdMath n=196@62.8% → combined 226@65.9% (MEDIUM)
    #   WELLINGTON: Hans n=11@100% noise; ColdMath n=358@56.7% drives combined 369@58.0% (MEDIUM, just above HARD threshold)
    # Everything else defaults to "medium" (55-75% win rate)
}
RISK_PCT_BY_TIER = {"easy": 0.03, "medium": 0.02, "hard": 0.01}


def city_tier(location: str) -> str:
    """Return difficulty tier for a city. Defaults to 'medium' if unknown."""
    return CITY_DIFFICULTY.get((location or "").upper(), "medium")


def city_risk_pct(location: str) -> float:
    """Return per-trade risk % (0.01/0.02/0.03) for a given city."""
    return RISK_PCT_BY_TIER[city_tier(location)]

# =============================================================================
# Structured logging
# =============================================================================

logger = logging.getLogger("weather_trader")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))


_SKIP_LOG = _p.Path(__file__).parent / "data" / "skip_events.jsonl"


def _log_skip(reason: str, location: str, date_str: str, metric: str,
              market_id: str | None = None, price: float | None = None,
              confidence: float | None = None, edge: float | None = None,
              spread: float | None = None, signal_strength: str | None = None,
              threshold: float | None = None, actual: float | None = None) -> None:
    """Log a skipped trade candidate to skip_events.jsonl for funnel analysis."""
    import json as _json
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "location": location,
        "date": date_str,
        "metric": metric,
        "market_id": market_id,
        "price": price,
        "confidence": confidence,
        "edge": edge,
        "spread": spread,
        "signal_strength": signal_strength,
        "threshold": threshold,
        "actual": actual,
    }
    try:
        with _SKIP_LOG.open("a") as f:
            f.write(_json.dumps(entry, default=str) + "\n")
    except OSError:
        pass


def validate_live_trading_prereqs():
    """Fail fast if --live is used without required credentials."""
    missing = []
    if not os.environ.get("SIMMER_API_KEY"):
        missing.append("SIMMER_API_KEY")
    if not os.environ.get("WALLET_PRIVATE_KEY"):
        missing.append("WALLET_PRIVATE_KEY")
    if missing:
        print(f"❌ Cannot start live trading: missing env vars {', '.join(missing)}")
        print(f"   Set WALLET_PRIVATE_KEY=0x... to sign orders client-side.")
        sys.exit(1)


# =============================================================================
# Disk-persisted forecast cache
# =============================================================================

_FORECAST_CACHE_FILE = _p.Path(__file__).parent / "data" / "forecast_cache.json"
# Split TTL: D+0 markets are fast-moving (refresh every ~1h so we pick up new
# GFS/ICON runs and METAR updates). D+1+ markets are slower-moving (3h is fine).
_FORECAST_CACHE_TTL_D0_SECONDS = 1 * 3600
_FORECAST_CACHE_TTL_DEFAULT_SECONDS = 3 * 3600


def _forecast_cache_ttl_for(key_str: str) -> int:
    """Return TTL based on whether the cached entry targets today (D+0)."""
    # key is "location|date_str|metric" — compare date to today UTC.
    try:
        parts = key_str.split("|")
        if len(parts) >= 2:
            today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if parts[1] == today_utc:
                return _FORECAST_CACHE_TTL_D0_SECONDS
    except Exception:
        pass
    return _FORECAST_CACHE_TTL_DEFAULT_SECONDS


def _load_forecast_disk_cache() -> dict:
    """Load persisted forecasts, filtering out expired entries."""
    if not FORECAST_CACHE_DISK or not _FORECAST_CACHE_FILE.exists():
        return {}
    try:
        raw = json.loads(_FORECAST_CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    now = time.time()
    fresh = {}
    for key_str, entry in raw.items():
        ttl = _forecast_cache_ttl_for(key_str)
        if now - entry.get("cached_at", 0) < ttl:
            fresh[key_str] = entry.get("result")
    return fresh


def _save_forecast_disk_cache(cache: dict):
    """Persist forecasts with timestamps."""
    if not FORECAST_CACHE_DISK:
        return
    try:
        _FORECAST_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        serialized = {k: {"cached_at": now, "result": v} for k, v in cache.items()}
        _FORECAST_CACHE_FILE.write_text(json.dumps(serialized, default=str))
    except OSError:
        pass


def _cache_key_to_str(key: tuple) -> str:
    return "|".join(str(x) for x in key)


# =============================================================================
# Daily loss tracking
# =============================================================================

def get_realized_daily_pnl() -> float:
    """Sum P&L of trades resolved today (UTC). Uses paper journal if available."""
    if not PAPER_JOURNAL_AVAILABLE:
        return 0.0
    try:
        from paper_journal import _load_trades
        trades = _load_trades()
    except Exception:
        return 0.0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0.0
    for t in trades:
        resolved_at = t.get("resolved_at") or ""
        if resolved_at.startswith(today) and t.get("pnl") is not None:
            total += float(t["pnl"])
    return total


def daily_loss_limit_breached() -> bool:
    """Return True if max_daily_loss_usd is configured and today's loss exceeds it."""
    if MAX_DAILY_LOSS_USD <= 0:
        return False
    realized = get_realized_daily_pnl()
    return realized <= -MAX_DAILY_LOSS_USD

# =============================================================================
# NOAA Weather API
# =============================================================================

# International city coordinates for Open-Meteo fallback
# Keyed by the city name as it appears in market questions
INTERNATIONAL_LOCATIONS = {
    "Tel Aviv":   {"lat": 32.0853, "lon": 34.7818, "tz": "Asia/Jerusalem"},
    "Munich":     {"lat": 48.1351, "lon": 11.5820, "tz": "Europe/Berlin"},
    "London":     {"lat": 51.5074, "lon": -0.1278, "tz": "Europe/London"},
    "Tokyo":      {"lat": 35.6762, "lon": 139.6503, "tz": "Asia/Tokyo"},
    "Seoul":      {"lat": 37.5665, "lon": 126.9780, "tz": "Asia/Seoul"},
    "Ankara":     {"lat": 39.9334, "lon": 32.8597,  "tz": "Europe/Istanbul"},
    "Lucknow":    {"lat": 26.8467, "lon": 80.9462,  "tz": "Asia/Kolkata"},
    "Wellington": {"lat": -41.2866, "lon": 174.7756, "tz": "Pacific/Auckland"},
    "Toronto":    {"lat": 43.6532, "lon": -79.3832, "tz": "America/Toronto"},
    "Paris":      {"lat": 48.8566, "lon": 2.3522,   "tz": "Europe/Paris"},
    "Milan":      {"lat": 45.4642, "lon": 9.1900,   "tz": "Europe/Rome"},
    "Sao Paulo":  {"lat": -23.5505, "lon": -46.6333, "tz": "America/Sao_Paulo"},
    "Warsaw":     {"lat": 52.2297, "lon": 21.0122,  "tz": "Europe/Warsaw"},
    "Singapore":  {"lat": 1.3521,  "lon": 103.8198, "tz": "Asia/Singapore"},
    "Shanghai":   {"lat": 31.2304, "lon": 121.4737, "tz": "Asia/Shanghai"},
    "Hong Kong":  {"lat": 22.3193, "lon": 114.1694, "tz": "Asia/Hong_Kong"},
    "Buenos Aires": {"lat": -34.6037, "lon": -58.3816, "tz": "America/Argentina/Buenos_Aires"},
}

# Per-location bias correction in °C. NWP gridded models and Open-Meteo archive data
# diverge from official weather station readings (Polymarket's resolution source).
# Values derived from resolved trade analysis; update as more data accumulates.
LOCATION_BIAS_C = {
    "Hong Kong": 0.8,   # HKO station consistently reads ~+0.8°C above gridded model output
    "Shenzhen":  1.0,   # Models ran cold by ~1°C across all resolved Shenzhen trades
}

# Forecasts are fetched exclusively via ensemble_forecast.get_ensemble_forecast.
# Legacy get_noaa_forecast / get_openmeteo_forecast / fetch_json helpers were
# removed — they were never called anywhere.


# =============================================================================
# Market Parsing
# =============================================================================

def parse_weather_event(event_name: str) -> dict:
    """Parse weather event name to extract location, date, metric."""
    if not event_name:
        return None

    event_lower = event_name.lower()

    if 'highest' in event_lower or 'high temp' in event_lower:
        metric = 'high'
    elif 'lowest' in event_lower or 'low temp' in event_lower:
        return None  # Skip lowest-temp events — 0% conversion rate on Polymarket
    else:
        metric = 'high'

    location = None
    # Multi-word aliases must come before overlapping single-word ones
    # (e.g. "new york" before "york", "hong kong" before "hong").
    location_aliases = {
        # US cities
        'new york': 'NYC', 'nyc': 'NYC', 'laguardia': 'NYC', 'la guardia': 'NYC',
        'chicago': 'Chicago', "o'hare": 'Chicago', 'ohare': 'Chicago',
        'seattle': 'Seattle', 'sea-tac': 'Seattle',
        'atlanta': 'Atlanta', 'hartsfield': 'Atlanta',
        'dallas': 'Dallas', 'dfw': 'Dallas',
        'miami': 'Miami',
        'houston': 'Houston',
        'san francisco': 'San Francisco',
        'phoenix': 'Phoenix',
        'los angeles': 'Los Angeles',
        'denver': 'Denver',
        'austin': 'Austin',
        'las vegas': 'Las Vegas',
        # International cities
        'hong kong': 'Hong Kong',
        'tel aviv': 'Tel Aviv',
        'sao paulo': 'Sao Paulo', 'são paulo': 'Sao Paulo',
        'shanghai': 'Shanghai',
        'beijing': 'Beijing',
        'shenzhen': 'Shenzhen',
        'chengdu': 'Chengdu',
        'chongqing': 'Chongqing',
        'wuhan': 'Wuhan',
        'munich': 'Munich',
        'london': 'London',
        'tokyo': 'Tokyo',
        'seoul': 'Seoul',
        'ankara': 'Ankara',
        'lucknow': 'Lucknow',
        'wellington': 'Wellington',
        'toronto': 'Toronto',
        'paris': 'Paris',
        'milan': 'Milan',
        'warsaw': 'Warsaw',
        'singapore': 'Singapore',
        'buenos aires': 'Buenos Aires',
    }

    for alias, loc in location_aliases.items():
        if alias in event_lower:
            location = loc
            break

    if not location:
        return None

    # Detect temperature unit from event name
    temp_unit = 'C' if '°c' in event_lower or re.search(r'\d+°?c\b', event_lower, re.IGNORECASE) else 'F'

    month_day_match = re.search(r'on\s+([a-zA-Z]+)\s+(\d{1,2})', event_name, re.IGNORECASE)
    if not month_day_match:
        return None

    month_name = month_day_match.group(1).lower()
    day = int(month_day_match.group(2))

    month_map = {
        'january': 1, 'jan': 1, 'february': 2, 'feb': 2, 'march': 3, 'mar': 3,
        'april': 4, 'apr': 4, 'may': 5, 'june': 6, 'jun': 6, 'july': 7, 'jul': 7,
        'august': 8, 'aug': 8, 'september': 9, 'sep': 9, 'october': 10, 'oct': 10,
        'november': 11, 'nov': 11, 'december': 12, 'dec': 12,
    }

    month = month_map.get(month_name)
    if not month:
        return None

    now = datetime.now(timezone.utc)
    year = now.year
    try:
        target_date = datetime(year, month, day, tzinfo=timezone.utc)
        if target_date < now - timedelta(days=7):
            year += 1
        date_str = f"{year}-{month:02d}-{day:02d}"
    except ValueError:
        return None

    return {"location": location, "date": date_str, "metric": metric, "unit": temp_unit}


def parse_temperature_bucket(outcome_name: str):
    """Parse temperature bucket from outcome name. Works for both °F and °C markets,
    including single-degree exact buckets (e.g. '22°C') and ranges (e.g. '54-55°F').
    Returns (min, max, unit) where unit is 'C' or 'F'. None if unparseable."""
    if not outcome_name:
        return None

    # Detect unit from explicit °C marker (°F defaults if absent)
    bucket_unit = 'C' if re.search(r'°C', outcome_name, re.IGNORECASE) else 'F'

    below_match = re.search(r'(-?\d+)\s*°?[fFcC]?\s*(or below|or less)', outcome_name, re.IGNORECASE)
    if below_match:
        return (-999, int(below_match.group(1)), bucket_unit)

    above_match = re.search(r'(-?\d+)\s*°?[fFcC]?\s*(or higher|or above|or more)', outcome_name, re.IGNORECASE)
    if above_match:
        return (int(above_match.group(1)), 999, bucket_unit)

    range_match = re.search(r'(-?\d+)\s*(?:°?\s*[fFcC])?\s*(?:-|–|to)\s*(-?\d+)', outcome_name)
    if range_match:
        low, high = int(range_match.group(1)), int(range_match.group(2))
        return (min(low, high), max(low, high), bucket_unit)

    # Single exact-degree bucket: "be 22°C on" or "-5°F"
    exact_match = re.search(r'(-?\d+)\s*°[fFcC]', outcome_name)
    if exact_match:
        t = int(exact_match.group(1))
        return (t, t, bucket_unit)

    # Bare integer in short outcome names like "22°C" or "-5"
    bare_match = re.match(r'^\s*(-?\d+)\s*°?[cCfF]?\s*$', outcome_name.strip())
    if bare_match:
        t = int(bare_match.group(1))
        return (t, t, bucket_unit)

    return None


def parse_market_bucket(market: dict):
    """
    Extract a parseable bucket from a Simmer market dict.

    Some markets return outcome_name="Yes"/"No" instead of the bucket label.
    In that case the bucket info lives in the question text, e.g. "Will the
    highest temperature in Hong Kong be 28°C on April 21, 2026?". We try the
    most-specific fields first and fall back to the question.

    Returns:
        ((lo, hi, unit), bucket_label) on success — bucket_label is a
        human-readable string for logging/journal storage.
        (None, "") if nothing parses.
    """
    if not isinstance(market, dict):
        return None, ""
    # For weather markets, question text always has the correct threshold
    # (outcome_name can contain stale or inconsistent values like "17°C" when
    # the actual question is "43°C"). Try question first for weather; for all
    # other markets fall back to the original priority order.
    question = market.get("question", "")
    bucket_from_question = parse_temperature_bucket(question)
    if bucket_from_question:
        return bucket_from_question, question

    # Fallback order for non-weather markets
    candidates = [
        market.get("outcome_name"),
        market.get("outcome"),
        market.get("name"),
    ]
    for raw in candidates:
        if not raw or not isinstance(raw, str):
            continue
        bucket = parse_temperature_bucket(raw)
        if bucket:
            return bucket, raw
    return None, ""


# =============================================================================
# Simmer API - Core
# =============================================================================

# =============================================================================
# Simmer API - Portfolio & Context
# =============================================================================

# Short-TTL in-memory caches — market context and price history don't change
# fast enough to warrant hitting the API multiple times per run.
_CONTEXT_CACHE_TTL = 60.0   # seconds
_HISTORY_CACHE_TTL = 120.0
_context_cache = {}   # market_id -> (expiry_ts, data)
_history_cache = {}   # market_id -> (expiry_ts, data)
_portfolio_cache = {"expiry": 0.0, "data": None}
_PORTFOLIO_TTL = 30.0


def get_portfolio() -> dict:
    """Get portfolio summary from SDK (30s cache)."""
    now = time.time()
    if _portfolio_cache["data"] is not None and now < _portfolio_cache["expiry"]:
        return _portfolio_cache["data"]
    try:
        data = simmer_call(get_client().get_portfolio, _label="portfolio")
        _portfolio_cache["data"] = data
        _portfolio_cache["expiry"] = now + _PORTFOLIO_TTL
        return data
    except Exception as e:
        print(f"  ⚠️  Portfolio fetch failed: {e}")
        return None


def get_market_context(market_id: str, my_probability: float = None) -> dict:
    """Get market context with safeguards. 60s TTL cache keyed by (market_id, probability)."""
    cache_key = (market_id, round(my_probability, 2) if my_probability is not None else None)
    now = time.time()
    hit = _context_cache.get(cache_key)
    if hit and now < hit[0]:
        return hit[1]
    try:
        if my_probability is not None:
            data = simmer_call(
                get_client()._request, "GET", f"/api/sdk/context/{market_id}",
                params={"my_probability": my_probability}, _label="context",
            )
        else:
            data = simmer_call(get_client().get_market_context, market_id, _label="context")
        _context_cache[cache_key] = (now + _CONTEXT_CACHE_TTL, data)
        return data
    except Exception as e:
        log_error("context_fetch", str(e), market_id=market_id)
        return None


def get_price_history(market_id: str) -> list:
    """Get price history for trend detection. 120s TTL cache."""
    now = time.time()
    hit = _history_cache.get(market_id)
    if hit and now < hit[0]:
        return hit[1]
    try:
        data = simmer_call(get_client().get_price_history, market_id, _label="price_history")
        _history_cache[market_id] = (now + _HISTORY_CACHE_TTL, data)
        return data
    except Exception as e:
        log_error("price_history", str(e), market_id=market_id)
        return []


def check_context_safeguards(context: dict, use_edge: bool = True) -> tuple:
    """
    Check context for safeguards. Returns (should_trade, reasons).
    
    Args:
        context: Context response from SDK
        use_edge: If True, respect edge recommendation (TRADE/HOLD/SKIP)
    """
    if not context:
        return True, []  # No context = proceed (fail open)

    reasons = []
    market = context.get("market", {})
    warnings = context.get("warnings", [])
    discipline = context.get("discipline", {})
    slippage = context.get("slippage", {})
    edge = context.get("edge", {})

    # Check for deal-breakers in warnings
    for warning in warnings:
        if "MARKET RESOLVED" in str(warning).upper():
            return False, ["Market already resolved"]

    # Check flip-flop warning
    warning_level = discipline.get("warning_level", "none")
    if warning_level == "severe":
        return False, [f"Severe flip-flop warning: {discipline.get('flip_flop_warning', '')}"]
    elif warning_level == "mild":
        reasons.append("Mild flip-flop warning (proceed with caution)")

    # Check time to resolution
    time_str = market.get("time_to_resolution", "")
    if time_str:
        try:
            hours = 0
            if "d" in time_str:
                days = int(time_str.split("d")[0].strip())
                hours += days * 24
            if "h" in time_str:
                h_part = time_str.split("h")[0]
                if "d" in h_part:
                    h_part = h_part.split("d")[-1].strip()
                hours += int(h_part)

            if hours < TIME_TO_RESOLUTION_MIN_HOURS:
                return False, [f"Resolves in {hours}h - too soon"]
        except (ValueError, IndexError):
            pass

    # Check liquidity (pre-filter before slippage, avoids wasting a context call)
    if MIN_LIQUIDITY_USD > 0:
        liquidity = market.get("liquidity", 0) or 0
        if liquidity < MIN_LIQUIDITY_USD:
            return False, [f"Liquidity too low: ${liquidity:.0f} < ${MIN_LIQUIDITY_USD:.0f} min"]

    # Check slippage
    estimates = slippage.get("estimates", []) if slippage else []
    if estimates:
        slippage_pct = estimates[0].get("slippage_pct", 0)
        if slippage_pct > SLIPPAGE_MAX_PCT:
            return False, [f"Slippage too high: {slippage_pct:.1%} (max {SLIPPAGE_MAX_PCT:.0%})"]

    # Check edge recommendation (if available and use_edge=True)
    if use_edge and edge:
        recommendation = edge.get("recommendation")
        user_edge = edge.get("user_edge")
        threshold = edge.get("suggested_threshold", 0)
        
        if recommendation == "SKIP":
            return False, ["Edge analysis: SKIP (market resolved or invalid)"]
        elif recommendation == "HOLD":
            if user_edge is not None and threshold:
                reasons.append(f"Edge {user_edge:.1%} below threshold {threshold:.1%} - marginal opportunity")
            else:
                reasons.append("Edge analysis recommends HOLD")
        elif recommendation == "TRADE":
            reasons.append(f"Edge {user_edge:.1%} ≥ threshold {threshold:.1%} - good opportunity")

    return True, reasons


def _bucket_probability(lo_f: float, hi_f: float, mean_f: float, spread_f: float) -> float:
    """
    Approximate the probability that the daily resolution falls within [lo_f, hi_f]
    given a Gaussian forecast N(mean_f, sigma) where sigma is derived from spread.

    spread_f is the ensemble max_delta (max - min across models). For a ~4σ range,
    sigma ≈ spread / 4. We floor sigma at 1.5°F because the Gaussian approximation
    underestimates true uncertainty when models happen to cluster.

    Sentinels: lo_f == -999 means "-∞", hi_f == 999 means "+∞".
    """
    import math
    if spread_f is None or spread_f <= 0:
        sigma = 2.0
    else:
        sigma = max(spread_f / 4.0, 1.5)

    def _phi(z: float) -> float:
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

    if lo_f == -999:
        p_lo = 0.0
    else:
        p_lo = _phi((lo_f - mean_f) / sigma)
    if hi_f == 999:
        p_hi = 1.0
    else:
        p_hi = _phi((hi_f - mean_f) / sigma)
    return max(0.0, min(1.0, p_hi - p_lo))


def rank_event_buckets_by_edge(event_markets: list, forecast_f: float,
                               spread_f: float, signal_strength: str,
                               is_international: bool = False) -> list:
    """
    Rank ALL buckets in an event by edge (bucket_probability × signal_discount − price).

    Unlike the old Pass 1/Pass 2 matching that only considered the one bucket
    containing the forecast, this considers every parseable bucket. Range buckets
    spanning the forecast get more probability mass and often beat exact buckets
    on edge, which matches how pro traders (Hans323) profit — 72% of their
    volume is range buckets vs 5% exact.

    Signal strength applies a multiplicative discount:
      strong=1.00, moderate=0.92, weak=0.80, unknown=0.85, single_source=0.75

    Exact buckets (lo==hi) are widened to ±0.5° since Polymarket resolution
    rounds the daily temp — an exact "87°F" bucket effectively covers 86.5–87.5.

    Returns list of dicts sorted by edge desc, each with:
      market, bucket (lo, hi, unit), outcome_name, price, raw_prob,
      confidence (prob × discount), edge (confidence − price), lo_f, hi_f.
    """
    if spread_f is None or spread_f <= 0:
        spread_f = 4.0
    discount = {
        "strong": 1.00, "moderate": 0.92, "weak": 0.80,
        "single_source": 0.75, "unknown": 0.85,
    }.get(signal_strength, 0.85)

    ranked = []
    for market in event_markets:
        bucket, outcome_name = parse_market_bucket(market)
        if not bucket:
            continue
        lo, hi, unit = bucket

        # International locations (HK, Tokyo, etc.) use Celsius markets exclusively.
        # Reject any °F-denominated bucket to avoid matching against domestic-style
        # events (e.g. Simmer-native Fahrenheit markets) that appear in discovery.
        if is_international and unit == 'F':
            continue

        # Convert °C bucket bounds to °F (forecast is always °F)
        if unit == 'C':
            lo_f = lo * 9 / 5 + 32 if lo != -999 else -999
            hi_f = hi * 9 / 5 + 32 if hi != 999 else 999
        else:
            lo_f, hi_f = lo, hi

        # Widen exact buckets by ±0.5 of the native unit (resolution rounds daily temp).
        # °C markets resolve in Celsius, so ±0.5°C = ±0.9°F. Applying ±0.5°F to a
        # Celsius bucket underwidened by nearly half, causing boundary-zone misses.
        prob_lo, prob_hi = lo_f, hi_f
        if lo_f == hi_f and lo_f != -999 and lo_f != 999:
            widen_f = 0.9 if unit == 'C' else 0.5
            prob_lo, prob_hi = lo_f - widen_f, hi_f + widen_f

        raw_prob = _bucket_probability(prob_lo, prob_hi, forecast_f, spread_f)
        confidence = raw_prob * discount
        price = market.get("external_price_yes") or 0.5
        edge = confidence - price

        ranked.append({
            "market": market, "bucket": bucket, "outcome_name": outcome_name,
            "price": price, "raw_prob": raw_prob, "confidence": confidence,
            "edge": edge, "lo_f": lo_f, "hi_f": hi_f, "unit": unit,
        })

    ranked.sort(key=lambda x: -x["edge"])
    return ranked


def find_punt_candidates(event_markets: list, forecast_temp: float, spread: float,
                         core_match_id: str | None, already_held: set,
                         location: str, date_str: str, metric: str,
                         is_international: bool, signal_strength: str,
                         models_used: int, agreement_pct: float) -> list:
    """
    Scan all buckets in an event for tail-mispriced punt candidates.

    A punt candidate is a bucket where:
      - Market price <= PUNT_PRICE_CEILING (very cheap tail bucket)
      - Model probability >= PUNT_MIN_CONFIDENCE
      - Edge = model_prob - price >= PUNT_MIN_EDGE
      - Not the bucket the core strategy matched (avoid double-buy)
      - Not already held

    Never raises. Returns [] on any error or if punt mode is off.
    """
    if not PUNT_MODE or forecast_temp is None:
        return []
    candidates = []
    for market in event_markets:
        market_id = market.get("id")
        if market_id and (market_id == core_match_id or market_id in already_held):
            continue
        price = market.get("external_price_yes")
        if price is None or price <= 0 or price > PUNT_PRICE_CEILING:
            continue
        bucket, outcome_name = parse_market_bucket(market)
        if not bucket:
            continue
        lo, hi, unit = bucket
        if unit == 'C':
            lo = lo * 9 / 5 + 32 if lo != -999 else -999
            hi = hi * 9 / 5 + 32 if hi != 999 else 999
        prob = _bucket_probability(lo, hi, forecast_temp, spread or 0.0)
        if prob < PUNT_MIN_CONFIDENCE:
            continue
        edge = prob - price
        if edge < PUNT_MIN_EDGE:
            continue
        candidates.append({
            "location": location, "date_str": date_str, "metric": metric,
            "market": market, "market_id": market_id,
            "outcome_name": outcome_name, "price": price,
            "confidence": prob, "edge": edge,
            "signal_strength": signal_strength, "models_used": models_used,
            "agreement_pct": agreement_pct, "spread": spread,
            "forecast_temp": forecast_temp,
            "is_international": is_international,
            "strategy": "punt",
        })
    return candidates


def get_open_market_ids() -> set:
    """Return set of market_ids we already hold >0 shares in (weather source).

    Tries Simmer API first; falls back to local paper journal if that fails.
    """
    held = set()
    # Try Simmer API first
    try:
        positions = get_positions()
        for pos in positions:
            shares = (pos.get("shares_yes") or 0) + (pos.get("shares_no") or 0)
            if shares > 0:
                mid = pos.get("market_id")
                if mid:
                    held.add(mid)
    except Exception:
        pass
    # Fall back to local paper journal
    if not held:
        try:
            from paper_journal import get_open_positions
            for pos in get_open_positions():
                mid = pos.get("market_id")
                if mid:
                    held.add(mid)
        except Exception:
            pass
    return held


def compute_dynamic_exit(entry_price: float) -> float:
    """Dynamic exit threshold: max(EXIT_THRESHOLD, entry * multiplier). Caps below 0.99."""
    if EXIT_PROFIT_MULTIPLIER <= 0 or entry_price <= 0:
        return EXIT_THRESHOLD
    dynamic = min(0.99, entry_price * EXIT_PROFIT_MULTIPLIER)
    return max(EXIT_THRESHOLD, dynamic)


def detect_price_trend(history: list) -> dict:
    """
    Analyze price history for trends.
    Returns: {direction: "up"/"down"/"flat", change_24h: float, is_opportunity: bool}
    """
    if not history or len(history) < 2:
        return {"direction": "unknown", "change_24h": 0, "is_opportunity": False}

    # Get recent and older prices
    recent_price = history[-1].get("price_yes", 0.5)
    
    # Find price ~24h ago (assuming 15-min intervals, ~96 points)
    lookback = min(96, len(history) - 1)
    old_price = history[-lookback].get("price_yes", recent_price)

    if old_price == 0:
        return {"direction": "unknown", "change_24h": 0, "is_opportunity": False}

    change = (recent_price - old_price) / old_price

    if change < -PRICE_DROP_THRESHOLD:
        return {"direction": "down", "change_24h": change, "is_opportunity": True}
    elif change > PRICE_DROP_THRESHOLD:
        return {"direction": "up", "change_24h": change, "is_opportunity": False}
    else:
        return {"direction": "flat", "change_24h": change, "is_opportunity": False}


# =============================================================================
# Volatility Targeting
# =============================================================================

import math

def calculate_ewma_vol(history: list, span: int = 10) -> float | None:
    """
    Calculate annualized EWMA volatility from price history points.

    Uses log returns of YES prices with exponentially weighted moving average.
    Returns annualized volatility as a decimal (e.g. 0.25 = 25%), or None if
    insufficient data.

    Args:
        history: List of dicts with 'price_yes' key (from get_price_history)
        span: EWMA span — lower values weight recent data more heavily
    """
    prices = [p.get("price_yes") or 0 for p in history]
    # Filter out zero/near-zero prices that would break log returns
    prices = [p for p in prices if p > 0.001]
    if len(prices) < span + 5:
        return None

    # Log returns
    log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    if not log_returns:
        return None

    # EWMA variance (exponentially weighted moving average of squared deviations)
    alpha = 2.0 / (span + 1)
    ewma_var = log_returns[0] ** 2  # seed with first squared return
    for r in log_returns[1:]:
        ewma_var = alpha * (r ** 2) + (1 - alpha) * ewma_var

    ewma_std = math.sqrt(ewma_var)

    # Annualize: price history is ~15-min intervals, ~96 per day, 365 days
    # sqrt(96 * 365) ≈ 187.2
    intervals_per_day = 96
    annualized = ewma_std * math.sqrt(intervals_per_day * 365)
    return annualized


def apply_vol_targeting(base_size: float, current_vol: float | None,
                        target_vol: float = TARGET_VOL,
                        max_leverage: float = VOL_MAX_LEVERAGE,
                        min_allocation: float = VOL_MIN_ALLOCATION) -> tuple:
    """
    Apply volatility targeting multiplier to base position size.

    Returns (adjusted_size, metadata_dict).
    Falls back to base_size if vol data is unavailable.
    """
    meta = {"vol_targeting": True, "base_size": base_size, "current_vol": current_vol,
            "target_vol": target_vol}

    if current_vol is None or current_vol <= 0:
        meta["adjusted_for"] = "no_vol_data"
        meta["leverage"] = 1.0
        return base_size, meta

    raw_leverage = target_vol / current_vol
    leverage = max(min_allocation, min(raw_leverage, max_leverage))

    if leverage == min_allocation:
        meta["adjusted_for"] = "min_allocation_floor"
    elif leverage == max_leverage:
        meta["adjusted_for"] = "max_leverage_cap"
    else:
        meta["adjusted_for"] = "volatility_target"

    meta["raw_leverage"] = round(raw_leverage, 3)
    meta["leverage"] = round(leverage, 3)

    return round(base_size * leverage, 2), meta


# =============================================================================
# Market Discovery - Auto-import from Polymarket
# =============================================================================
# NOTE: Unlike fastloop (which queries Gamma API directly with tag=crypto),
# weather uses Simmer's list_importable_markets (Dome-backed keyword search).
# Gamma API has no weather/temperature tag and no public text search endpoint
# (/search requires auth). Tested Feb 2026: 600+ events paginated, zero weather.
# This path is slower but is the only way to discover weather markets by keyword.
# Trading does NOT depend on discovery — v1.10.1+ trades from already-imported
# markets via GET /api/sdk/markets?tags=weather.
# =============================================================================

# Search terms per location (matching Polymarket event naming).
# Keys MUST be uppercase to match ACTIVE_LOCATIONS entries (which are upper-cased
# in config parsing). If a location has no entry, a default "temperature {name}"
# is used.
LOCATION_SEARCH_TERMS = {
    # US cities
    "NYC":           ["temperature new york", "temperature nyc"],
    "CHICAGO":       ["temperature chicago"],
    "SEATTLE":       ["temperature seattle"],
    "ATLANTA":       ["temperature atlanta"],
    "DALLAS":        ["temperature dallas"],
    "MIAMI":         ["temperature miami"],
    "HOUSTON":       ["temperature houston"],
    "SAN FRANCISCO": ["temperature san francisco", "temperature sf"],
    "PHOENIX":       ["temperature phoenix"],
    "LOS ANGELES":   ["temperature los angeles", "temperature la"],
    "DENVER":        ["temperature denver"],
    "AUSTIN":        ["temperature austin"],
    "LAS VEGAS":     ["temperature las vegas", "temperature vegas"],
    # International cities
    "TEL AVIV":      ["temperature tel aviv"],
    "MUNICH":        ["temperature munich"],
    "LONDON":        ["temperature london"],
    "TOKYO":         ["temperature tokyo"],
    "SEOUL":         ["temperature seoul"],
    "ANKARA":        ["temperature ankara"],
    "LUCKNOW":       ["temperature lucknow"],
    "WELLINGTON":    ["temperature wellington"],
    "TORONTO":       ["temperature toronto"],
    "PARIS":         ["temperature paris"],
    "MILAN":         ["temperature milan"],
    "SAO PAULO":     ["temperature sao paulo", "temperature são paulo"],
    "WARSAW":        ["temperature warsaw"],
    "SINGAPORE":     ["temperature singapore"],
    "SHANGHAI":      ["temperature shanghai"],
    "BEIJING":       ["temperature beijing"],
    "SHENZHEN":      ["temperature shenzhen"],
    "CHENGDU":       ["temperature chengdu"],
    "CHONGQING":     ["temperature chongqing"],
    "WUHAN":         ["temperature wuhan"],
    "HONG KONG":     ["temperature hong kong"],
}


def discover_and_import_weather_markets(log=print):
    """Discover weather markets on Polymarket and auto-import to Simmer.

    Searches the importable markets endpoint for weather events matching
    ACTIVE_LOCATIONS, then imports any that aren't already in Simmer.

    Discovery uses a per-location TTL cache (DISCOVERY_CACHE_MINUTES) so
    each city is rescanned on its own schedule.
    """
    cache_file = _p.Path(__file__).parent / "data" / "discovery_cache.json"
    cache_ttl_seconds = DISCOVERY_CACHE_MINUTES * 60
    try:
        per_loc_cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    except Exception:
        per_loc_cache = {}
    # Strip legacy global keys
    per_loc_cache.pop("last_discovery", None)

    client = get_client()
    imported_count = 0
    seen_urls = set()
    scanned_any = False

    def _save_cache():
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(per_loc_cache))
        except Exception:
            pass

    for location in ACTIVE_LOCATIONS:
        # Per-location TTL check
        last_scan = per_loc_cache.get(location, 0)
        if time.time() - last_scan < cache_ttl_seconds:
            log(f"  Discovery cache hit for {location} — skipping")
            continue

        if scanned_any:
            time.sleep(2)  # Rate limit between cities
        scanned_any = True

        search_terms = LOCATION_SEARCH_TERMS.get(location, [f"temperature {location.lower()}"])

        for term in search_terms:
            # Retry with exponential backoff on 429 from Simmer
            results = []
            wait_time = 2
            for attempt in range(3):
                try:
                    results = simmer_call(
                        client.list_importable_markets,
                        q=term, venue="polymarket", min_volume=1000, limit=20,
                        _label="list_importable",
                    )
                    break  # success
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "rate limit" in err_str.lower():
                        if attempt < 2:
                            log(f"  Simmer rate-limited on '{term}' — retrying in {wait_time}s...")
                            time.sleep(wait_time)
                            wait_time *= 3  # exponential backoff: 2s → 6s → 18s
                            continue
                        else:
                            log(f"  Simmer 429 persists after retries for '{term}' — skipping discovery")
                            _save_cache()
                            return imported_count
                    else:
                        log(f"  Discovery search failed for '{term}': {e}")
                        log_error("discovery_search", str(e), location=term)
                        break

            for m in results:
                url = m.get("url", "")
                question = (m.get("question") or "").lower()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                # Filter: must be a temperature market on Polymarket
                if "temperature" not in question:
                    continue
                if not url.startswith("https://polymarket.com/"):
                    continue

                # Try to import
                try:
                    result = simmer_call(client.import_market, url, _label="import_market")
                    status = result.get("status", "") if result else ""
                    if status == "imported":
                        imported_count += 1
                        log(f"  Imported: {m.get('question', url)[:70]}")
                    elif status == "already_exists":
                        pass  # Expected for most
                except Exception as e:
                    err_str = str(e)
                    if "rate limit" in err_str.lower() or "429" in err_str:
                        log(f"  Import rate limit reached — stopping discovery")
                        log_error("import_rate_limit", err_str, location=location)
                        _save_cache()
                        return imported_count
                    log_error("import_failed", err_str, location=location, url=url)
                    log(f"  Import failed for {url[:50]}: {e}")

        # Mark location as scanned successfully
        per_loc_cache[location] = time.time()

    _save_cache()
    return imported_count


# =============================================================================
# Simmer API - Trading
# =============================================================================

def fetch_weather_markets():
    """Fetch weather-tagged markets from Simmer API.

    Simmer returns ~500-700 active weather markets across all tracked cities.
    The 100-limit default silently dropped most of them and left whole cities
    (e.g. Buenos Aires) invisible to the scan. Pull up to Simmer's hard cap
    (1000 per request) so every ACTIVE_LOCATIONS city with live markets gets seen.
    """
    try:
        result = simmer_call(
            get_client()._request, "GET", "/api/sdk/markets",
            params={"tags": "weather", "status": "active", "limit": 1000},
            _label="markets",
        )
        return result.get("markets", [])
    except Exception:
        print("  Failed to fetch markets from Simmer API")
        return []


def execute_trade(market_id: str, side: str, amount: float, reasoning: str = None, signal_data: dict = None) -> dict:
    """Execute a buy trade via Simmer SDK with source tagging."""
    try:
        result = simmer_call(
            get_client().trade,
            market_id=market_id, side=side, amount=amount, source=TRADE_SOURCE, skill_slug=SKILL_SLUG,
            reasoning=reasoning, signal_data=signal_data, order_type=ORDER_TYPE,
            _label="trade_buy",
        )
        out = {
            "success": result.success, "trade_id": result.trade_id,
            "shares_bought": result.shares_bought, "shares": result.shares_bought,
            "error": result.error, "simulated": result.simulated,
            "order_status": result.order_status,
        }
        if result.order_status == "live":
            print(f"  [GTC] Order placed on book — waiting for fill (trade {result.trade_id})")
        return out
    except Exception as e:
        return {"success": False, "error": str(e)}


def execute_sell(market_id: str, shares: float) -> dict:
    """Execute a sell trade via Simmer SDK with source tagging."""
    try:
        result = simmer_call(
            get_client().trade,
            market_id=market_id, side="yes", action="sell",
            shares=shares, source=TRADE_SOURCE, skill_slug=SKILL_SLUG,
            order_type=ORDER_TYPE,
            _label="trade_sell",
        )
        out = {
            "success": result.success, "trade_id": result.trade_id,
            "error": result.error, "simulated": result.simulated,
            "order_status": result.order_status,
        }
        if result.order_status == "live":
            print(f"  [GTC] Sell order placed on book — waiting for fill (trade {result.trade_id})")
        return out
    except Exception as e:
        return {"success": False, "error": str(e)}


_positions_cache = {"expiry": 0.0, "data": None, "venue": None}
_POSITIONS_TTL = 30.0


def get_positions(venue: str = None, force_fresh: bool = False) -> list:
    """Get current positions as list of dicts, filtered by venue. 30s cache."""
    now = time.time()
    client = get_client()
    effective_venue = venue or client.venue
    if (not force_fresh
            and _positions_cache["data"] is not None
            and _positions_cache["venue"] == effective_venue
            and now < _positions_cache["expiry"]):
        return _positions_cache["data"]
    try:
        positions = simmer_call(client.get_positions, venue=effective_venue, _label="positions")
        from dataclasses import asdict
        data = [asdict(p) for p in positions]
        _positions_cache["data"] = data
        _positions_cache["venue"] = effective_venue
        _positions_cache["expiry"] = now + _POSITIONS_TTL
        return data
    except Exception as e:
        print(f"  Error fetching positions: {e}")
        return []


def calculate_position_size(default_size: float, smart_sizing: bool,
                            location: str | None = None) -> float:
    """
    Position sizing hierarchy:
      1. If location is given: use city-tier risk % (EASY 3% / MEDIUM 2% / HARD 1%)
         of paper balance — no cap.
      2. Else if smart_sizing: use SMART_SIZING_PCT of live portfolio balance.
      3. Otherwise return default_size (typically MAX_POSITION_USD).

    City-tier sizing is additive to smart_sizing — city tier wins when both
    are available. The empirical tiers were derived from 9k pro trades:
    HARD cities (Tokyo, Shanghai, Wellington, Beijing, Wuhan) size down to
    1% because even pros win <55% there; EASY cities (Tel Aviv, Warsaw, SF,
    LA, Milan, Chengdu, Houston, Munich, Seoul) size up to 3%.
    """
    # Path 1: city-tier risk-based sizing (works in both live and paper modes)
    if location:
        tier = city_tier(location)
        risk_pct = RISK_PCT_BY_TIER[tier]
        # Try live balance first, fall back to paper balance
        balance = None
        if smart_sizing:
            portfolio = get_portfolio()
            if portfolio:
                balance = portfolio.get("balance_usdc")
        if balance is None or balance <= 0:
            balance = _config.get("paper_balance", 10000.0)
        city_size = balance * risk_pct
        city_size = max(city_size, 1.0)
        print(f"  💡 City sizing: ${city_size:.2f} "
              f"({risk_pct:.0%} of ${balance:.2f}, tier={tier.upper()})")
        return city_size

    # Path 2: legacy smart_sizing (portfolio % without city tier)
    if not smart_sizing:
        return default_size

    portfolio = get_portfolio()
    if not portfolio:
        print(f"  ⚠️  Smart sizing failed, using default ${default_size:.2f}")
        return default_size

    balance = portfolio.get("balance_usdc", 0)
    if balance <= 0:
        print(f"  ⚠️  No available balance, using default ${default_size:.2f}")
        return default_size

    smart_size = balance * SMART_SIZING_PCT
    smart_size = max(smart_size, 1.0)

    print(f"  💡 Smart sizing: ${smart_size:.2f} ({SMART_SIZING_PCT:.0%} of ${balance:.2f} balance)")
    return smart_size


# =============================================================================
# Exit Strategy
# =============================================================================

def check_signal_invalidation(dry_run: bool = False, log=print) -> tuple:
    """
    Re-evaluate open paper trades against the latest ensemble forecast.
    If the models no longer support the position, exit early.

    Triggers (scaled by days-to-resolution):
      1. Forecast drift outside held bucket — D+0: >2°, D+1: >3°, D+2+: >5°
      2. Signal degraded to "weak" — D+0 only
      3. Agreement collapsed below 50% — D+0 only
      4. METAR contradiction — D+0 afternoon only

    Exit price: entry_price × 0.5 (conservative ~50% recovery estimate).
    We do NOT use model-implied bucket probability because it can diverge
    wildly from the actual market price, creating fake near-total losses.

    Returns: (checked, invalidated)
    """
    if not PAPER_JOURNAL_AVAILABLE:
        return 0, 0

    try:
        open_trades = get_open_positions()
    except Exception:
        return 0, 0

    if not open_trades:
        return 0, 0

    log(f"\n🔄 Signal invalidation check: {len(open_trades)} open position(s)...")
    checked = 0
    invalidated = 0
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for trade in open_trades:
        location = trade.get("location")
        date_str = trade.get("target_date")
        metric = trade.get("metric", "high")
        bucket_str = trade.get("bucket", "")
        entry_signal = trade.get("signal_strength", "")
        entry_agreement = trade.get("agreement_pct", 100)
        trade_id = trade.get("trade_id")

        if not location or not date_str or not bucket_str:
            continue

        # Parse the bucket to get (lo, hi, unit)
        parsed = parse_temperature_bucket(bucket_str)
        if not parsed:
            continue
        lo, hi, bucket_unit = parsed

        # Days until resolution — scale trigger thresholds accordingly
        try:
            from datetime import date as _date
            target = _date.fromisoformat(date_str)
            today = _date.fromisoformat(today_str)
            days_out = max(0, (target - today).days)
        except Exception:
            days_out = 99

        checked += 1
        reasons = []

        # Re-fetch ensemble forecast in the bucket's unit
        try:
            forecast = get_ensemble_forecast(
                city=location, date_str=date_str, metric=metric, unit=bucket_unit,
            )
        except Exception:
            continue

        new_temp = forecast.get("weighted_temp")
        new_signal = forecast.get("signal_strength", "")
        new_agreement = forecast.get("agreement_pct", 100)
        new_spread = forecast.get("max_delta")
        metar_temp = forecast.get("metar_temp")

        if new_temp is None:
            continue

        # --- Trigger 1: forecast moved outside bucket ---
        # Threshold widens with days-to-resolution: D+0=2°, D+1=3°, D+2+=5°
        drift_threshold = {0: 2.0, 1: 3.0}.get(days_out, 5.0)
        bucket_lo = lo if lo != -999 else new_temp - 50
        bucket_hi = hi if hi != 999 else new_temp + 50
        if new_temp < bucket_lo - drift_threshold:
            reasons.append(f"forecast {new_temp:.1f}° dropped {bucket_lo - new_temp:.1f}° below bucket [{lo}–{hi}] (D+{days_out}, threshold {drift_threshold}°)")
        elif new_temp > bucket_hi + drift_threshold:
            reasons.append(f"forecast {new_temp:.1f}° rose {new_temp - bucket_hi:.1f}° above bucket [{lo}–{hi}] (D+{days_out}, threshold {drift_threshold}°)")

        # --- Trigger 2: signal degraded to weak (D+0 only) ---
        if days_out == 0 and new_signal == "weak" and entry_signal in ("strong", "moderate"):
            reasons.append(f"signal degraded {entry_signal} → weak (D+0)")

        # --- Trigger 3: agreement collapsed <50% (D+0 only) ---
        if days_out == 0 and new_agreement < 50.0 and entry_agreement >= 70.0:
            reasons.append(f"agreement collapsed {entry_agreement:.0f}% → {new_agreement:.0f}% (D+0)")

        # --- Trigger 4: METAR contradicts bucket (D+0 high, afternoon only) ---
        if days_out == 0 and metar_temp is not None and metric == "high":
            from zoneinfo import ZoneInfo
            try:
                loc_data = INTERNATIONAL_LOCATIONS.get(location) or LOCATIONS.get(location) or {}
                tz_name = loc_data.get("tz", "UTC") if isinstance(loc_data, dict) else "UTC"
                from datetime import datetime as _dt
                local_hour = _dt.now(ZoneInfo(tz_name)).hour
            except Exception:
                local_hour = 0
            if local_hour >= 14:
                if metar_temp < bucket_lo - 2 or metar_temp > bucket_hi + 2:
                    reasons.append(f"METAR obs {metar_temp:.1f}° outside bucket [{lo}–{hi}]")

        if not reasons:
            continue

        # Signal invalidated — exit at ~50% of entry (conservative recovery estimate).
        entry_price = trade.get("entry_price", 0)
        exit_price = round(entry_price * 0.5, 4)

        log(f"  ⚠️  {location} {date_str} bucket [{bucket_str}] (D+{days_out}): INVALIDATED")
        for r in reasons:
            log(f"      → {r}")
        log(f"      Closing at ${exit_price:.4f} (~50% of entry ${entry_price:.4f})")

        # Close in paper journal
        try:
            from paper_journal import _load_trades, _save_trades, _compute_pnl, log_loss
            trades_all = _load_trades()
            for t in trades_all:
                if t.get("trade_id") == trade_id and t.get("status") == "open":
                    side = t.get("side", "yes")
                    entry = t.get("entry_price", 0)
                    shares = t.get("shares", 0)
                    pnl = _compute_pnl(side, entry, exit_price, shares)
                    t["status"] = "resolved"
                    t["outcome"] = "invalidated"
                    t["exit_price"] = exit_price
                    t["pnl"] = round(pnl, 4)
                    t["resolved_at"] = datetime.now(timezone.utc).isoformat()
                    t["resolution_source"] = "signal_invalidation"
                    t["invalidation_reasons"] = reasons
                    t["invalidation_forecast"] = {
                        "temp": new_temp,
                        "signal": new_signal,
                        "agreement": new_agreement,
                        "spread": new_spread,
                        "metar": metar_temp,
                        "days_out": days_out,
                    }
                    if pnl < 0:
                        log_loss(t)
                    emoji = "✅" if pnl > 0 else "❌"
                    log(f"      {emoji} Closed: P&L ${pnl:.4f} (entry ${entry:.4f} → exit ${exit_price:.4f})")
                    invalidated += 1
                    break
            _save_trades(trades_all)
        except Exception as e:
            log(f"      ❌ Failed to close trade: {e}")

    if checked and not invalidated:
        log(f"  ✓ All {checked} positions still valid")

    return checked, invalidated


def check_exit_opportunities(dry_run: bool = False, use_safeguards: bool = True) -> tuple:
    """Check open positions for exit opportunities. Returns: (exits_found, exits_executed)"""
    positions = get_positions()

    if not positions:
        return 0, 0

    weather_positions = []
    for pos in positions:
        question = pos.get("question", "").lower()
        sources = pos.get("sources", [])
        # Check if from weather skill OR has weather keywords
        if TRADE_SOURCE in sources or any(kw in question for kw in ["temperature", "°f", "highest temp"]):
            weather_positions.append(pos)

    if not weather_positions:
        return 0, 0

    print(f"\n📈 Checking {len(weather_positions)} weather positions for exit...")

    exits_found = 0
    exits_executed = 0

    for pos in weather_positions:
        market_id = pos.get("market_id")
        current_price = pos.get("current_price") or pos.get("price_yes") or 0
        shares = pos.get("shares_yes") or pos.get("shares") or 0
        entry_price = pos.get("avg_price") or pos.get("entry_price") or 0
        question = pos.get("question", "Unknown")[:50]

        if shares < MIN_SHARES_PER_ORDER:
            continue

        # Dynamic exit threshold: max(EXIT_THRESHOLD, entry * EXIT_PROFIT_MULTIPLIER)
        dynamic_exit = compute_dynamic_exit(entry_price) if entry_price > 0 else EXIT_THRESHOLD

        # Laddered exit: partial sale at ladder_first_exit if enabled and not yet at final
        if (LADDER_FIRST_EXIT > 0 and current_price >= LADDER_FIRST_EXIT
                and current_price < dynamic_exit and not pos.get("ladder_sold")):
            partial_shares = max(MIN_SHARES_PER_ORDER, shares * LADDER_FIRST_FRACTION)
            if partial_shares < shares and (shares - partial_shares) >= MIN_SHARES_PER_ORDER:
                exits_found += 1
                print(f"  🪜 {question}... (ladder)")
                print(f"     Price ${current_price:.2f} >= ladder exit ${LADDER_FIRST_EXIT:.2f} — partial sell {partial_shares:.1f}/{shares:.1f}")
                if use_safeguards:
                    ctx = get_market_context(market_id)
                    ok, reasons = check_context_safeguards(ctx)
                    if not ok:
                        print(f"     ⏭️  Skipped: {'; '.join(reasons)}")
                        continue
                tag = "SIMULATED" if dry_run else "LIVE"
                print(f"     Selling {partial_shares:.1f} shares ({tag})...")
                result = execute_sell(market_id, partial_shares)
                if result.get("success"):
                    exits_executed += 1
                    print(f"     ✅ {'[PAPER] ' if result.get('simulated') else ''}Sold {partial_shares:.1f} @ ${current_price:.2f}")
                else:
                    print(f"     ❌ Partial sell failed: {result.get('error', 'Unknown')}")
                continue  # Leave remainder for full-exit check next cycle

        if current_price >= dynamic_exit:
            exits_found += 1
            print(f"  📤 {question}...")
            print(f"     Price ${current_price:.2f} >= exit ${dynamic_exit:.2f} (base {EXIT_THRESHOLD:.2f}, entry {entry_price:.2f})")

            # Check safeguards before selling
            if use_safeguards:
                context = get_market_context(market_id)
                should_trade, reasons = check_context_safeguards(context)
                if not should_trade:
                    print(f"     ⏭️  Skipped: {'; '.join(reasons)}")
                    continue
                if reasons:
                    print(f"     ⚠️  Warnings: {'; '.join(reasons)}")

            # Re-fetch fresh share count to avoid selling more than available
            fresh_positions = get_positions(force_fresh=True)
            fresh_pos = next((p for p in fresh_positions if p.get("market_id") == market_id), None)
            if fresh_pos:
                fresh_shares = fresh_pos.get("shares_yes") or fresh_pos.get("shares") or 0
                if fresh_shares < MIN_SHARES_PER_ORDER:
                    print(f"     ⏭️  Skipped: fresh share count {fresh_shares:.1f} below minimum")
                    continue
                if fresh_shares != shares:
                    print(f"     ℹ️  Share count updated: {shares:.1f} → {fresh_shares:.1f}")
                    shares = fresh_shares

            tag = "SIMULATED" if dry_run else "LIVE"
            print(f"     Selling {shares:.1f} shares ({tag})...")
            result = execute_sell(market_id, shares)

            if result.get("success"):
                exits_executed += 1
                trade_id = result.get("trade_id")
                print(f"     ✅ {'[PAPER] ' if result.get('simulated') else ''}Sold {shares:.1f} shares @ ${current_price:.2f}")

                # Log sell trade context for journal (skip for paper trades)
                if trade_id and JOURNAL_AVAILABLE and not result.get("simulated"):
                    log_trade(
                        trade_id=trade_id,
                        source=TRADE_SOURCE, skill_slug=SKILL_SLUG,
                        thesis=f"Exit: price ${current_price:.2f} reached exit threshold ${EXIT_THRESHOLD:.2f}",
                        action="sell",
                    )
            else:
                error = result.get("error", "Unknown error")
                print(f"     ❌ Sell failed: {error}")
        else:
            print(f"  📊 {question}...")
            print(f"     Price ${current_price:.2f} < exit ${dynamic_exit:.2f} - hold")

    return exits_found, exits_executed


# =============================================================================
# Main Strategy Logic
# =============================================================================

def run_weather_strategy(dry_run: bool = True, positions_only: bool = False,
                         show_config: bool = False, smart_sizing: bool = False,
                         use_safeguards: bool = True, use_trends: bool = True,
                         quiet: bool = False, vol_targeting: bool = VOL_TARGETING):
    """Run the weather trading strategy."""
    def log(msg, force=False):
        """Print unless quiet mode is on. force=True always prints."""
        if not quiet or force:
            print(msg)

    log("🌤️  Simmer Weather Trading Skill")
    log("=" * 50)

    if dry_run:
        log("\n  [PAPER MODE] Trades will be simulated with real prices. Use --live for real trades.")

    log(f"\n⚙️  Configuration:")
    log(f"  Min edge:        {MIN_EDGE:+.0%} (primary entry gate: confidence - price)")
    log(f"  Entry ceiling:   {ENTRY_THRESHOLD:.0%} (price sanity cap)")
    log(f"  Exit threshold:  {EXIT_THRESHOLD:.0%} (sell above this)")
    log(f"  Max trades/run:  {MAX_TRADES_PER_RUN}")
    log(f"  Locations:       {', '.join(ACTIVE_LOCATIONS)}")
    log(f"  Smart sizing:    {'✓ Enabled' if smart_sizing else '✗ Disabled'}")
    log(f"  Safeguards:      {'✓ Enabled' if use_safeguards else '✗ Disabled'}")
    log(f"  Trend detection: {'✓ Enabled' if use_trends else '✗ Disabled'}")
    log(f"  Vol targeting:   {'✓ Enabled' if vol_targeting else '✗ Disabled'}")
    log(f"  Punt mode:       {'✓ Enabled' if PUNT_MODE else '✗ Disabled'}")
    if PUNT_MODE:
        log(f"    Punt size:     ${PUNT_MAX_POSITION_USD:.2f}")
        log(f"    Price ceiling: {PUNT_PRICE_CEILING:.1%}")
        log(f"    Min edge:      {PUNT_MIN_EDGE:+.0%}")
        log(f"    Min confidence:{PUNT_MIN_CONFIDENCE:.0%}")
        log(f"    Daily budget:  ${PUNT_DAILY_BUDGET_USD:.2f}")
    if vol_targeting:
        log(f"    Target vol:    {TARGET_VOL:.0%} annualized")
        log(f"    Max leverage:  {VOL_MAX_LEVERAGE:.1f}x")
        log(f"    Min alloc:     {VOL_MIN_ALLOCATION:.0%}")
        log(f"    EWMA span:     {VOL_SPAN}")

    if show_config:
        config_path = get_config_path(__file__)
        log(f"\n  Config file: {config_path}")
        log(f"  Config exists: {'Yes' if config_path.exists() else 'No'}")
        log("\n  To change settings, either:")
        log("  1. Create/edit config.json in skill directory:")
        log('     {"entry_threshold": 0.20, "exit_threshold": 0.50, "locations": "NYC,Chicago"}')
        log("  2. Or use --set flag:")
        log("     python weather_trader.py --set entry_threshold=0.20")
        log("  3. Or set environment variables (lowest priority):")
        log("     SIMMER_WEATHER_ENTRY=0.20")
        return

    # Fail fast if live trading requested without required credentials
    if not dry_run:
        validate_live_trading_prereqs()

    # Check daily loss limit before spending any API calls on new entries
    if daily_loss_limit_breached():
        realized = get_realized_daily_pnl()
        log(f"\n🛑 Daily loss limit breached: realized ${realized:.2f} ≤ -${MAX_DAILY_LOSS_USD:.2f}", force=True)
        log(f"   Skipping new entries. Exit scan will still run.", force=True)

    # Initialize client early to validate API key
    client = get_client(live=not dry_run)

    # Redeem any winning positions before starting the cycle
    try:
        redeemed = simmer_call(client.auto_redeem, _label="auto_redeem")
        for r in redeemed:
            if r.get("success"):
                log(f"  💰 Redeemed {r['market_id'][:8]}... ({r.get('side', '?')})")
    except Exception:
        pass  # Non-critical — don't block trading

    # Show portfolio if smart sizing enabled
    if smart_sizing:
        log("\n💰 Portfolio:")
        portfolio = get_portfolio()
        if portfolio:
            log(f"  Balance: ${portfolio.get('balance_usdc', 0):.2f}")
            log(f"  Exposure: ${portfolio.get('total_exposure', 0):.2f}")
            log(f"  Positions: {portfolio.get('positions_count', 0)}")
            by_source = portfolio.get('by_source', {})
            if by_source:
                log(f"  By source: {json.dumps(by_source, indent=4)}")

    if positions_only:
        log("\n📊 Current Positions:")
        positions = get_positions()
        if not positions:
            log("  No open positions")
        else:
            for pos in positions:
                log(f"  • {pos.get('question', 'Unknown')[:50]}...")
                sources = pos.get('sources', [])
                log(f"    YES: {pos.get('shares_yes', 0):.1f} | NO: {pos.get('shares_no', 0):.1f} | P&L: ${pos.get('pnl', 0):.2f} | Sources: {sources}")
        return

    # Check for newly resolved paper trades before scanning
    if PAPER_JOURNAL_AVAILABLE:
        try:
            newly_resolved = update_resolved_trades()
            if newly_resolved:
                for t in newly_resolved:
                    pnl = t.get('pnl', 0)
                    emoji = "✅" if pnl > 0 else "❌"
                    log(f"  {emoji} Paper trade resolved: {t.get('location')} {t.get('target_date')} {t.get('side', '').upper()} → {t.get('outcome', '')} | P&L: ${pnl:.4f}")
        except Exception:
            pass  # Non-critical

        # Show paper journal stats
        stats = get_stats()
        if stats.get('total_trades', 0) > 0:
            log(f"\n📓 Paper Journal: {stats['resolved_trades']} resolved | {stats['open_trades']} open | Win rate: {stats['win_rate']}% | P&L: ${stats['total_pnl']:.4f}")

    log("\n🔍 Discovering new weather markets on Polymarket...")
    newly_imported = discover_and_import_weather_markets(log=log)
    if newly_imported:
        log(f"  Auto-imported {newly_imported} new market(s)")
    else:
        log("  No new markets to import")

    log("\n📡 Fetching weather markets...")
    markets = fetch_weather_markets()
    log(f"  Found {len(markets)} weather markets")

    if not markets:
        log("  No weather markets available")
        return

    events = {}
    for market in markets:
        # Group by event_id if available, otherwise derive from question
        event_key = market.get("event_id")
        if not event_key:
            # Fall back: parse question to derive (location, date) grouping key
            info = parse_weather_event(market.get("event_name") or market.get("question", ""))
            event_key = f"{info['location']}_{info['date']}" if info else "unknown"
        if event_key not in events:
            events[event_key] = []
        events[event_key].append(market)

    log(f"  Grouped into {len(events)} events")

    # Pre-warm GRIB cache once, synchronously, before city forecast threads start.
    # Without this, every city thread detects a stale cache simultaneously and races
    # to download the same 47MB GRIB file, corrupting it and timing out the scan.
    log("  Pre-warming AIFS GRIB cache...")
    grib_warm = prewarm_grib_cache()
    log(f"  GRIB cache: {'warm' if grib_warm else 'unavailable (will use other models)'}")

    # Load persisted forecast cache from disk + fresh set for this run
    forecast_cache = _load_forecast_disk_cache()
    already_held_markets = get_open_market_ids()
    trades_executed = 0
    total_usd_spent = 0.0
    opportunities_found = 0
    skip_reasons = []
    execution_errors = []

    # =========================================================================
    # PASS 1: Collect all viable candidates (no trades yet)
    # =========================================================================
    candidates = []  # list of dicts with all fields needed to execute a trade
    punt_candidates = []  # separate list — never competes with core candidates

    for event_id, event_markets in events.items():
        event_name = event_markets[0].get("event_name") or event_markets[0].get("question", "")
        event_info = parse_weather_event(event_name)
        if not event_info:
            continue

        location = event_info["location"]
        date_str = event_info["date"]
        metric = event_info["metric"]

        if location.upper() not in ACTIVE_LOCATIONS:
            continue

        if BINARY_ONLY and len(event_markets) > 2:
            log(f"  ⏭️  Skipping range event ({len(event_markets)} outcomes) — binary_only=true")
            continue

        log(f"\n📍 {location} {date_str} ({metric} temp)")
        is_international = location in INTERNATIONAL_LOCATIONS

        # Ensemble forecast — in-memory cache keyed by (location, date, metric)
        cache_key_str = _cache_key_to_str((location, date_str, metric))
        newly_fetched = False
        if cache_key_str not in forecast_cache:
            log(f"  Fetching ensemble forecast (AIFS ENS + 6-model blend)...")
            forecast_cache[cache_key_str] = get_ensemble_forecast(
                city=location, date_str=date_str, metric=metric.lower(), unit='F',
            )
            newly_fetched = True

        forecasts = forecast_cache[cache_key_str] or {}
        # Log forecast for accuracy tracking (once per new fetch, after bucket matching)
        # market_id is set at line 1706 below
        _logged_market_id_for_cache = None
        forecast_temp = forecasts.get("weighted_temp") or forecasts.get("ensemble_mean")
        signal_strength = forecasts.get("signal_strength", "unknown")
        models_used = forecasts.get("models_count", 0)
        agreement_pct = forecasts.get("agreement_pct", 0)
        spread = forecasts.get("max_delta")
        if forecast_temp is None:
            err_msg = forecasts.get("error", "unknown")
            log(f"  ⚠️  No ensemble forecast for {date_str} (signal: {signal_strength})")
            skip_reasons.append(f"no forecast: {err_msg}")
            log_error("no_forecast", err_msg, location=location, date=date_str, metric=metric, signal=signal_strength)
            continue

        # Markets resolve in whole degrees. Round to the nearest integer in the
        # market's native unit so bucket selection reflects what will actually settle.
        if is_international:
            raw_c = (forecast_temp - 32) * 5 / 9
            bias_c = LOCATION_BIAS_C.get(location, 0.0)
            rounded_c = round(raw_c + bias_c)
            forecast_temp = rounded_c * 9 / 5 + 32  # store as °F for probability math
            unit_label = "°C"
            bias_str = f" (bias {bias_c:+.1f}°C)" if bias_c else ""
            display_temp = f"{rounded_c}°C{bias_str}"
        else:
            forecast_temp = round(forecast_temp)
            unit_label = "°F"
            display_temp = f"{forecast_temp}°F"
        log(f"  AIFS ENS: {display_temp} | signal: {signal_strength} | {models_used} models | agree: {agreement_pct}% | spread: {spread}°")

        # Rank every bucket in this event by edge (Gaussian bucket probability
        # × signal-strength discount − market price). Picks the BEST bucket
        # per event, not just the one containing the forecast. Ranges and
        # thresholds with fat probability mass naturally beat narrow exacts
        # when the market prices them inefficiently — matching pro strategy
        # (Hans323: 72% range, 22% threshold, 5% exact).
        ranked_buckets = rank_event_buckets_by_edge(
            event_markets, forecast_temp, spread, signal_strength,
            is_international=is_international,
        )
        matching_market = None
        matched_rb = None
        for rb in ranked_buckets:
            p = rb["price"]
            # Skip extreme / off-book prices, but keep walking the ranked list
            if p < MIN_TICK_SIZE or p > (1 - MIN_TICK_SIZE):
                continue
            # List is sorted by edge desc — once below MIN_EDGE, nothing better remains
            if rb["edge"] < MIN_EDGE:
                break
            matching_market = rb["market"]
            matched_rb = rb
            break

        # Punt scan: find deep tail-priced mispricings in this event.
        # Runs regardless of whether core matched — excludes core's bucket if any.
        if PUNT_MODE:
            _core_match_id = matching_market.get("id") if matching_market else None
            _event_punts = find_punt_candidates(
                event_markets=event_markets,
                forecast_temp=forecast_temp,
                spread=spread,
                core_match_id=_core_match_id,
                already_held=already_held_markets,
                location=location, date_str=date_str, metric=metric,
                is_international=is_international,
                signal_strength=signal_strength,
                models_used=models_used,
                agreement_pct=agreement_pct,
            )
            for _p in _event_punts:
                log(f"  🎯 Punt candidate: {_p['outcome_name']} @ ${_p['price']:.3f} | model_prob={_p['confidence']:.0%} | edge={_p['edge']:+.2f}")
            punt_candidates.extend(_event_punts)

        if not matching_market:
            # Prefer the top-ranked bucket (matcher output) over a distance-based
            # "nearest" re-scan — the ranker already considered every parseable
            # bucket, so its #1 is the true best candidate. Surface the actual
            # edge/confidence/price so it's obvious why the gate failed.
            top_rb = ranked_buckets[0] if ranked_buckets else None
            if top_rb is not None:
                _outcome = top_rb.get("outcome_name") or "?"
                _price = top_rb.get("price") or 0.0
                _conf = top_rb.get("confidence") or 0.0
                _edge = top_rb.get("edge") or 0.0
                if _price < MIN_TICK_SIZE or _price > (1 - MIN_TICK_SIZE):
                    _why = f"price extreme ({_price:.3f})"
                else:
                    _why = f"edge {_edge:+.2%} < {MIN_EDGE:.0%}"
                log(f"  ⚠️  No entry for {display_temp} — best bucket: {_outcome} @ ${_price:.2f} "
                    f"(conf {_conf:.0%}, {_why})")
            else:
                unparsed_count = sum(1 for m in event_markets if not parse_market_bucket(m)[0])
                log(f"  ⚠️  No parseable buckets for {display_temp} — "
                    f"{len(event_markets)} markets, {unparsed_count} unparseable")
                # Diagnostic: show what fields the markets actually expose so we
                # can see whether bucket info is in outcome_name, question, or elsewhere.
                for m in event_markets[:3]:
                    sample_fields = {k: m.get(k) for k in ("outcome_name", "outcome", "name", "question") if m.get(k)}
                    log(f"    market sample: {sample_fields}")
            skip_reasons.append("no bucket match")

            # Record the skip in skip_events.jsonl so the funnel is debuggable.
            # Distinguish between "no parseable buckets" (no markets the ranker
            # could score) and "best bucket below edge gate" (ranker found
            # options but none cleared MIN_EDGE or were all at price extremes).
            best_rb = ranked_buckets[0] if ranked_buckets else None
            if best_rb is None:
                _log_skip("no_bucket_parseable", location, date_str, metric,
                          signal_strength=signal_strength, spread=spread)
            else:
                best_price = best_rb.get("price")
                best_conf = best_rb.get("confidence")
                best_edge = best_rb.get("edge")
                best_mid = best_rb["market"].get("id")
                # Classify: extreme price vs edge below MIN_EDGE
                if best_price is not None and (best_price < MIN_TICK_SIZE or best_price > (1 - MIN_TICK_SIZE)):
                    reason = "no_bucket_price_extreme"
                    threshold, actual = None, best_price
                else:
                    reason = "no_bucket_low_edge"
                    threshold, actual = MIN_EDGE, best_edge
                _log_skip(reason, location, date_str, metric,
                          market_id=best_mid, price=best_price,
                          confidence=best_conf, edge=best_edge, spread=spread,
                          signal_strength=signal_strength,
                          threshold=threshold, actual=actual)

            # Log forecast even when no bucket match
            if newly_fetched and FORECAST_HISTORY_AVAILABLE and forecasts.get("weighted_temp") is not None:
                try:
                    log_forecast(
                        location=location, date_str=date_str, metric=metric,
                        forecast_temp=forecasts.get("weighted_temp"),
                        signal_strength=forecasts.get("signal_strength", "unknown"),
                        models_used=forecasts.get("models_count", 0),
                        agreement_pct=forecasts.get("agreement_pct", 0),
                        spread=forecasts.get("max_delta"),
                        model_temps=forecasts.get("model_temps"),
                        market_id=None,
                    )
                except Exception:
                    pass
            continue

        # Re-extract the parseable bucket label (matched_market may have
        # outcome_name="Yes" with the bucket info in the question field).
        _matched_bucket, outcome_name = parse_market_bucket(matching_market)
        if not outcome_name:
            outcome_name = matching_market.get("outcome_name", "") or matching_market.get("question", "")
        # Clean bucket label synthesized from the parsed (lo, hi, unit) tuple.
        # Ensures paper journal stores a parseable bucket string even when
        # outcome_name was the full question text.
        if _matched_bucket:
            _lo, _hi, _unit = _matched_bucket
            if _lo == -999:
                bucket_label = f"{_hi}°{_unit} or below"
            elif _hi == 999:
                bucket_label = f"{_lo}°{_unit} or above"
            elif _lo == _hi:
                bucket_label = f"{_lo}°{_unit}"
            else:
                bucket_label = f"{_lo}-{_hi}°{_unit}"
        else:
            bucket_label = outcome_name

        # Cross-check: if the ranker selected this market, its bucket info
        # should match what parse_market_bucket returns. If they disagree
        # (e.g. ranker saw different data than parser), prefer the ranker's
        # bucket since that's what the probability was computed against.
        if matched_rb and _matched_bucket:
            rb_bucket = matched_rb.get("bucket")
            if rb_bucket and rb_bucket != _matched_bucket:
                _rlo, _rhi, _runit = rb_bucket
                if _rlo == -999:
                    bucket_label = f"{_rhi}°{_runit} or below"
                elif _rhi == 999:
                    bucket_label = f"{_rlo}°{_runit} or above"
                elif _rlo == _rhi:
                    bucket_label = f"{_rlo}°{_runit}"
                else:
                    bucket_label = f"{_rlo}-{_rhi}°{_runit}"
                log(f"  ⚠️  Bucket mismatch: parser={_lo}-{_hi}°{_unit} vs ranker={_rlo}-{_rhi}°{_runit} — using ranker")
        price = matching_market.get("external_price_yes") or 0.5
        market_id = matching_market.get("id")
        log(f"  Matching bucket: {bucket_label} @ ${price:.2f}")

        # Log forecast after bucket match so we have market_id
        if newly_fetched and FORECAST_HISTORY_AVAILABLE and forecasts.get("weighted_temp") is not None:
            try:
                log_forecast(
                    location=location, date_str=date_str, metric=metric,
                    forecast_temp=forecasts.get("weighted_temp"),
                    signal_strength=forecasts.get("signal_strength", "unknown"),
                    models_used=forecasts.get("models_count", 0),
                    agreement_pct=forecasts.get("agreement_pct", 0),
                    spread=forecasts.get("max_delta"),
                    model_temps=forecasts.get("model_temps"),
                    market_id=market_id,
                )
            except Exception:
                pass

        if price < MIN_TICK_SIZE or price > (1 - MIN_TICK_SIZE):
            log(f"  ⏸️  Price ${price:.4f} at extreme — skip")
            skip_reasons.append("price at extreme")
            _log_skip("price_extreme", location, date_str, metric, market_id=market_id,
                      price=price, signal_strength=signal_strength, spread=spread)
            continue

        # Confidence comes from the bucket-specific probability computed in
        # rank_event_buckets_by_edge (Gaussian prob × signal-strength discount).
        # Fall back to flat signal-strength heuristic if ranking wasn't used.
        if matched_rb is not None:
            confidence = matched_rb["confidence"]
        elif signal_strength == "strong":
            confidence = 0.88
        elif signal_strength == "moderate":
            confidence = 0.80
        elif signal_strength == "weak":
            confidence = 0.68
        else:
            confidence = 0.72

        # Hard spread cap — skip regardless of edge or signal strength
        MAX_SPREAD = 5.8
        if spread is not None and spread > MAX_SPREAD:
            log(f"  ⏸️  Spread {spread}° exceeds max {MAX_SPREAD}° — skip")
            skip_reasons.append(f"spread>{MAX_SPREAD}°")
            _log_skip("high_spread", location, date_str, metric, market_id=market_id,
                      price=price, confidence=confidence, spread=spread,
                      signal_strength=signal_strength, threshold=MAX_SPREAD, actual=spread)
            continue

        # Aggregation: don't re-buy markets we already hold
        if market_id and market_id in already_held_markets:
            log(f"  ⏭️  Already holding position in this market — skipping re-entry")
            skip_reasons.append("already held")
            _log_skip("already_held", location, date_str, metric, market_id=market_id,
                      price=price, confidence=confidence, spread=spread,
                      signal_strength=signal_strength)
            continue

        # Same-event check: if we already have an open position on this
        # (location, date, metric) — regardless of bucket — skip to avoid
        # redundant same-side exposure across multiple buckets on one event
        if PAPER_JOURNAL_AVAILABLE:
            try:
                open_by_event = get_open_positions_by_event()
                event_key = (location, date_str, metric)
                existing = open_by_event.get(event_key)
                if existing:
                    log(f"  ⏭️  Already holding {existing['side'].upper()} on {location} {date_str} {metric} — skipping (bucket '{existing['bucket']}' already open)")
                    skip_reasons.append("same-event position already open")
                    _log_skip("same_event_open", location, date_str, metric, market_id=market_id,
                              price=price, confidence=confidence, spread=spread,
                              signal_strength=signal_strength)
                    continue
            except Exception:
                pass

        # Primary entry gate: edge (confidence - price). Price ceiling is a
        # loose sanity cap only — avoids buckets priced near resolution.
        edge = confidence - price
        if edge < MIN_EDGE:
            log(f"  ⏸️  Edge {edge:+.2f} below min {MIN_EDGE:+.2f} (price ${price:.2f}, conf {confidence:.2f}) - skip")
            skip_reasons.append(f"edge<{MIN_EDGE:+.2f}")
            _log_skip("low_edge", location, date_str, metric, market_id=market_id,
                      price=price, confidence=confidence, edge=edge, spread=spread,
                      signal_strength=signal_strength, threshold=MIN_EDGE, actual=edge)
            continue
        if price >= ENTRY_THRESHOLD:
            log(f"  ⏸️  Price ${price:.2f} above ceiling ${ENTRY_THRESHOLD:.2f} - skip")
            skip_reasons.append("price above ceiling")
            _log_skip("price_ceiling", location, date_str, metric, market_id=market_id,
                      price=price, confidence=confidence, edge=edge, spread=spread,
                      signal_strength=signal_strength, threshold=ENTRY_THRESHOLD, actual=price)
            continue
        # Weak signals are allowed through only when edge is strong enough (gate above).
        # Log a note so we can track which trades came from weak signals.
        if signal_strength == "weak":
            log(f"  ⚠️  Weak signal ({spread}° spread) but edge {edge:+.2f} ≥ {MIN_EDGE:+.2f} — allowing")

        candidates.append({
            "location": location, "date_str": date_str, "metric": metric,
            "market": matching_market, "market_id": market_id,
            "outcome_name": outcome_name, "price": price, "confidence": confidence,
            "edge": confidence - price,
            "signal_strength": signal_strength, "models_used": models_used,
            "agreement_pct": agreement_pct, "spread": spread,
            "forecast_temp": forecast_temp, "unit_label": unit_label,
            "display_temp": display_temp,
            "is_international": is_international,
        })
        opportunities_found += 1

    # =========================================================================
    # PASS 2: Rank by edge and (optionally) concurrent context fetch, then execute
    # =========================================================================
    candidates.sort(key=lambda c: c["edge"], reverse=True)
    if candidates:
        log(f"\n🏆 Ranked {len(candidates)} candidate(s) by edge (highest first)")

    # Batch-fetch market context + price history concurrently for top candidates
    top_n = candidates[:MAX_TRADES_PER_RUN] if not daily_loss_limit_breached() else []
    context_map, history_map = {}, {}
    if CONCURRENT_SCANS and top_n and (use_safeguards or use_trends or vol_targeting):
        from concurrent.futures import ThreadPoolExecutor
        # Keep concurrency low to avoid Simmer 429s — the simmer_call throttle
        # will serialize requests anyway, but fewer workers = less coordination cost
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {}
            for c in top_n:
                mid = c["market_id"]
                if use_safeguards:
                    futures[ex.submit(get_market_context, mid, c["confidence"])] = ("ctx", mid)
                if use_trends or vol_targeting:
                    futures[ex.submit(get_price_history, mid)] = ("hist", mid)
            for fut, (kind, mid) in futures.items():
                try:
                    result = fut.result(timeout=20)
                    if kind == "ctx":
                        context_map[mid] = result
                    else:
                        history_map[mid] = result
                except Exception:
                    pass

    for c in top_n:
        if trades_executed >= MAX_TRADES_PER_RUN:
            skip_reasons.append("max trades reached")
            break
        if daily_loss_limit_breached():
            skip_reasons.append("daily loss limit")
            break

        market_id = c["market_id"]
        price = c["price"]
        confidence = c["confidence"]
        forecast_temp = c["forecast_temp"]
        unit_label = c["unit_label"]
        display_temp = c["display_temp"]
        outcome_name = c["outcome_name"]
        location = c["location"]
        date_str = c["date_str"]
        metric = c["metric"]
        signal_strength = c["signal_strength"]
        models_used = c["models_used"]
        agreement_pct = c["agreement_pct"]
        spread = c["spread"]
        matching_market = c["market"]
        is_international = c["is_international"]

        log(f"\n📍 {location} {date_str} ({metric} temp) | edge {c['edge']:.1%}")

        # Safeguards (use pre-fetched if available)
        if use_safeguards:
            context = context_map.get(market_id) if CONCURRENT_SCANS else get_market_context(market_id, my_probability=confidence)
            should_trade, reasons = check_context_safeguards(context)
            if not should_trade:
                log(f"  ⏭️  Safeguard blocked: {'; '.join(reasons)}")
                skip_reasons.append(f"safeguard: {reasons[0]}")
                _log_skip(f"safeguard:{reasons[0]}", location, date_str, metric,
                          market_id=market_id, price=price, confidence=confidence,
                          edge=c["edge"], spread=c["spread"], signal_strength=signal_strength)
                continue
            if reasons:
                log(f"  ⚠️  Warnings: {'; '.join(reasons)}")

        history = history_map.get(market_id, []) if CONCURRENT_SCANS else (get_price_history(market_id) if (use_trends or vol_targeting) else [])

        trend_bonus = ""
        if use_trends and history:
            trend = detect_price_trend(history)
            if trend["is_opportunity"]:
                trend_bonus = f" 📉 (dropped {abs(trend['change_24h']):.0%} in 24h)"
            elif trend["direction"] == "up":
                trend_bonus = f" 📈 (up {trend['change_24h']:.0%} in 24h)"

        position_size = calculate_position_size(MAX_POSITION_USD, smart_sizing, location=location)
        vol_meta = None
        if vol_targeting and history:
            current_vol = calculate_ewma_vol(history, span=VOL_SPAN)
            position_size, vol_meta = apply_vol_targeting(
                position_size, current_vol,
                target_vol=TARGET_VOL, max_leverage=VOL_MAX_LEVERAGE,
                min_allocation=VOL_MIN_ALLOCATION,
            )
            if current_vol is not None:
                log(f"  📊 Vol targeting: realized={current_vol:.0%} → {vol_meta['leverage']:.2f}x (${position_size:.2f})")

        if MIN_SHARES_PER_ORDER * price > position_size:
            log(f"  ⚠️  Position size ${position_size:.2f} too small for {MIN_SHARES_PER_ORDER} shares at ${price:.2f}")
            skip_reasons.append("position too small")
            _log_skip("position_too_small", location, date_str, metric,
                      market_id=market_id, price=price, confidence=confidence,
                      edge=c["edge"], spread=c["spread"], signal_strength=signal_strength)
            continue

        _mt = forecasts.get("model_temps", {})
        _mt_str = ", ".join(f"{k}:{v:.1f}°" for k, v in sorted(_mt.items())) if _mt else "?"
        log(f"  ✅ BUY opportunity!{trend_bonus} | {_mt_str} → {display_temp}")
        tag = "SIMULATED" if dry_run else "LIVE"
        log(f"  Executing trade ({tag})...", force=True)

        signal = {
            "edge": round(c["edge"], 4),
            "confidence": round(confidence, 2),
            "signal_source": "aifs_ensemble",
            "forecast_temp": forecast_temp,
            "bucket_range": outcome_name,
            "market_price": round(price, 4),
            "threshold": ENTRY_THRESHOLD,
            "models_used": models_used,
            "agreement_pct": agreement_pct,
            "spread": spread,
            "signal_strength": signal_strength,
            "model_temps": forecasts.get("model_temps"),
        }
        if vol_meta:
            signal["vol_targeting"] = vol_meta

        result = execute_trade(
            market_id, "yes", position_size,
            reasoning=f"Ensemble {display_temp} → bucket {outcome_name} underpriced at {price:.0%}",
            signal_data=signal,
        )

        if result.get("success"):
            trades_executed += 1
            total_usd_spent += position_size
            shares = result.get("shares_bought") or result.get("shares") or 0
            trade_id = result.get("trade_id")
            log(f"  ✅ {'[PAPER] ' if result.get('simulated') else ''}Bought {shares:.1f} shares @ ${price:.2f}", force=True)

            if trade_id and JOURNAL_AVAILABLE and not result.get("simulated"):
                # Journal confidence ~ the model confidence shifted by realized edge.
                journal_confidence = min(0.95, max(0.5, confidence + 0.5 * (confidence - price)))
                log_trade(
                    trade_id=trade_id,
                    source=TRADE_SOURCE, skill_slug=SKILL_SLUG,
                    thesis=f"{'Open-Meteo' if is_international else 'Ensemble'} forecasts {display_temp} for {location} on {date_str}, bucket '{outcome_name}' @ ${price:.2f}",
                    confidence=round(journal_confidence, 2),
                    location=location, forecast_temp=forecast_temp,
                    target_date=date_str, metric=metric,
                )

            if result.get("simulated") and PAPER_JOURNAL_AVAILABLE and market_id:
                try:
                    log_paper_trade(
                        market_id=market_id,
                        question=matching_market.get("question", "") or matching_market.get("event_name", ""),
                        side="yes", entry_price=price, shares=shares,
                        cost=position_size, bucket=bucket_label,
                        forecast_temp=forecast_temp, signal_strength=signal_strength,
                        location=location, date_str=date_str, metric=metric,
                        models_used=models_used, agreement_pct=agreement_pct, spread=spread,
                        model_temps=forecasts.get("model_temps"),
                        confidence=confidence,
                    )
                except Exception:
                    pass
        else:
            error = result.get("error", "Unknown error")
            log(f"  ❌ Trade failed: {error}", force=True)
            execution_errors.append(error[:120])

    # Persist forecast cache to disk for next run
    _save_forecast_disk_cache(forecast_cache)

    # =========================================================================
    # PUNT EXECUTION PASS (side strategy — isolated from core budget/rules)
    # =========================================================================
    punts_executed = 0
    punt_usd_spent = 0.0
    if PUNT_MODE and punt_candidates:
        # Compute remaining daily punt budget by summing today's punt journal entries
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        punt_usd_today = 0.0
        if PAPER_JOURNAL_AVAILABLE:
            try:
                from paper_journal import _load_trades
                for _t in _load_trades():
                    if _t.get("strategy") != "punt":
                        continue
                    entered = str(_t.get("entered_at", ""))[:10]
                    if entered == today_utc:
                        punt_usd_today += float(_t.get("cost", 0.0) or 0.0)
            except Exception:
                pass
        remaining_budget = max(0.0, PUNT_DAILY_BUDGET_USD - punt_usd_today)
        log(f"\n🎯 Punt pass: {len(punt_candidates)} candidate(s), ${remaining_budget:.2f} daily budget remaining")

        # Rank by edge descending
        punt_candidates.sort(key=lambda c: c["edge"], reverse=True)
        # Dedupe by market_id (in case same bucket punt-scanned twice)
        _seen_punt_ids = set()
        for p in punt_candidates:
            if remaining_budget < PUNT_MAX_POSITION_USD:
                log(f"  ⏸️  Punt daily budget exhausted (${punt_usd_today + punt_usd_spent:.2f}/${PUNT_DAILY_BUDGET_USD:.2f})")
                break
            mid = p.get("market_id")
            if not mid or mid in _seen_punt_ids:
                continue
            _seen_punt_ids.add(mid)

            size = PUNT_MAX_POSITION_USD
            tag = "[PAPER]" if dry_run else "[LIVE]"
            log(f"  🎯 PUNT {tag} {p['location']} {p['date_str']} {p['outcome_name']} @ ${p['price']:.3f} | prob={p['confidence']:.0%} | edge={p['edge']:+.2f} | ${size:.2f}", force=True)

            try:
                result = execute_trade(
                    mid, "yes", size,
                    reasoning=f"PUNT: ensemble says {p['confidence']:.0%} but market priced at {p['price']:.1%} — {p['outcome_name']}",
                    signal_data={
                        "strategy": "punt",
                        "edge": round(p["edge"], 4),
                        "model_probability": round(p["confidence"], 4),
                        "market_price": round(p["price"], 4),
                        "forecast_temp": p["forecast_temp"],
                        "spread": p.get("spread"),
                        "signal_strength": p.get("signal_strength"),
                    },
                )
            except Exception as e:
                log(f"  ❌ Punt trade failed: {e}", force=True)
                continue

            if result.get("success"):
                punts_executed += 1
                punt_usd_spent += size
                remaining_budget -= size
                shares = result.get("shares_bought") or result.get("shares") or 0
                log(f"  ✅ {tag} Punt bought {shares:.0f} shares @ ${p['price']:.3f}", force=True)

                if result.get("simulated") and PAPER_JOURNAL_AVAILABLE:
                    # Synthesize a clean bucket label from the parsed bucket
                    # (may differ from outcome_name if that was the full question).
                    _pb, _ = parse_market_bucket(p["market"])
                    if _pb:
                        _lo, _hi, _u = _pb
                        if _lo == -999:
                            _label = f"{_hi}°{_u} or below"
                        elif _hi == 999:
                            _label = f"{_lo}°{_u} or above"
                        elif _lo == _hi:
                            _label = f"{_lo}°{_u}"
                        else:
                            _label = f"{_lo}-{_hi}°{_u}"
                    else:
                        _label = p["outcome_name"]
                    try:
                        log_paper_trade(
                            market_id=mid,
                            question=p["market"].get("question", "") or p["market"].get("event_name", ""),
                            side="yes", entry_price=p["price"], shares=shares,
                            cost=size, bucket=_label,
                            forecast_temp=p["forecast_temp"],
                            signal_strength=p.get("signal_strength", "punt"),
                            location=p["location"], date_str=p["date_str"], metric=p["metric"],
                            models_used=p.get("models_used", 0),
                            agreement_pct=p.get("agreement_pct", 0),
                            spread=p.get("spread"),
                            model_temps=p.get("model_temps"),
                            strategy="punt",
                            confidence=p.get("confidence"),
                        )
                    except Exception:
                        pass
            else:
                log(f"  ❌ Punt trade failed: {result.get('error', 'unknown')}", force=True)

    # Signal invalidation: disabled — bucket parsing bugs caused false closures.
    # Re-enable once bucket data from Simmer is trustworthy end-to-end.
    inv_checked, inv_closed = 0, 0
    # inv_checked, inv_closed = check_signal_invalidation(dry_run, log)

    exits_found, exits_executed = check_exit_opportunities(dry_run, use_safeguards)

    log("\n" + "=" * 50)
    total_trades = trades_executed + exits_executed + punts_executed + inv_closed
    show_summary = not quiet or total_trades > 0
    if show_summary:
        print("📊 Summary:")
        print(f"  Events scanned: {len(events)}")
        print(f"  Entry opportunities: {opportunities_found}")
        print(f"  Exit opportunities:  {exits_found}")
        print(f"  Core trades:         {trades_executed + exits_executed}")
        if inv_closed:
            print(f"  Signal invalidations: {inv_closed}/{inv_checked} positions closed")
        if PUNT_MODE:
            print(f"  Punts found:         {len(punt_candidates)}")
            print(f"  Punts executed:      {punts_executed}  (${punt_usd_spent:.2f})")
        print(f"  Trades executed:     {total_trades}")

    # Structured report for automaton
    if os.environ.get("AUTOMATON_MANAGED"):
        global _automaton_reported
        report = {"signals": opportunities_found + exits_found, "trades_attempted": opportunities_found + exits_found, "trades_executed": total_trades, "amount_usd": round(total_usd_spent, 2)}
        if (opportunities_found + exits_found) > 0 and total_trades == 0 and skip_reasons:
            report["skip_reason"] = ", ".join(dict.fromkeys(skip_reasons))
        if execution_errors:
            report["execution_errors"] = execution_errors
        print(json.dumps({"automaton": report}))
        _automaton_reported = True

    if dry_run and show_summary:
        print("\n  [PAPER MODE - trades simulated with real prices]")


# =============================================================================
# CLI Entry Point
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simmer Weather Trading Skill")
    parser.add_argument("--live", action="store_true", help="Execute real trades (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="(Default) Show opportunities without trading")
    parser.add_argument("--positions", action="store_true", help="Show current positions only")
    parser.add_argument("--config", action="store_true", help="Show current config")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="Set config value (e.g., --set entry_threshold=0.20)")
    parser.add_argument("--smart-sizing", action="store_true", help="Use portfolio-based position sizing")
    parser.add_argument("--no-safeguards", action="store_true", help="Disable context safeguards")
    parser.add_argument("--no-trends", action="store_true", help="Disable price trend detection")
    parser.add_argument("--vol-targeting", action="store_true", help="Enable volatility targeting (dynamic position sizing based on realized vol)")
    parser.add_argument("--punt-mode", action="store_true", help="Enable punt mode: buy deeply-mispriced tail buckets with small stakes (side strategy)")
    parser.add_argument("--late", action="store_true", help="Run LATE mode only: day-of intraday scan using TWC observations (no CORE/PUNT pass).")
    parser.add_argument("--late-force", action="store_true", help="With --late, ignore the local time-of-day window.")
    parser.add_argument("--late-city", help="With --late, process only this city.")
    parser.add_argument("--quiet", "-q", action="store_true", help="Only output when trades execute or errors occur (ideal for high-frequency runs)")
    args = parser.parse_args()

    if args.late:
        # Delegate to the standalone LATE runner. Kept separate because its
        # signal source (TWC intraday) has nothing in common with the ensemble
        # forecast pipeline that CORE and PUNT share.
        import subprocess
        cmd = [sys.executable, str(_p.Path(__file__).parent / "late_trader.py")]
        if args.live:
            cmd.append("--live")
        if args.late_force:
            cmd.append("--force")
        if args.late_city:
            cmd.extend(["--city", args.late_city])
        sys.exit(subprocess.call(cmd))

    # Handle --set config updates
    if args.set:
        updates = {}
        for item in args.set:
            if "=" in item:
                key, value = item.split("=", 1)
                # Try to convert to appropriate type
                if key in CONFIG_SCHEMA:
                    type_fn = CONFIG_SCHEMA[key].get("type", str)
                    try:
                        if type_fn == bool:
                            value = value.lower() in ('true', '1', 'yes')
                        else:
                            value = type_fn(value)
                    except (ValueError, TypeError):
                        pass
                updates[key] = value
        if updates:
            updated = update_config(updates, __file__)
            print(f"✅ Config updated: {updates}")
            print(f"   Saved to: {get_config_path(__file__)}")
            # Reload config
            _config = load_config(CONFIG_SCHEMA, __file__, slug="polymarket-weather-trader")
            # Update module-level vars
            globals()["ENTRY_THRESHOLD"] = _config["entry_threshold"]
            globals()["MIN_EDGE"] = _config["min_edge"]
            globals()["EXIT_THRESHOLD"] = _config["exit_threshold"]
            globals()["MAX_POSITION_USD"] = _config["max_position_usd"]
            globals()["SMART_SIZING_PCT"] = _config["sizing_pct"]
            globals()["MAX_TRADES_PER_RUN"] = _config["max_trades_per_run"]
            globals()["BINARY_ONLY"] = _config["binary_only"]
            globals()["VOL_TARGETING"] = _config["vol_targeting"]
            globals()["TARGET_VOL"] = _config["target_vol"]
            globals()["VOL_MAX_LEVERAGE"] = _config["vol_max_leverage"]
            globals()["VOL_MIN_ALLOCATION"] = _config["vol_min_allocation"]
            globals()["VOL_SPAN"] = _config["vol_span"]
            globals()["MAX_DAILY_LOSS_USD"] = _config["max_daily_loss_usd"]
            globals()["EXIT_PROFIT_MULTIPLIER"] = _config["exit_profit_multiplier"]
            globals()["LADDER_FIRST_EXIT"] = _config["ladder_first_exit"]
            globals()["LADDER_FIRST_FRACTION"] = _config["ladder_first_fraction"]
            globals()["DISCOVERY_CACHE_MINUTES"] = _config["discovery_cache_minutes"]
            globals()["FORECAST_CACHE_DISK"] = _config["forecast_cache_disk"]
            globals()["CONCURRENT_SCANS"] = _config["concurrent_scans"]
            globals()["LOG_LEVEL"] = _config["log_level"]
            globals()["PUNT_MODE"] = _config["punt_mode"]
            globals()["PUNT_MAX_POSITION_USD"] = _config["punt_max_position_usd"]
            globals()["PUNT_PRICE_CEILING"] = _config["punt_price_ceiling"]
            globals()["PUNT_MIN_EDGE"] = _config["punt_min_edge"]
            globals()["PUNT_MIN_CONFIDENCE"] = _config["punt_min_confidence"]
            globals()["PUNT_DAILY_BUDGET_USD"] = _config["punt_daily_budget_usd"]
            _locations_str = _config["locations"]
            globals()["ACTIVE_LOCATIONS"] = [loc.strip().upper() for loc in _locations_str.split(",") if loc.strip()]

    # Default to dry-run unless --live is explicitly passed
    dry_run = not args.live

    # CLI flag overrides config: --punt-mode enables the side strategy
    if args.punt_mode:
        globals()["PUNT_MODE"] = True

    run_weather_strategy(
        dry_run=dry_run,
        positions_only=args.positions,
        show_config=args.config,
        smart_sizing=args.smart_sizing,
        use_safeguards=not args.no_safeguards,
        use_trends=not args.no_trends,
        quiet=args.quiet,
        vol_targeting=args.vol_targeting or VOL_TARGETING,
    )

    # Fallback report for automaton if the strategy returned early (no signal)
    if os.environ.get("AUTOMATON_MANAGED") and not _automaton_reported:
        print(json.dumps({"automaton": {"signals": 0, "trades_attempted": 0, "trades_executed": 0, "skip_reason": "no_signal"}}))
