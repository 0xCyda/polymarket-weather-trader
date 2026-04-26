#!/usr/bin/env python3
"""
Paper Trading Journal — Polymarket Weather

Tracks paper trades locally, fetches resolution from Polymarket API,
and computes win/loss P&L without needing Simmer balance.

Usage:
    from paper_journal import log_paper_trade, get_open_positions, get_resolved_trades, get_stats
"""

import json
import os
import sys
import pathlib
from datetime import datetime, timezone
from pathlib import Path

# Loss log — one JSON line per losing trade, written to losses.log in the journal dir
_LOSSES_LOG: Path | None = None

def _losses_log_path() -> Path:
    global _LOSSES_LOG
    if _LOSSES_LOG is None:
        # Co-located with paper_trades.jsonl under data/ for ops consistency
        _LOSSES_LOG = Path(__file__).parent.parent / "data" / "losses.log"
        _LOSSES_LOG.parent.mkdir(exist_ok=True)
    return _LOSSES_LOG


def log_loss(trade: dict) -> None:
    """Append a losing trade to losses.log with full signal + execution context."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "trade_id": trade.get("trade_id"),
        "market_id": trade.get("market_id"),
        "question": trade.get("question"),
        "location": trade.get("location"),
        "target_date": trade.get("target_date"),
        "metric": trade.get("metric"),
        "bucket": trade.get("bucket"),
        "side": trade.get("side"),
        "strategy": trade.get("strategy", "core"),
        "signal_strength": trade.get("signal_strength"),
        "entry_price": trade.get("entry_price"),
        "exit_price": trade.get("exit_price"),
        "shares": trade.get("shares"),
        "cost": trade.get("cost"),
        "pnl": trade.get("pnl"),
        "forecast_temp": trade.get("forecast_temp"),
        "model_temps": trade.get("model_temps"),
        "models_used": trade.get("models_used"),
        "agreement_pct": trade.get("agreement_pct"),
        "spread": trade.get("spread"),
        "actual_temp": trade.get("actual_temp"),
        "outcome": trade.get("outcome"),
        "resolved_at": trade.get("resolved_at"),
        "entered_at": trade.get("entered_at"),
    }
    try:
        with open(_losses_log_path(), "a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # Never let logging break trade resolution
import subprocess
import requests
from datetime import datetime, timezone

# Force line-buffered stdout
sys.stdout.reconfigure(line_buffering=True)

JOURNAL_DIR = pathlib.Path(__file__).parent.parent / "data"
JOURNAL_FILE = JOURNAL_DIR / "paper_trades.jsonl"
JOURNAL_FILE.parent.mkdir(exist_ok=True)


def _load_trades() -> list:
    """Load all trades from JSONL, oldest first."""
    if not JOURNAL_FILE.exists():
        return []
    trades = []
    for line in JOURNAL_FILE.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"Warning: skipping corrupt trade line: {line[:80]}", file=sys.stderr)
    return trades


_LOCK_FILE = JOURNAL_FILE.with_suffix(".lock")


def _save_trades(trades: list) -> None:
    """
    Atomically rewrite JSONL with all trades.

    Uses a lockfile to prevent concurrent processes (core + late) from
    clobbering each other's writes. The lock is advisory (fcntl on POSIX,
    msvcrt on Windows). Falls back to unlocked write if locking unavailable.
    """
    import contextlib

    @contextlib.contextmanager
    def _file_lock():
        lock_fd = None
        try:
            lock_fd = open(_LOCK_FILE, "w")
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except (ImportError, OSError):
                try:
                    import msvcrt
                    msvcrt.locking(lock_fd.fileno(), msvcrt.LK_LOCK, 1)
                except (ImportError, OSError):
                    pass
            yield
        finally:
            if lock_fd:
                try:
                    lock_fd.close()
                except Exception:
                    pass

    tmp = JOURNAL_FILE.with_suffix(JOURNAL_FILE.suffix + ".tmp")
    payload = "\n".join(json.dumps(t, default=str) for t in trades) + "\n"
    with _file_lock():
        tmp.write_text(payload)
        os.replace(tmp, JOURNAL_FILE)


def log_paper_trade(
    market_id: str,
    question: str,
    side: str,          # "yes" or "no"
    entry_price: float,
    shares: float,
    cost: float,
    bucket: str,
    forecast_temp: float,
    signal_strength: str,
    location: str,
    date_str: str,
    metric: str,
    models_used: int,
    agreement_pct: float,
    spread: float,
    strategy: str = "core",   # "core" (default) or "punt"
    model_temps: dict | None = None,  # {model_name: temp} for all models in ensemble
    confidence: float | None = None,  # numeric confidence at entry (for calibration)
    polymarket_token_id: str | None = None,   # CLOB YES token; lets dashboard skip Simmer for live prices
    polymarket_no_token_id: str | None = None,
) -> str:
    """
    Log a new paper trade. Returns the trade_id.
    """
    trade_id = f"paper_{market_id[:16]}_{int(datetime.now(timezone.utc).timestamp())}"
    trade = {
        "trade_id": trade_id,
        "market_id": market_id,
        "question": question,
        "side": side,
        "entry_price": entry_price,
        "shares": shares,
        "cost": cost,
        "bucket": bucket,
        "forecast_temp": forecast_temp,
        "signal_strength": signal_strength,
        "strategy": strategy,
        "location": location,
        "target_date": date_str,
        "metric": metric,
        "models_used": models_used,
        "agreement_pct": agreement_pct,
        "spread": spread,
        "model_temps": model_temps,
        "confidence": confidence,
        "polymarket_token_id": polymarket_token_id,
        "polymarket_no_token_id": polymarket_no_token_id,
        "status": "open",
        "outcome": None,
        "exit_price": None,
        "pnl": None,
        "resolved_at": None,
        "entered_at": datetime.now(timezone.utc).isoformat(),
    }
    # Hard dedup: prevent same market_id from being logged twice (race condition guard)
    trades = _load_trades()
    for existing in trades:
        if existing.get("market_id") == market_id and existing.get("status") == "open":
            print(f"Warning: open position already exists for market {market_id[:16]} — skipping duplicate log")
            return existing.get("trade_id", "")
    trades.append(trade)
    _save_trades(trades)
    return trade_id


def _fetch_market_resolution(market_id: str) -> dict | None:
    """Fetch resolution state from Simmer API by market ID (clob.polymarket.com is 403-blocked)."""
    try:
        api_key = os.environ.get("SIMMER_API_KEY")
        if not api_key:
            return None
        resp = requests.get(
            f"https://api.simmer.markets/api/sdk/context/{market_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        m = data.get("market", {}) if isinstance(data, dict) else {}
        if not m:
            return None
        resolved = m.get("status") == "resolved"
        outcome = m.get("outcome")
        # Simmer often leaves outcome=None even for resolved markets.
        # Infer from final settlement price: <0.05 = NO, >0.95 = YES.
        # Mid-range (0.05–0.95) stays None so historical fallback can settle.
        if outcome is None and resolved:
            price = m.get("current_price", 0.5)
            if isinstance(price, (int, float)):
                if price < 0.05:
                    outcome = "no"
                elif price > 0.95:
                    outcome = "yes"
        return {
            "resolved": resolved,
            "outcome": outcome,
            "end_date_utc": m.get("resolves_at", ""),
            "question": m.get("question", ""),
        }
    except Exception:
        return None


# =============================================================================
# Open-Meteo historical archive fallback — settles trades when Simmer API
# returns resolved=true but outcome=None, or when target_date has passed and
# Simmer never confirms resolution. Lets us decide YES/NO ourselves.
# =============================================================================

# Location lat/lon for 34 supported cities — mirrored from ensemble_forecast.py
# Kept here to avoid circular imports. Keep in sync if locations are added.
_HISTORICAL_LOCATIONS = {
    "NYC": (40.7769, -73.8740, "America/New_York"),
    "Chicago": (41.9742, -87.9073, "America/Chicago"),
    "Seattle": (47.4502, -122.3088, "America/Los_Angeles"),
    "Atlanta": (33.6407, -84.4277, "America/New_York"),
    "Dallas": (32.8998, -97.0403, "America/Chicago"),
    "Miami": (25.7959, -80.2870, "America/New_York"),
    "Houston": (29.9902, -95.3368, "America/Chicago"),
    "San Francisco": (37.6213, -122.3790, "America/Los_Angeles"),
    "Phoenix": (33.4373, -112.0078, "America/Phoenix"),
    "Los Angeles": (33.9425, -118.4081, "America/Los_Angeles"),
    "Denver": (39.8617, -104.6732, "America/Denver"),
    "Austin": (30.1945, -97.6699, "America/Chicago"),
    "Las Vegas": (36.0840, -115.1537, "America/Los_Angeles"),
    "Tel Aviv": (32.0853, 34.7818, "Asia/Jerusalem"),
    "Munich": (48.1351, 11.5820, "Europe/Berlin"),
    "London": (51.5074, -0.1278, "Europe/London"),
    "Tokyo": (35.6762, 139.6503, "Asia/Tokyo"),
    "Seoul": (37.5665, 126.9780, "Asia/Seoul"),
    "Ankara": (39.9334, 32.8597, "Europe/Istanbul"),
    "Lucknow": (26.8467, 80.9462, "Asia/Kolkata"),
    "Wellington": (-41.2866, 174.7756, "Pacific/Auckland"),
    "Toronto": (43.6777, -79.6248, "America/Toronto"),
    "Paris": (48.8566, 2.3522, "Europe/Paris"),
    "Milan": (45.4642, 9.1900, "Europe/Rome"),
    "Sao Paulo": (-23.5505, -46.6333, "America/Sao_Paulo"),
    "Warsaw": (52.2297, 21.0122, "Europe/Warsaw"),
    "Singapore": (1.3521, 103.8198, "Asia/Singapore"),
    "Shanghai": (31.2304, 121.4737, "Asia/Shanghai"),
    "Beijing": (39.9042, 116.4074, "Asia/Shanghai"),
    "Shenzhen": (22.5431, 114.0579, "Asia/Shanghai"),
    "Chengdu": (30.5728, 104.0668, "Asia/Shanghai"),
    "Chongqing": (29.4316, 106.9123, "Asia/Shanghai"),
    "Wuhan": (30.5928, 114.3055, "Asia/Shanghai"),
    "Hong Kong": (22.3193, 114.1694, "Asia/Hong_Kong"),
    "Buenos Aires": (-34.6037, -58.3816, "America/Argentina/Buenos_Aires"),
}


# Polymarket/Gamma-only actual-temperature resolution.
# The actual high/low for a trade is derived from which bucket resolved YES
# in the corresponding Polymarket event. Local cache at
# data/polymarket_events.jsonl is consulted first; live Gamma API is used as
# fallback when the cache is stale or missing the event.
import re as _re

_GAMMA_EVENTS_ENDPOINT = "https://gamma-api.polymarket.com/events"
_EVENTS_CACHE_FILE = pathlib.Path(__file__).resolve().parent.parent / "data" / "polymarket_events.jsonl"

_POLYMARKET_CITY_ALIASES = {
    # location-name (lowercased) -> slug-city used by Polymarket
    "nyc": "nyc",
    "new york": "nyc",
    "new york city": "nyc",
    "hong kong": "hong-kong",
    "los angeles": "los-angeles",
    "san francisco": "san-francisco",
    "buenos aires": "buenos-aires",
    "kuala lumpur": "kuala-lumpur",
    "cape town": "cape-town",
    "sao paulo": "sao-paulo",
    "new delhi": "new-delhi",
    "rio de janeiro": "rio-de-janeiro",
    "tel aviv": "tel-aviv",
    "mexico city": "mexico-city",
}

_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

_events_index_cache: dict | None = None


def _location_to_slug_cities(location: str) -> list[str]:
    """Return candidate slug-city strings for a given location name."""
    lo = (location or "").strip().lower()
    out: list[str] = []
    if lo in _POLYMARKET_CITY_ALIASES:
        out.append(_POLYMARKET_CITY_ALIASES[lo])
    generic = _re.sub(r"[^\w]+", "-", lo).strip("-")
    if generic and generic not in out:
        out.append(generic)
    return out


def _load_events_index() -> dict:
    """Build {(slug_city, YYYY-MM-DD, metric): markets[]} from local cache."""
    global _events_index_cache
    if _events_index_cache is not None:
        return _events_index_cache
    idx: dict = {}
    if _EVENTS_CACHE_FILE.exists():
        slug_re = _re.compile(
            r"^(highest|lowest)-temperature-in-(.+?)-on-([a-z]+)-(\d+)(?:-(\d{4}))?$"
        )
        months = {n: i + 1 for i, n in enumerate(_MONTH_NAMES)}
        months.update({n[:3]: i + 1 for i, n in enumerate(_MONTH_NAMES)})
        for line in _EVENTS_CACHE_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = slug_re.match(ev.get("slug", ""))
            if not m:
                continue
            mo = months.get(m.group(3))
            if not mo:
                continue
            year = int(m.group(5)) if m.group(5) else 2026
            date = f"{year:04d}-{mo:02d}-{int(m.group(4)):02d}"
            metric = "high" if m.group(1) == "highest" else "low"
            idx[(m.group(2), date, metric)] = ev.get("markets", [])
    _events_index_cache = idx
    return idx


def _parse_outcome_prices(raw) -> list | None:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def _find_yes_bucket_label(markets: list) -> str | None:
    """Return the groupItemTitle of the bucket resolved YES (or proposed ≥0.99)."""
    for m in markets or []:
        prices = _parse_outcome_prices(m.get("outcomePrices"))
        if not prices or len(prices) < 2:
            continue
        try:
            yes_price = float(prices[0])
        except (TypeError, ValueError):
            continue
        if yes_price >= 0.99:
            return m.get("groupItemTitle")
    return None


def _bucket_label_to_temp_f(label: str) -> float | None:
    """Parse a Polymarket bucket label (e.g. '28°C', '58-59°F', '21°C or below')
    into a representative temperature in °F."""
    if not label:
        return None
    s = label.strip()
    unit = "C" if _re.search(r"°?\s*C\b", s, _re.IGNORECASE) else "F"

    def to_f(v: float) -> float:
        return v * 9 / 5 + 32 if unit == "C" else v

    m = _re.match(
        r"(-?\d+(?:\.\d+)?)\s*°?[FC]?\s*(?:-|–|to)\s*(-?\d+(?:\.\d+)?)",
        s, _re.IGNORECASE,
    )
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return round(to_f((lo + hi) / 2), 1)
    m = _re.match(
        r"(-?\d+(?:\.\d+)?)\s*°?[FC]?\s*(?:or\s+)?(?:below|less|lower)",
        s, _re.IGNORECASE,
    )
    if m:
        return round(to_f(float(m.group(1))), 1)
    m = _re.match(
        r"(-?\d+(?:\.\d+)?)\s*°?[FC]?\s*(?:or\s+)?(?:above|higher|more)",
        s, _re.IGNORECASE,
    )
    if m:
        return round(to_f(float(m.group(1))), 1)
    m = _re.match(r"(-?\d+(?:\.\d+)?)", s)
    if m:
        return round(to_f(float(m.group(1))), 1)
    return None


def _fetch_polymarket_event_live(slug_city: str, date_str: str, metric: str) -> list | None:
    """Fetch an event's bucket markets via live Gamma API by exact slug."""
    try:
        year, mo, day = date_str.split("-")
        month_name = _MONTH_NAMES[int(mo) - 1]
        prefix = "highest" if metric == "high" else "lowest"
        slug = f"{prefix}-temperature-in-{slug_city}-on-{month_name}-{int(day)}-{year}"
        resp = requests.get(_GAMMA_EVENTS_ENDPOINT, params={"slug": slug}, timeout=15)
        if resp.status_code != 200:
            return None
        events = resp.json()
        if not events:
            return None
        return events[0].get("markets", [])
    except Exception:
        return None


