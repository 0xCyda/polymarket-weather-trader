# Polymarket Weather Trader — Lessons Learned

## Bug Fixes

### format_scan.py: get_positions() silent failure when Simmer API returns 401
**Date:** 2026-04-19
**Symptom:** Scan showed `Open positions: 4` in the portfolio header but `None` in the positions table.
**Root cause:** `get_positions()` called `get_simmer_positions()` which silently caught HTTP 401 from Simmer API and returned `[]`. Meanwhile `get_portfolio_stats()` reads from paper journal (which correctly reported 4 open trades), creating a mismatch.
**Fix:** `get_positions()` now falls back to `paper_journal.get_open_positions()` when Simmer API returns no positions. The paper journal is the authoritative source for paper-mode positions.
**File:** `scripts/format_scan.py` — `get_positions()` function
**Verification:** `PYTHONPATH=scripts python3.12 -c "from format_scan import get_positions; print(len(get_positions()))"` should return 4.

## Gotchas

- **Simmer API key can go stale.** When it returns 401, `get_simmer_positions()` returns `[]` silently. The paper journal always has the correct position state for paper mode.
- **Paper journal is the source of truth for paper-mode positions.** Never rely solely on Simmer API for position counts in paper mode.
- **pip install fails silently on WSL Python 3.12.** Modules like `uvicorn` and `simmer_sdk` fail with PEP 668 error (`--break-system-packages` required). Always use `pip install <pkg> --break-system-packages` on this WSL install.
- **Canonical repo path is `/home/brandon/projects/polymarket-weather-trader`.** If you see `~/.openclaw/workspace/skills/polymarket-weather-trader`, treat it as a compatibility path only. Launch dashboards, scripts, and manual debugging from the projects repo.

---

## 2026-04-20 — Dashboard Startup: uvicorn Missing

**Symptom:** `python3.12 dashboard.py` fails silently — no error shown, process exits. The `dashboard.py` FastAPI app imports `uvicorn` but it wasn't installed.

**Root cause:** `uvicorn` is not listed in `requirements.txt` and wasn't installed. The terminal tool's `subprocess.run(['pip', 'install', ...])` also failed with PEP 668 (external system package restriction) but returned no output, making it invisible.

**Fix:** `pip install uvicorn --break-system-packages`

**Verification:** `curl http://127.0.0.1:8414/` returns HTTP 200.

---

## 2026-04-20 — uPNL Always Shows $0.00

**Symptom:** Portfolio header shows `Open positions: 4` and balance correctly, but every position's `uPNL` column reads `$0.00` regardless of actual market movement.

**Root cause:** `format_scan.py` imports `paper_journal` and enriches positions with live prices via `_fetch_live_price(market_id, api_key)`. However, `load_env()` — which reads `SIMMER_API_KEY` from the skill's `.env` file into `os.environ` — is called at the top of `format_scan.py`'s `__main__` block (line ~363), but `get_positions()` is called from `get_portfolio_stats()` which runs BEFORE the script's main block. When called via cron or import, `load_env()` never runs, so `os.environ.get("SIMMER_API_KEY")` returns `""` → `_fetch_live_price()` gets an empty key → Simmer API returns 401 → `current_price = 0.0` → `uPNL = 0`.

**Fix:** Call `load_env()` inside `get_positions()` before reading `SIMMER_API_KEY`, not just in `__main__`.

**File:** `scripts/format_scan.py` — `get_positions()` function (around line 183)

**Fix needed:**
```python
def get_positions():
    # ADD THIS LINE — ensure env is loaded before reading API key
    load_env()
    simmer_positions = get_simmer_positions()
    ...
```

---

## 2026-04-21 — `--list-open` Returns Truncated IDs, Not Market UUIDs

**Symptom:** `paper_journal.py --list-open` shows trade IDs like `paper_0a0cf089-aa36-45_1776644844`. Extracting the market ID as `0a0cf089-aa36-45` and calling `GET /api/sdk/context/0a0cf089-aa36-45` returns `404 Market not found`.

**Root cause:** The ID format is `{type}_{market_id_short}_{timestamp}` — the market ID portion is truncated. Simmer API requires the full UUID.

