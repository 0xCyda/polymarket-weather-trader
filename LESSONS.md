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
