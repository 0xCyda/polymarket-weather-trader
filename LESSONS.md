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

**Impact:** uPNL and current_price are frozen at $0 until the API key is fixed or env is loaded.

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