def fetch_historical_temp(location: str, date_str: str, metric: str, unit: str = "F") -> float | None:
    """Return the Polymarket-resolved actual temperature for a past date.

    Source of truth is the Polymarket event bucket that resolved YES. Reads
    the local cache at data/polymarket_events.jsonl first; falls back to a
    live Gamma API lookup if the cached event is missing or not yet resolved.
    Returns None if no resolution is available from Polymarket.
    """
    candidates = _location_to_slug_cities(location)
    idx = _load_events_index()

    # 1) Local cache — only accept if a YES bucket is present (≥0.99).
    for slug_city in candidates:
        label = _find_yes_bucket_label(idx.get((slug_city, date_str, metric), []))
        if label:
            val_f = _bucket_label_to_temp_f(label)
            if val_f is not None:
                return round(val_f, 1) if unit == "F" else round((val_f - 32) * 5 / 9, 1)

    # 2) Live Gamma fallback (cache stale or event missing).
    for slug_city in candidates:
        markets = _fetch_polymarket_event_live(slug_city, date_str, metric)
        label = _find_yes_bucket_label(markets) if markets else None
        if label:
            val_f = _bucket_label_to_temp_f(label)
            if val_f is not None:
                return round(val_f, 1) if unit == "F" else round((val_f - 32) * 5 / 9, 1)

    return None