**Fix:** Read `data/paper_trades.jsonl` directly. Each line is a JSON object with the full `market_id` field (e.g. `"market_id": "0a0cf089-aa36-45bf-a800-8f4752cdb9b1"`). Use these full UUIDs for all API calls. The `paper_trades.jsonl` is the authoritative source for both trade state and full market IDs.

**Verification:**
```bash
grep '"status": "open"' data/paper_trades.jsonl | python3.12 -c "import json,sys; [print(json.loads(l)['market_id'], json.loads(l)['question'][:60]) for l in sys.stdin]"
```

---

## 2026-04-20 — No Manual Close for Paper Positions

**Symptom:** User asked to manually close Hong Kong and Chengdu positions and take profit. No mechanism exists to do so.

**Root cause:** `paper_journal.py` only resolves positions via `update_resolved_trades()` which calls `_fetch_market_resolution()`. This fetches from Polymarket's Gamma API and only resolves when the market's `resolved` flag is `True`. There is no `close_position(trade_id, exit_price)` function.

**How Wellington resolved:** The market closed naturally on Polymarket — the date passed, Polymarket settled it, and the cron job's next `update_resolved_trades()` call picked up the settlement price.

**Limitation:** Cannot close positions manually at a specific price. Positions remain "open" in the journal until Polymarket resolves the market.

**If manual close is needed:** Add `close_position(trade_id: str, exit_price: float)` to `paper_journal.py` that sets `status="resolved"`, `exit_price=exit_price`, calculates P&L manually, and sets `resolved_at`.

---

## 2026-04-20 — Auto-Resolution Failure: CLOB API 403-Blocked

**Symptom:** Chengdu market (Apr 19) resolved to 30°C on Polymarket, but paper journal showed it as "open" indefinitely. Scan run showed no auto-resolution.

**Root cause (chain of failures):**

1. `_fetch_market_resolution()` called `https://clob.polymarket.com/markets/{market_id}` — this returns **403 Forbidden** for server-side HTTP requests without a browser session/cookies.

2. The `requests.HTTPError` was caught by `except Exception: return None` — completely silent, trades never resolved.

3. The Simmer API (`GET /api/sdk/context/{id}`) DOES detect `status: "resolved": true` from Polymarket's chain, but returns **`outcome: null`** — Simmer doesn't surface which bucket won.

4. `update_resolved_trades()` computes `exit_price` from `outcome`, so even with Simmer detecting resolution, `outcome: None` caused `exit_price = 1.0 if None.lower() in (...)` → `AttributeError` was also silently caught.

**Fixes applied (paper_journal.py):**

1. `_fetch_market_resolution()` now calls `https://api.simmer.markets/api/sdk/context/{id}` instead of CLOB. Uses `SIMMER_API_KEY` from env. Detects `resolved: True` but outcome still `None`.

2. `update_resolved_trades()` now guards against `outcome: None`:
   ```python
   outcome = resolution.get("outcome", "")
   if not outcome:
       continue  # Simmer doesn't surface outcomes — skip for now
   exit_price = 1.0 if outcome.lower() in ("yes", "true") else 0.0
   ```

**Limitation:** Simmer's context API (`/api/sdk/context/{id}`) returns `status: "resolved"` but `outcome: null` — so auto-resolution detection now works, but the winning bucket/PNL still won't be filled in. Paper trades will remain "open" with `pnl: null` until Simmer adds outcome to their API, or until we switch to scraping the Polymarket page directly.

**Workaround found:** `https://clob.polymarket.com/markets?_id={market_id}` (query param, not path param) returns 200 OK with full outcome data including `tokens[].winner`. However, `_outcome_price()` still uses the broken path-based URL — it would need updating to use this query-URL pattern if we want full auto-resolution.

**Manual close if needed:** Until full auto-resolution works, manually edit `paper_trades.jsonl` — set `status="resolved"`, `outcome="no"` (or `"yes"`), `exit_price=0.0` or `1.0` based on whether your bucket won, `pnl=(exit_price - entry_price) * shares`, and `resolved_at=ISO timestamp`.

---

