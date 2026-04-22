#!/usr/bin/env python3
"""
Pull all weather events from Polymarket's Gamma API.

For every weather event we can find (resolved + open), save the event
metadata and bucket markets to data/polymarket_events.jsonl. This gives
us ground-truth outcomes for backtesting our strategy against reality.

Usage:
    python scripts/pull_weather_markets.py                  # all weather events
    python scripts/pull_weather_markets.py --closed-only    # just resolved
    python scripts/pull_weather_markets.py --limit 500      # cap total

Output: data/polymarket_events.jsonl (one JSON object per event with markets).
"""

import json
import pathlib
import sys
import time
from collections import Counter
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# Force UTF-8 on Windows
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

GAMMA_API = "https://gamma-api.polymarket.com"
OUT_FILE = pathlib.Path(__file__).parent.parent / "data" / "polymarket_events.jsonl"
OUT_FILE.parent.mkdir(exist_ok=True)


def _get(url: str, params: dict | None = None, retries: int = 3) -> Any:
    if params:
        url = f"{url}?{urlencode(params)}"
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "polymarket-weather-trader/1.0"})
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** attempt)
                continue
            print(f"  HTTP {e.code} on {url[:100]}: {e.reason}", file=sys.stderr)
            return None
        except (URLError, Exception) as e:
            if attempt == retries - 1:
                print(f"  error on {url[:100]}: {e}", file=sys.stderr)
                return None
            time.sleep(2 ** attempt)
    return None


def fetch_events(tag: str = "weather", closed: bool | None = None,
                 page_limit: int = 100, max_events: int | None = None) -> list:
    """
    Paginate /events endpoint filtered by tag.

    Gamma API filters we try in order (schema varies by version):
      tag_slug=weather, tag=weather, tags=weather
    """
    events = []
    offset = 0
    # Try multiple filter shapes; keep the one that returns results
    filter_candidates = [
        {"tag_slug": tag},
        {"tag": tag},
        {"tags": tag},
    ]
    working_filter = None

    while True:
        if max_events and len(events) >= max_events:
            break

        if working_filter is None:
            # First call — try each filter shape until one works
            page = None
            for f in filter_candidates:
                params = dict(f)
                params.update({"limit": page_limit, "offset": offset})
                if closed is not None:
                    params["closed"] = str(closed).lower()
                page = _get(f"{GAMMA_API}/events", params=params)
                if page and isinstance(page, list) and len(page) > 0:
                    working_filter = f
                    break
                if page and isinstance(page, dict) and page.get("data"):
                    working_filter = f
                    page = page["data"]
                    break
            if not working_filter:
                print(f"  No weather events returned for any filter shape. "
                      f"Gamma API may have changed — check endpoint.", file=sys.stderr)
                return events
        else:
            params = dict(working_filter)
            params.update({"limit": page_limit, "offset": offset})
            if closed is not None:
                params["closed"] = str(closed).lower()
            page = _get(f"{GAMMA_API}/events", params=params)
            if isinstance(page, dict) and page.get("data"):
                page = page["data"]
            if not page or not isinstance(page, list):
                break

        if not page:
            break

        events.extend(page)
        print(f"  page offset={offset:>5} → +{len(page)} events (total {len(events)})")
        if len(page) < page_limit:
            break
        offset += page_limit
        time.sleep(0.2)

    return events[:max_events] if max_events else events


def is_weather_event(e: dict) -> bool:
    """Defensive weather-tag check (Gamma may not filter server-side reliably)."""
    blob = " ".join(str(e.get(k, "")).lower() for k in
                    ("title", "slug", "description", "category", "subcategory"))
    tags = e.get("tags") or []
    if isinstance(tags, list):
        for t in tags:
            tag_str = str(t.get("slug") if isinstance(t, dict) else t).lower()
            if "weather" in tag_str or "temperature" in tag_str:
                return True
    return any(kw in blob for kw in (
        "temperature", "weather", "highest temp", "lowest temp",
        "°f", "°c", "fahrenheit", "celsius",
    ))


def summarize(events: list) -> None:
    if not events:
        print("\n  No events to summarize.")
        return

    resolved = [e for e in events if e.get("closed") or e.get("resolved")]
    open_ = [e for e in events if not (e.get("closed") or e.get("resolved"))]

    # Extract city from titles
    cities = Counter()
    months = Counter()
    for e in events:
        title = (e.get("title") or "").lower()
        for city in [
            "nyc", "new york", "chicago", "seattle", "atlanta", "dallas", "miami",
            "houston", "san francisco", "phoenix", "los angeles", "denver", "austin",
            "las vegas", "tel aviv", "munich", "london", "tokyo", "seoul", "ankara",
            "lucknow", "wellington", "toronto", "paris", "milan", "sao paulo", "warsaw",
            "singapore", "shanghai", "beijing", "shenzhen", "chengdu", "chongqing",
            "wuhan", "hong kong", "buenos aires",
        ]:
            if city in title:
                cities[city.title()] += 1
                break
        for m in ["january", "february", "march", "april", "may", "june",
                  "july", "august", "september", "october", "november", "december"]:
            if m in title:
                months[m.title()] += 1
                break

    total_markets = sum(len(e.get("markets") or []) for e in events)
    print(f"\n  Total events:       {len(events)}")
    print(f"  Resolved/closed:    {len(resolved)}")
    print(f"  Still open:         {len(open_)}")
    print(f"  Total sub-markets:  {total_markets}")

    print(f"\n  Top 15 cities:")
    for city, n in cities.most_common(15):
        print(f"    {city:<18s} {n:>4d} events")

    if months:
        print(f"\n  Target months covered:")
        for m, n in months.most_common():
            print(f"    {m:<12s} {n:>4d} events")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Pull Polymarket weather events")
    parser.add_argument("--closed-only", action="store_true",
                        help="Only pull resolved/closed events")
    parser.add_argument("--open-only", action="store_true",
                        help="Only pull open events")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max events to pull (default: all)")
    parser.add_argument("--tag", default="weather", help="Gamma tag filter")
    parser.add_argument("--page-limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and summarize but don't write output file")
    args = parser.parse_args()

    closed = None
    if args.closed_only:
        closed = True
    elif args.open_only:
        closed = False

    mode = "resolved" if args.closed_only else "open" if args.open_only else "all"
    print(f"Pulling {mode} weather events from Gamma API (tag={args.tag})...")
    events = fetch_events(
        tag=args.tag, closed=closed,
        page_limit=args.page_limit, max_events=args.limit,
    )

    # Defensive filter — some results may not actually be weather
    weather = [e for e in events if is_weather_event(e)]
    non_weather = len(events) - len(weather)
    if non_weather > 0:
        print(f"  Filtered out {non_weather} non-weather events")

    summarize(weather)

    if args.dry_run:
        print(f"\n  --dry-run: not writing output")
        return

    with OUT_FILE.open("w") as f:
        for e in weather:
            f.write(json.dumps(e, default=str) + "\n")
    print(f"\n  Saved {len(weather)} events → {OUT_FILE}")


if __name__ == "__main__":
    main()