def _parse_bucket_range(bucket_str: str) -> tuple | None:
    """
    Parse a bucket string into (lo, hi, unit) in Fahrenheit.
    Duplicates parse_temperature_bucket from weather_trader.py to keep
    paper_journal.py self-contained (avoids import cycle).

    Returns (lo_f, hi_f, unit) or None. -999/999 are open-ended sentinels.
    """
    import re
    if not bucket_str:
        return None
    unit = 'C' if re.search(r'°C', bucket_str, re.IGNORECASE) else 'F'

    def _to_f(lo, hi):
        if unit == 'C':
            lo = lo * 9 / 5 + 32 if lo != -999 else -999
            hi = hi * 9 / 5 + 32 if hi != 999 else 999
        return (lo, hi)

    m = re.search(r'(-?\d+)\s*°?[fFcC]?\s*(or below|or less)', bucket_str, re.IGNORECASE)
    if m:
        lo, hi = _to_f(-999, int(m.group(1)))
        return (lo, hi, 'F')
    m = re.search(r'(-?\d+)\s*°?[fFcC]?\s*(or higher|or above|or more)', bucket_str, re.IGNORECASE)
    if m:
        lo, hi = _to_f(int(m.group(1)), 999)
        return (lo, hi, 'F')
    m = re.search(r'(-?\d+)\s*(?:°?\s*[fFcC])?\s*(?:-|–|to)\s*(-?\d+)', bucket_str)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        lo, hi = _to_f(min(a, b), max(a, b))
        return (lo, hi, 'F')
    m = re.search(r'(-?\d+)\s*°[fFcC]', bucket_str)
    if m:
        t = int(m.group(1))
        lo, hi = _to_f(t, t)
        return (lo, hi, 'F')
    m = re.match(r'^\s*(-?\d+)\s*°?[cCfF]?\s*$', bucket_str.strip())
    if m:
        t = int(m.group(1))
        lo, hi = _to_f(t, t)
        return (lo, hi, 'F')
    return None