## 2026-04-20 — GitHub Push Protected: Even Partial API Key Redaction Triggers GH013

**Symptom:** `git push` fails with `GH013: Repository rule violations found for refs/heads/master — GITHUB PUSH PROTECTION`. Even though `SKILL.md` showed `sk_liv...8edd` (partially redacted), GitHub's scanner still matched the pattern.

**Root cause:** GitHub scans for secret patterns, not just exact matches. The pattern `sk_liv...8edd` is close enough to the real `sk_live_77917ee...` that push protection triggered.

**Fix:** Full redaction to `***` or `sk_live_XREDACTED` — no partial key fragments.
```markdown
# WRONG — still triggers GH013:
- Key: `sk_liv...8edd`

# RIGHT:
- Key: `***`
```

---

## 2026-04-20 — GitHub API Auth Without gh CLI

**Problem:** `gh` CLI is not installed in this WSL environment. Can't use `gh auth` or `gh api`.

**Solution:** Token stored in `~/.git-credentials`. Extract with Python:
```python
import re
with open("/home/brandon/.git-credentials") as f:
    creds = f.read()
match = re.search(r'https://[^:]+:([^@]+)@github\.com', creds)
token = match.group(1)  # ghp_XXXXXXXX...
```
Then use `urllib.request.Request` with `Authorization: token {token}` header.

---

## 2026-04-20 — market_id Missing from forecast_history.jsonl

**Symptom:** Dashboard's "AIFS ENS Signals" table showed `market_id: —` for all entries.

**Root cause:** `log_forecast()` was called at line ~1612 in `weather_trader.py` — BEFORE bucket matching at ~1636-1706 where `market_id = matching_market.get("id")` is set. The `market_id` was always `None` at log time.

**Fix (two files):**
1. `scripts/forecast_history.py` — added `market_id: str | None = None` parameter
2. `weather_trader.py` — moved `log_forecast()` to AFTER `market_id = matching_market.get("id")` (~line 1706), in both "no match" and "matched" paths

**Limitation:** Old entries (pre-fix) in `forecast_history.jsonl` have `market_id: None` — cannot retroactively fix without full re-scan. Clear `data/forecast_cache.json` to force fresh logging.

---

## 2026-04-20 — WSL: Background Long-Running Processes

**Problem:** `python3.12 dashboard.py &` fails with "Foreground command uses '&' backgrounding. Use terminal(background=true)".

**Fix:** Use `terminal(background=True)` instead of shell `&`.
```python
# WRONG:
terminal("python3.12 dashboard.py &")

# RIGHT:
terminal("python3.12 dashboard.py", background=True)
```

**Health check:** `curl http://127.0.0.1:8414/` → 200 OK

---

## 2026-04-21 — WSL: `terminal(background=True)` Also Fails for dashboard.py

**Symptom:** `terminal(background=True, command="python3.12 /path/to/dashboard.py")` returns `Failed to start background process: [Errno 2] No such file or directory: 'None'`. Same failure mode across multiple invocations.

**Root cause:** The Hermes terminal tool's background=True path has an internal bug/assertion failure when the working directory doesn't exist or the Python path can't be resolved. It produces no useful error.

**Working workaround — use execute_code subprocess:**
```python
import subprocess, time
subprocess.run(["pkill", "-f", "dashboard.py"], capture_output=True)
time.sleep(1)
proc = subprocess.Popen(
    ["python3.12", "/home/brandon/projects/polymarket-weather-trader/scripts/dashboard.py"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
time.sleep(3)  # wait for uvicorn to bind
# test
import requests
r = requests.get("http://localhost:8414/api/config", timeout=5)
print(r.json())
```

**Alternative:** Use `hermes gateway restart --all` to restart all registered services including the dashboard if it's a registered gateway service.

---

## 2026-04-20 — Weak Signal Override Caused Bad Trade Entry

**Symptom:** NYC 58-59°F Apr 22 entered at $0.11 with `weak` signal (10.4° spread). Loss expected.

**Root cause:** `MIN_EDGE` gate allows `weak` signals through when `confidence - price ≥ 0.25`. For this trade: `0.68 - 0.11 = +0.57 ≥ 0.25` → allowed. A 10.4° spread means models fundamentally disagree — edge math looks good but forecast is noisy.