# Days past target_date before we fall back to historical-temp settlement
_FALLBACK_DAYS_PAST = 1


def _fetch_actual_temp_via_polymarket_cli(location: str, date_str: str, metric: str) -> float | None:
    """
    Use `polymarket markets search` to find all bucket markets for a city/date,
    identify the winning bucket (highest lastTradePrice), and extract the actual
    temperature in Fahrenheit. This is the authoritative resolution source —
    Polymarket itself — before falling back to Open-Meteo which may differ.

    Returns temperature in F, or None if the CLI is unavailable or fails.
    """
    city_part = location.lower().replace(" ", "")
    try:
        year, mo, day = date_str.split("-")
    except ValueError:
        return None
    month_name = ["january","february","march","april","may","june",
                  "july","august","september","october","november","december"][int(mo)-1]
    search_query = f"{city_part} {month_name} {int(day)}"

    try:
        r = subprocess.run(
            ["polymarket", "markets", "search", search_query, "--output", "json"],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode != 0:
            return None
        raw = r.stdout.strip()
        if not raw:
            return None
        results = json.loads(raw)
        if isinstance(results, dict):
            results = results.get("results", [])
    except Exception:
        return None

    if not results:
        return None

    # Find the bucket with the highest lastTradePrice — that's the winning bucket
    best_price = -1.0
    best_market = None
    for m in results:
        try:
            price = float(m.get("lastTradePrice") or 0)
        except (TypeError, ValueError):
            continue
        if price > best_price:
            best_price = price
            best_market = m

    if not best_market or best_price < 0.1:
        return None

    # Parse temperature from the winning bucket's question
    question = best_market.get("question", "")
    m = _re.search(r"be\s+(-?\d+(?:\.\d+)?)\s*°?\s*([CF])", question, _re.IGNORECASE)
    if m:
        temp = float(m.group(1))
        unit = m.group(2).upper()
        if unit == "C":
            return round(temp * 9.0 / 5.0 + 32.0, 1)
        return round(temp, 1)

    # Fallback: parse groupItemTitle (e.g. "14°C")
    label = best_market.get("groupItemTitle", "")
    m = _re.search(r"(-?\d+(?:\.\d+)?)\s*°\s*([CF])", label, _re.IGNORECASE)
    if m:
        temp = float(m.group(1))
        unit = m.group(2).upper()
        if unit == "C":
            return round(temp * 9.0 / 5.0 + 32.0, 1)
        return round(temp, 1)

    return None


def _historical_fallback_settlement(trade: dict, force: bool = False) -> dict | None:
    """
    Settle a stuck-open trade by checking the actual observed temperature.
    Resolution chain:
      1. Simmer API (caller does this first)
      2. Local events cache
      3. Live Gamma API
      4. Polymarket CLI search (authoritative — this is Polymarket's own data)
      5. Open-Meteo archive (last resort)

    Returns {"outcome": "yes"/"no", "exit_price": 0.0|1.0, "actual_temp": float,
    "source": "..."} or None if we can't determine.

    force=True bypasses the _FALLBACK_DAYS_PAST guard.
    """
    target_date = trade.get("target_date")
    location = trade.get("location")
    metric = trade.get("metric", "high")
    bucket = trade.get("bucket") or trade.get("question", "")
    if not target_date or not location or not bucket:
        return None

    # Check target_date is at least _FALLBACK_DAYS_PAST old (UTC)
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if not force and (datetime.now(timezone.utc) - target).days < _FALLBACK_DAYS_PAST:
        return None

    # Step 1-3: local cache + Gamma API
    actual = fetch_historical_temp(location, target_date, metric, unit="F")
    source = "polymarket_cache"

    # Step 4: Polymarket CLI (authoritative — before Open-Meteo)
    if actual is None:
        actual = _fetch_actual_temp_via_polymarket_cli(location, target_date, metric)
        source = "polymarket_cli"

    # Step 5: Open-Meteo archive (last resort)
    if actual is None:
        return None

    bucket_range = _parse_bucket_range(bucket)
    if not bucket_range:
        return None
    lo_f, hi_f, _ = bucket_range

    yes_won = (lo_f <= actual <= hi_f)
    return {
        "outcome": "yes" if yes_won else "no",
        "exit_price": 1.0 if yes_won else 0.0,
        "actual_temp": actual,
        "source": source,
    }


def _compute_pnl(side: str, entry: float, exit_price: float, shares: float) -> float:
    """
    Compute realized P&L for a paper trade.

    exit_price is the YES token settlement price (0 or 1 at resolution).
    - YES position payoff at settlement: exit_price  →  P&L = (exit_price - entry) * shares
    - NO  position payoff at settlement: (1 - exit_price)
      Entry paid for NO tokens was `entry` per share.
      P&L = ((1 - exit_price) - entry) * shares
    """
    shares = float(shares or 0)
    entry = float(entry or 0)
    exit_price = float(exit_price or 0)
    if (side or "yes").lower() == "yes":
        return (exit_price - entry) * shares
    return ((1.0 - exit_price) - entry) * shares


def update_resolved_trades() -> list:
    """
    Check all open paper trades. Settlement flow:
      1. Query Simmer API for resolution status + outcome.
      2. If Simmer says resolved AND outcome surfaced → settle.
      3. If Simmer says resolved but outcome=None, OR target_date is >2 days
         past with no resolution, → fall back to Open-Meteo archive historical
         temperature and settle ourselves.

    Returns list of newly resolved trades.
    """
    trades = _load_trades()
    newly_resolved = []

    for trade in trades:
        if trade.get("status") == "resolved":
            continue

        market_id = trade.get("market_id")
        resolution = _fetch_market_resolution(market_id) if market_id else None

        outcome = None
        exit_price = None
        source = None

        # Path 1: Simmer reports resolved + outcome
        if resolution and resolution.get("resolved") and resolution.get("outcome"):
            outcome = resolution["outcome"]
            exit_price = 1.0 if outcome.lower() in ("yes", "true") else 0.0
            source = "simmer"
            # Fetch and persist actual temp once at resolution time (not on every dashboard load)
            if trade.get("actual_temp") is None:
                try:
                    loc = trade.get("location", "")
                    date_str = trade.get("target_date") or trade.get("resolution_date", "")[:10]
                    metric = trade.get("metric", "high")
                    if loc and date_str:
                        actual = fetch_historical_temp(loc, date_str, metric, unit="F")
                        if actual is not None:
                            trade["actual_temp"] = actual
                except Exception:
                    pass
        else:
            # Path 2: historical fallback (Simmer outcome=None OR just expired).
            # Pass force=True when Simmer already confirmed resolved — no point
            # waiting _FALLBACK_DAYS_PAST extra days for Open-Meteo data.
            simmer_resolved = bool(resolution and resolution.get("resolved"))
            fb = _historical_fallback_settlement(trade, force=simmer_resolved)
            if fb:
                outcome = fb["outcome"]
                exit_price = fb["exit_price"]
                source = fb["source"]
                trade["actual_temp"] = fb["actual_temp"]

        if outcome is None or exit_price is None:
            continue  # Not ready yet

        side = trade.get("side", "yes")
        entry = trade.get("entry_price", 0)
        shares = trade.get("shares", 0)
        pnl = _compute_pnl(side, entry, exit_price, shares)

        old_status = trade.get("status")
        trade["status"] = "resolved"
        trade["outcome"] = outcome
        trade["exit_price"] = exit_price
        trade["pnl"] = round(pnl, 4)
        trade["resolved_at"] = datetime.now(timezone.utc).isoformat()
        trade["resolution_date"] = (
            (resolution or {}).get("end_date_utc", "")[:10] if resolution else trade.get("target_date", "")
        )
        trade["resolution_source"] = source

        # Write to losses.log if this was a losing trade
        if pnl < 0:
            log_loss(trade)

        if old_status == "open":
            newly_resolved.append(trade)

    _save_trades(trades)
    return newly_resolved


def manual_resolve(trade_id: str, outcome: str) -> dict | None:
    """
    Manually resolve a specific trade. outcome must be "yes" or "no".
    Returns the updated trade dict or None if not found.
    """
    outcome = (outcome or "").lower().strip()
    if outcome not in ("yes", "no"):
        raise ValueError("outcome must be 'yes' or 'no'")
    trades = _load_trades()
    target = None
    for t in trades:
        if t.get("trade_id") == trade_id:
            target = t
            break
    if target is None:
        return None
    exit_price = 1.0 if outcome == "yes" else 0.0
    target["status"] = "resolved"
    target["outcome"] = outcome
    target["exit_price"] = exit_price
    target["pnl"] = round(_compute_pnl(
        target.get("side", "yes"), target.get("entry_price", 0),
        exit_price, target.get("shares", 0)
    ), 4)
    target["resolved_at"] = datetime.now(timezone.utc).isoformat()
    target["resolution_source"] = "manual"
    # Populate actual_temp at resolution time so the dashboard / loss log
    # never show blanks. Best-effort — failure here doesn't block resolution.
    if target.get("actual_temp") is None:
        try:
            actual = fetch_historical_temp(
                target.get("location", ""), target.get("target_date", ""),
                target.get("metric", "high"), unit="F",
            )
            if actual is not None:
                target["actual_temp"] = actual
        except Exception:
            pass
    if target.get("pnl", 0) < 0:
        log_loss(target)
    _save_trades(trades)
    return target


def backfill_actual_temps() -> list:
    """Walk the journal, fill in actual_temp on resolved rows that lack it.

    Useful when:
      * manual_resolve was called before this function existed
      * Simmer-path resolution succeeded but fetch_historical_temp failed at
        the time (Polymarket / Gamma not yet indexed, transient errors)

    Returns the list of trades that got patched.
    """
    trades = _load_trades()
    patched = []
    for t in trades:
        if t.get("status") != "resolved":
            continue
        if t.get("actual_temp") is not None:
            continue
        loc = t.get("location") or ""
        date = t.get("target_date") or ""
        metric = t.get("metric", "high")
        actual = None
        try:
            actual = fetch_historical_temp(loc, date, metric, unit="F")
        except Exception:
            actual = None
        if actual is None:
            try:
                fb = _historical_fallback_settlement(t, force=True)
                if fb:
                    actual = fb.get("actual_temp")
            except Exception:
                pass
        if actual is not None:
            t["actual_temp"] = actual
            patched.append(t)
    if patched:
        _save_trades(trades)
    return patched


def get_open_positions() -> list:
    """Return all open paper trades without live CLOB prices (prices require CLOB lookup per-trade)."""
    trades = _load_trades()
    return [t for t in trades if t.get("status") == "open"]


def get_open_positions_by_event() -> dict:
    """
    Return open positions keyed by (location, date_str, metric) tuple.
    Values: {"side": str, "market_id": str, "bucket": str, "entry_price": float}.
    Used to detect opposing same-event positions before executing a new trade.
    """
    positions = get_open_positions()
    by_event = {}
    for t in positions:
        key = (t.get("location", ""), t.get("target_date", ""), t.get("metric", ""))
        if key not in by_event:
            by_event[key] = {
                "side": t.get("side", "yes"),
                "market_id": t.get("market_id", ""),
                "bucket": t.get("bucket", ""),
                "entry_price": t.get("entry_price", 0),
                "cost": t.get("cost", 0),
            }
    return by_event


def get_resolved_trades(limit: int = 20) -> list:
    """Return most recent resolved trades."""
    trades = _load_trades()
    resolved = [t for t in trades if t.get("status") == "resolved"]
    resolved.sort(key=lambda t: t.get("resolved_at", ""), reverse=True)
    return resolved[:limit]


def get_stats() -> dict:
    """Compute aggregate paper trading stats."""
    trades = _load_trades()
    resolved = [t for t in trades if t.get("status") == "resolved"]
    open_trades = [t for t in trades if t.get("status") == "open"]

    if not resolved:
        return {
            "total_trades": len(trades),
            "open_trades": len(open_trades),
            "resolved_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "best_trade": None,
            "worst_trade": None,
        }

    wins = [t for t in resolved if t.get("pnl", 0) > 0]
    losses = [t for t in resolved if t.get("pnl", 0) < 0]
    pnls = [t.get("pnl", 0) for t in resolved]

    return {
        "total_trades": len(trades),
        "open_trades": len(open_trades),
        "resolved_trades": len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / (len(wins) + len(losses)) * 100, 1) if (wins or losses) else None,
        "total_pnl": round(sum(pnls), 4),
        "avg_pnl": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
        "best_trade": max(pnls) if pnls else None,
        "worst_trade": min(pnls) if pnls else None,
    }