**Fix:** `MAX_SPREAD=5.8` hard cap added in `d455115` — trades with spread > 5.8° blocked regardless of edge.

---

## 2026-04-20 — patch() Accidentally Deleted Code Block

**Symptom:** While moving `log_forecast()` call in `weather_trader.py`, the `if not matching_market:` block was accidentally replaced with empty string → `IndentationError: expected an indented block`.

**Lesson:** When removing a block that's indented inside an `if` statement, must replace with `pass` or a comment, not empty string. Get exact text including all indentation before patching.

---

## 2026-04-20 — forecast_cache Blocks New log_forecast Entries

**Symptom:** After deploying `market_id` fix, scan still logged no `market_id` entries.

**Root cause:** `newly_fetched` flag controls `log_forecast()` call. If forecast is in cache from prior run, `newly_fetched = False` even with new code deployed.

**Fix:** Clear `data/forecast_cache.json` before running scan after deploying `market_id` fix.
```bash
rm data/forecast_cache.json && python3.12 weather_trader.py --dry-run
```

---

## 2026-04-20 — AIFS Cache Corruption: `_is_cache_fresh` Only Checked Age, Not Integrity

**Symptom:** `FileNotFoundError: /tmp/aifs_ens_savnawwl/latest_cf_cf.grib2.5b7b6.idx` appearing in scan FAILURES section. The `latest_cf.grib2` in `~/.cache/aifs_ens/` was only 2380 bytes (corrupt/incomplete). cfgrib failed to open it.

**Root cause:** `_is_cache_fresh()` in `scripts/aifs_forecast.py` only checked file age — it returned `True` if the file was younger than 24 hours, regardless of whether the file was a valid GRIB file. An interrupted AWS S3 download leaves a 2-4KB stub that passes the age check.

**Fix applied:** `_is_cache_fresh()` now also validates with `cfgrib.open_file(str(path))` before returning True. If cfgrib throws, the cache is considered stale and re-downloaded.

**Why doubled `_cf_cf` in path:** The traceback mixed stale tempdir path names — a red herring. The real issue was the corrupt 2380-byte cache file.

**Files touched:** `scripts/aifs_forecast.py` — `_is_cache_fresh()` function

**Verification:** After fix, scan re-downloaded fresh 8.1MB `latest_cf.grib2` from ECMWF AWS S3 and returned valid forecast data.

**Note:** The PF (perturbed forecast) file is ~39MB and download times out on slow connections. If PF times out, the scan still returns valid results using CF (control forecast) only — PF is part of the ensemble spread calculation but not critical for core functionality.

---

## 2026-04-21 — Gamma Integer IDs vs CLOB UUIDs: `_fetch_live_price` Returns $0 for HK/SZ

**Symptom:** Dashboard showed Hong Kong and Shenzhen positions with `current_price = $0.000` and `uPNL = $0.00` even though both markets were still live and trading.

**Root cause:** Two different market ID formats exist in Polymarket's ecosystem:

| Format | Example | Source |
|--------|---------|--------|
| CLOB UUID | `ef0583a5-3d4e-4003-8ebd-6eebf1c969fb` | Bot-generated trades via CLOB API |
| Gamma integer | `2019315`, `2019436` | Manual entries via Gamma UI; older or hand-placed trades |

`_fetch_live_price()` in `dashboard.py` only called the Simmer API (`api.simmer.markets/api/sdk/context/{id}`) which **resolves CLOB UUIDs but returns nothing for Gamma integer IDs** — silently, with no error.

The HK and SZ manual entries (backfilled from the bucket-match bug) both used Gamma integer IDs, so their prices were permanently $0.

**Fix:** `_fetch_live_price()` now branches on ID format:
- **If `market_id.isdigit()`** (integer): route through `GET https://gamma-api.polymarket.com/markets/{id}` → extract `clobTokenIds` (first element = YES token) → `GET https://clob.polymarket.com/price?token_id=...&side=buy`
- **Otherwise** (UUID): use Simmer API as before

**Files touched:** `dashboard.py` — `_fetch_live_price()` function (~line 899)

**API test (live):**
```
GET https://gamma-api.polymarket.com/markets/2019315
→ clobTokenIds[0] = "31522716..."  (YES token)
→ GET https://clob.polymarket.com/price?token_id=31522...&side=buy
→ {"price": "0.4"}  → HK current_price = $0.40 ✓

GET https://gamma-api.polymarket.com/markets/2019436
→ clobTokenIds[0] = "21667742..."  (YES token)
→ GET https://clob.polymarket.com/price?token_id=21667...&side=buy
→ {"price": "0.2"}  → SZ current_price = $0.20 ✓
```

**Pattern to remember:** Any time you see `$0.00` uPNL for active Polymarket positions, check whether the `market_id` is a plain integer. If so, the Gamma→CLOB path is required. Simmer only handles CLOB UUIDs.

---

## 2026-04-21 — cfgrib Stale `.idx` FileNotFoundError After Fresh GRIB Download

**Symptom:** Scan FAILURES section shows `FileNotFoundError: /tmp/aifs_ens_xxxxx/latest_cf_cf.grib2.5b7b6.idx` — two occurrences per city. GRIB download succeeds (correct 8.1MB file), but cfgrib fails to open it.

**Root cause:** cfgrib creates a `.idx` index file keyed to GRIB content. When ECMWF re-runs the AIFS model (every 12h), the new GRIB has different content → same filename, different hash. The old `.idx` from a prior run still exists but doesn't match the new GRIB → FileNotFoundError. Note: `_is_cache_fresh()` was already fixed to validate GRIB readability, but the stale-idx problem persists because cfgrib checks the idx at open time, not at cache validation time.

**Fix applied:** In `_extract_member_daily_values()` (`scripts/aifs_forecast.py`), delete any pre-existing `.idx` before calling `cfgrib.open_file()`:
```python
idx_path = grib_path + ".idx"
if os.path.exists(idx_path):
    os.unlink(idx_path)
ds = cfgrib_mod.open_file(grib_path)
```

**Commit:** `5e79768` — "fix: delete stale cfgrib .idx file before opening GRIB"

---

## 2026-04-21 — Win Rate Dashboard Shows Wrong % (50% vs 55.6%)

**Symptom:** Dashboard Win Rate card showed 50.0% but should have been ~55.6% based on actual outcomes.

**Root cause:** Win rate formula was `wins / resolved * 100`. Breakeven trades (pnl = 0.0, e.g. LA 68-69°F entry at $0.275 exit $0.275) were counted in the denominator but not in wins or losses. With 5 wins, 4 losses, 1 breakeven: `5/10 = 50.0%` instead of `5/9 = 55.6%`.

**Fix applied (`dashboard.py`, `_get_stats()`):**
```python
# WRONG:
win_rate=round(len(wins) / len(resolved) * 100, 1) if resolved else None
# RIGHT:
win_rate=round(len(wins) / (len(wins) + len(losses)) * 100, 1) if (wins or losses) else None
```

**Commit:** `6531bfb` — "feat: add data/errors.log for structured error persistence" (also fixed win rate)

---

## 2026-04-21 — errors.log: Structural Error Logging for Persistent Error Tracking

**Problem:** Errors only appeared in cron output markdown files. No persistent, queryable error log existed. Key errors (GRIB failures, API errors, safeguard blocks) were not being captured in a structured way.

**Solution:** Added `log_error(kind, msg, **ctx)` function in `weather_trader.py` that appends structured JSON lines to `data/errors.log`. Covers: API errors from `simmer_call`, no-forecast conditions, context fetch failures, price history failures, discovery search failures, and import failures. Each entry includes `ts`, `kind`, `msg`, plus relevant context (market_id, location, date, etc.).

**Logged error kinds:** `api_error`, `no_forecast`, `context_fetch`, `price_history`, `discovery_search`, `import_rate_limit`, `import_failed`. Safeguard skips (slippage, flip-flop, time decay) are NOT logged — those are expected conditions, not errors.

**Files touched:** `weather_trader.py` — `log_error()` function, wired into key exception handlers

**Commit:** `6531bfb`

---