def print_summary() -> None:
    """Print a human-readable summary to stdout."""
    stats = get_stats()
    open_pos = get_open_positions()

    print("\n📓 Paper Trading Journal — Polymarket Weather")
    print("=" * 50)
    print(f"  Total trades:  {stats['total_trades']}")
    print(f"  Open:          {stats['open_trades']}")
    print(f"  Resolved:      {stats['resolved_trades']}")

    if stats["resolved_trades"] > 0:
        print(f"\n  Win rate:      {stats['win_rate']}%")
        print(f"  Total P&L:    ${stats['total_pnl']:.4f}")
        print(f"  Avg P&L:      ${stats['avg_pnl']:.4f}")
        print(f"  Best trade:   ${stats['best_trade']:.4f}" if stats['best_trade'] is not None else "  Best trade:    —")
        print(f"  Worst trade:  ${stats['worst_trade']:.4f}" if stats['worst_trade'] is not None else "  Worst trade:   —")
    else:
        print(f"\n  No resolved trades yet.")

    if open_pos:
        print(f"\n  Open positions ({len(open_pos)}):")
        for t in open_pos:
            print(f"  • {t.get('location', '?')} {t.get('target_date', '')} {t.get('metric', '')} — {t.get('side', '?').upper()} | {t.get('shares', 0):.1f} shares @ ${t.get('entry_price', 0):.4f}")

    print()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Paper journal — summary, manual resolve, backfill")
    parser.add_argument("--resolve", metavar="TRADE_ID", help="Manually resolve a trade by trade_id")
    parser.add_argument("--outcome", choices=["yes", "no"], help="Outcome for --resolve (yes|no)")
    parser.add_argument("--backfill", action="store_true",
                        help="Run update_resolved_trades() to settle any resolvable open trades (incl. historical fallback)")
    parser.add_argument("--list-open", action="store_true", help="List open trade_ids + questions")
    args = parser.parse_args()

    if args.list_open:
        for t in get_open_positions():
            q = (t.get("question") or "")[:60]
            print(f"{t.get('trade_id', '?')}  |  {t.get('location', '?')}  {t.get('target_date', '')}  |  {q}")
    elif args.resolve:
        if not args.outcome:
            print("Error: --outcome yes|no is required with --resolve", file=sys.stderr)
            sys.exit(2)
        updated = manual_resolve(args.resolve, args.outcome)
        if updated is None:
            print(f"Trade {args.resolve} not found.", file=sys.stderr)
            sys.exit(1)
        print(f"Resolved {args.resolve} as {args.outcome.upper()}: exit=${updated['exit_price']:.2f} "
              f"pnl=${updated['pnl']:.4f}")
    elif args.backfill:
        newly = update_resolved_trades()
        print(f"Settled {len(newly)} trade(s):")
        for t in newly:
            src = t.get("resolution_source", "?")
            print(f"  • {t.get('location')} {t.get('target_date')} {t.get('outcome', '?').upper()} "
                  f"(source={src}) pnl=${t.get('pnl', 0):.4f}")
        if not newly:
            print("  (none ready yet)")
    else:
        print_summary()