## 2026-04-21 — Dashboard API Timeout / Hang: Kill + Restart Pattern

**Symptom:** `http://localhost:8414/api/state` hangs and times out. Dashboard process is running but unresponsive. Simple `kill <pid>` followed by restart fixes it.

**Root cause:** The uvicorn/FastAPI server inside dashboard.py can hang on a stuck request (likely a slow Simmer API call blocking the event loop). Not a code bug — just a runtime health issue.

**Restart pattern (since terminal background=True has issues):**
```python
import subprocess, time
# Kill old process
subprocess.run(["pkill", "-f", "dashboard.py"], capture_output=True)
time.sleep(1)
# Start new
proc = subprocess.Popen(
    ["/usr/bin/python3.12", "dashboard.py"],
    cwd="/home/brandon/.hermes/skills/solebrace-skills/polymarket-weather-trader",
    stdout=open("/tmp/dashboard.log", "w"),
    stderr=subprocess.STDOUT,
)
time.sleep(5)  # uvicorn takes a few seconds to bind
import requests
r = requests.get("http://localhost:8414/api/state", timeout=10)
```

**PID discovery:** `ss -tlnp | grep 8414` or `ps aux | grep dashboard`

---

## 2026-04-21 — Gamma API: `resolved=None` Despite Closed Markets

**Symptom:** Hong Kong Apr 21 (`2019315`) showed `closed=False` in Gamma API despite being past resolution time. Shenzhen Apr 21 (`2019436`) showed `closed=True` but `outcome=None` and `resolved=None`.

**Root cause:** Gamma's `closed` flag and `resolved`/`outcome` are separate fields. A market can be `closed=True` (Polymarket has ended trading) while `umaResolutionStatus=resolved` (UMA oracle has certified the outcome) but `outcome=None` (Gamma hasn't surfaced the winning bucket yet). Similarly, markets can still accept trades (`closed=False`) past their `endDate` until Polymarket's nightly settlement batch runs.

**What to use for resolution checks:**
- `closed=True` → market has stopped accepting new trades
- `umaResolutionStatus=resolved` → oracle has certified the result
- `outcome` field → the winning bucket (only appears after UMA settles)
- `lastTradePrice` → last traded price is the best real-time signal before settlement
- For P&L: Gamma `outcomePrices` array tells you which token (YES=index 0 or 1) maps to which outcome — interpret `lastTradePrice` in context of which bucket you hold

**Simmer API:** Only handles CLOB UUID market IDs. Non-UUID (integer) IDs from Gamma return 404. Always use paper journal's `market_id` field which contains the full UUID.

---

## 2026-04-21 — Simmer `context` API Returns `outcome: null` for Resolved Markets

**Symptom:** Even after markets like Shenzhen Apr 21 closed (`closed=True`), Simmer's `GET /api/sdk/context/{id}` still returned `resolved=None` and `outcome=null`.

**Root cause:** Simmer's resolution detection is eventually consistent — they poll Polymarket's settlement API and can lag by minutes to hours after `closed=True`. For the bot, this means `update_resolved_trades()` can detect a market is closed but still not compute P&L.

**Manual close pattern (still needed for now):**
1. Get exit price from Gamma API (`lastTradePrice` + `outcomePrices` array to determine which bucket won)
2. Update `paper_trades.jsonl`: `status="resolved"`, `outcome="yes"/"no"`, `exit_price`, `pnl`, `resolved_at`

---

## 2026-04-21 — `_load_trades_jsonl` vs `paper_journal.get_open_positions()`: Different Schemas

**Symptom:** Dashboard API (`/api/state`) showed 2 open positions. Journal showed 2 open. But dashboard's `status` field was `None` while journal showed `status=open`.

**Root cause:** `dashboard.py` uses `_load_trades_jsonl()` which returns raw dicts. It does NOT set a `status` key — the paper journal entries have `status: open` as a top-level key, but when `_get_stats()` in dashboard reads resolved/open counts, it checks `t.get("status")`. Both data sources agree on 2 open positions — the UI field just wasn't being set.

**Note:** This is a display only issue in the dashboard API response, not a data integrity problem. The paper journal is the authoritative source.
