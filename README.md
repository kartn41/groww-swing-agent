# groww-swing-agent (Phase 1-4 core: safety layer)

Default mode is **PAPER**. Nothing here can place a live order unless BOTH
`GROWW_MODE=LIVE` is set AND `data/LIVE_ARMED` contains the exact arm phrase
(see `app/config.py`). Anything else => PAPER.

## Run the tests
    python3 -m unittest discover -s tests -v      # or: pytest

## Status (264 tests passing)
| Module | Status |
|---|---|
| `config.py` fail-closed PAPER/LIVE, risk limits, cost model | done, tested |
| `risk/costs.py` delivery charges | done, **rates are placeholders: verify** |
| `risk/engine.py` sizing + every limit from the spec, net-of-cost R:R, fail-closed on stale data | done, tested |
| `execution/state_machine.py` 10 states, illegal transitions blocked, history recorded | done, tested |
| `execution/executor.py` deterministic reference IDs, duplicate-signal block, timeout -> reconcile (no blind retry), kill switch, position reconciliation | done, tested vs PaperBroker |
| `broker/paper.py` | done, tested |
| `broker/groww_adapter.py` | written from docs, tested against a **fake** SDK only; never run against Groww; refuses to build outside LIVE |

## Review #1 (2026-10-20): defects found and fixed
Each has a regression test in `tests/test_review_fixes.py`, and each test was checked to
FAIL when its fix is reverted (mutation check).

| # | Severity | Defect | Fix |
|---|---|---|---|
| A | Critical | Status-API error looked like "no order" -> duplicate order | Lookup is tri-state; outage => no order |
| B | Critical | No stop placed after a fill | GTT stop for filled qty; failure => `unprotected` ALERT |
| C | Critical | Kill-switch path relative to CWD; dead under launchd | Paths anchored to project root |
| D | High | Only `TimeoutError` treated as unknown outcome | Any SDK/network error => unknown => reconcile, never retry |
| E | High | `FAILED`/`COMPLETED`/`DELIVERY_AWAITED` unhandled | Status sets from Groww annexure; unknown => reconcile |
| F | High | Same-day buys missing (holdings only) | holdings + cash positions; schema drift => unavailable |
| G | High | Ack has no fill qty -> never reached OPEN | Re-read status by reference after every placement |
| H | Medium | Cash check ignored buy charges | Qty reduced until order + charges fit cash |
| I | Medium | No fresh-price / gap-chase check | Missing price, chase > 0.5%, or price <= stop => skip |
| J | Medium | Daily loss ignored open-position losses | realized + unrealized |
| K | Low | Wrong SL constant, hard-coded ADV cap, tautological test | Fixed |

## Known limitations still open
- Later fills of a partially filled order do not resize the GTT yet (monitoring engine, not built).
- When a position exits by any other route, its GTT must be cancelled, or a stale GTT could sell
  shares bought later. The monitor must own this; not built.
- One entry per symbol/strategy/day by design (deterministic reference id).
- Whether Groww holdings and positions double-count on T+1 is unverified; test with 1 share.
- Cost rates are placeholders.

## NOT built yet (do not assume it exists)
Market data ingestion + quality checks, NIFTY 50 universe, regime analysis, indicators,
strategies (A-D), scoring, backtester, SQLite persistence, scheduler/launchd, dashboard,
Telegram, restart recovery, Dockerfile/Makefile. Next phases, in the order of your spec.

## Facts verified in Groww docs (2026-10-20) that change the design
1. `get_historical_candle_data` is **deprecated**; use `get_historical_candles` with `groww_symbol="NSE-WIPRO"`.
   Daily/weekly: full history from 2020, max 180 days per request => the data layer must chunk.
2. **OCO in CASH supports MIS only, not CNC.** A broker-side bracket for delivery swing positions
   is therefore NOT available. Stop protection uses a **GTT** SELL (trigger DOWN, product CNC).
   A GTT arms a limit order; a gap-down can skip it. Residual risk is documented, not solved.
3. Rate limits are per type: Orders 10/s 250/min; Live data 10/s 300/min; Non-trading 20/s 500/min;
   token generation 150 per 24h.
4. Order placement requires a registered static IP (per community docs; confirm in your account),
   which a MacBook on home broadband usually cannot satisfy.
5. The Trading API needs an active paid subscription.

## Live activation (not recommended yet)
Needs: months of paper results, static IP registered, 1-share test of entry + GTT stop + cancel,
compliance check of current SEBI/NSE retail-algo rules with Groww, then
`echo I_ACCEPT_REAL_MONEY_RISK > data/LIVE_ARMED` and `GROWW_MODE=LIVE`.
Emergency stop at any time: `touch data/KILL_SWITCH`.


## Paper bot (added) - how to run
    mkdir -p data/universe        # put NSE's NIFTY 50 constituents CSV here as ind_nifty50list.csv
    python -m app.run fetch                              # candles from Groww (first run is long; read-only)
    python -m app.run paper --as-of 2026-10-23           # one-day test on a scratch account (works on weekends)
    python -m app.run paper --replay 2026-07-01:2026-10-23   # replay a range on a scratch account
    python -m app.run paper --today                      # real paper account; only after 15:45 IST
    python -m app.run status
    python -m app.run telegram-chatid / telegram-test    # Telegram setup helpers
Paper commands refuse to run when GROWW_MODE=LIVE. Not built: scheduler/systemd timers, dashboard,
more strategies, backtest statistics, earnings calendar. Unverified against the real API: the
daily-candle interval constant name in growwapi (fails loudly if wrong), Telegram delivery.

## Review #2 findings
- Buy-side charges were not deducted from paper cash (profits overstated ~Rs40/trade). Fixed; the
  accounting-identity test caught it.
- Mutation check: each safeguard was broken on purpose. One surviving mutant was a real test gap
  (entry-below-SMA50 guard); a crafted-data test now covers it. The look-ahead guard is double-layered.


## Release 3: scheduler, dashboard, more strategies, backtester, event calendar, live GTT monitor

### Strategies and regime rules
| Regime | Strategies allowed |
|---|---|
| STRONG_UP | breakout, pullback, rs_continuation, vcp |
| WEAK_UP | pullback, vcp |
| SIDEWAYS, WEAK_DOWN, STRONG_DOWN, HIGH_VOLATILITY, UNKNOWN | none (no new longs) |
Each stock is checked by every allowed strategy; the best-scoring setup is kept. All thresholds are
starting hypotheses to be judged by the backtester, not proven values.

### Event calendar
Save NSE's corporate event calendar (or your own list) as `data/events/events.csv` with at least a
symbol and a date column. Results, bonus, split, rights, buyback and merger events block new entries
within EVENT_BLOCK_DAYS. Held positions are never auto-sold for an event; you get a warning instead.
Older than 14 days = stale. Missing/stale: warning in paper mode; blocks entries if EVENTS_REQUIRED=true.

### Backtest
    python -m app.run backtest --start 2021-01-01 --holdout 2025-01-01
Development vs untouched holdout, 6-month walk-forward folds, sensitivity runs, each strategy alone,
results by strategy and regime, bootstrap confidence interval, NIFTY buy-and-hold comparison, red flags.
Uses the same engine as paper trading. Survivorship bias (today's NIFTY 50 list applied to the past)
cannot be removed with current data and is stated in every report. A full run takes a while on 2 vCPU;
run it on your Mac (data APIs need no static IP) or use --quick.

### Scheduler (server)
    sudo bash deploy/install_systemd.sh        # as the ubuntu user
Installs a weekday 18:30 IST timer (after NSE publishes its bhavcopy) for `python -m app.run daily` (fetch with retries, process every
unprocessed day, report, Telegram, heartbeat) and the dashboard service. If the server was off, the
next run catches up all missed trading days. `systemctl list-timers` shows the next run.
Add `--with-live-monitor` only when you go live.

### Dashboard
Set DASHBOARD_PASSWORD in .env, then from your Mac: `ssh -L 8501:localhost:8501 trader@<static-ip>`
and open http://localhost:8501. Shows mode, equity vs NIFTY, positions, planned orders, closed trades,
why stocks were rejected, risk limits, backtest reports and redacted logs. Its one control is
Stop/Allow new entries (the kill switch). It cannot place orders.

### Live GTT monitor (LIVE only)
`python -m app.run monitor` checks every agent position against Groww holdings and keeps its GTT
stop correct: resizes after partial fills or partial sales, re-arms missing/cancelled/expired GTTs,
cancels GTTs whose shares are gone, and never touches GTTs you created in the Groww app.
Not built yet: the live ENTRY runner that turns planned orders into real orders and registers them.
Until then live mode is not usable end to end, by design.

### Review #3 findings
- VCP's trend rule (rising 50-day average) rejected genuine VCPs; changed to a rising 200-day.
- VCP stops are naturally 3-5 ATR after a contraction; VCP gets a 5-ATR limit (8% cap kept).
- Mutation check: two survivors. Stale-GTT cancel is double-layered (orphan scan catches it in the
  same cycle). The required-calendar test passed vacuously on a sideways day; rewritten to use a day
  with real planned orders.

### Unverified against real services
growwapi daily-candle constant name, Telegram delivery, Groww GTT get/modify/cancel responses,
NSE event CSV column names. Each fails loudly or degrades safely if wrong.

### Review #4 findings (all fixed, each with a test that fails if the fix is reverted)
- HIGH: a held stock removed from the NIFTY 50 list stopped being managed (prices froze, stop/target/
  trend exit never checked). Engine now keeps managing exits for it, `fetch` keeps downloading its candles
  (paper ledger and live registry), and breadth/sector strength still use only current index members.
- Walk-forward folds shared their boundary day (simulated twice).
- Backtest reports now state that historical results dates are not modeled.
- Dashboard service no longer restarts forever when DASHBOARD_PASSWORD is missing.
- Fetch errors are alerted once per daily run (previously up to 4 times; a first fix silently dropped
  alerts when the candle arrived on the first try; caught in review and restructured).
- Process note: an edit during this review deleted two runner functions; the full test suite caught it,
  they were restored from the previous release, and every file was checked for lost definitions.


## Updating the server: `update-agent`
One-time setup (as the ubuntu user; copy `update.sh` from this zip, or the standalone file you were given):

    sudo install -m 755 deploy/update.sh /usr/local/sbin/update-agent

Every update: upload the new zip to /tmp (any name starting with `groww-swing-agent`), then

    sudo update-agent --dry-run     # optional: validates the zip and prints the plan, changes nothing
    sudo update-agent               # newest zip in /tmp; --full-tests runs the whole suite (slow)
    sudo update-agent --install-units   # also install + enable the daily timer and dashboard (first time only)

What it does: validates a private copy of the zip (corrupt files, unsafe paths and symlinks are refused),
stops the timers so nothing new starts, waits up to 5 minutes for a running job to finish, then
terminates (SIGTERM, then SIGKILL) anything left; moves the old version to `groww-swing-agent.bak-<time>`;
unpacks the new one; carries over `.env`, `data/`, `requirements.lock`, `check_login.py` and appends any
NEW settings from `.env.example` to `.env` (existing values are never changed); builds a fresh venv; runs
an import check and smoke tests; refreshes the unit files; restarts exactly the services that were enabled.
Any failure after the old version was moved aside restores it (folders, unit files, services) and exits
non-zero. A dashboard that will not start only warns (it needs DASHBOARD_PASSWORD).
Guards: refuses while GROWW_MODE=LIVE and the market is open (09:00-15:45 IST, Mon-Fri) unless `--force`;
refuses a second concurrent update. Keeps the last 3 backups. Log: `/home/trader/update.log`.
Manual rollback: stop the services, `mv groww-swing-agent groww-swing-agent.bad`,
`mv groww-swing-agent.bak-<time> groww-swing-agent`, start the services.

Tested here with a stub `systemctl` and fake running jobs (30 scenarios, each safeguard also broken on
purpose to confirm a test fails). NOT testable in this sandbox: the `sudo -u trader` switch, real systemd,
and a real `pip install`. Run `--dry-run` first, then a real update; a failure rolls back automatically.


### Review #5: rate limits on the first real data download
Found on the server: Groww answered "Rate limit has breached" and the old fetch kept going, so dozens of
stocks failed in a row. Groww's published rate-limit table does not list historical candles at all, so the
limit for this endpoint is unknown; the fetch now treats it cautiously and adapts:
- A pause of GROWW_DATA_PACE seconds (default 0.6) before EVERY request, retries included.
- On a rate-limit error: wait 30, 60, 90, 120 seconds (4 retries), and slow down for the rest of the run.
  Detected by message text as well as class: the real error arrived as plain GrowwAPIException.
- Login/permission errors stop the whole run at once. Three stocks in a row failing after all waits also
  stop it ("stopped early", Telegram alert), so Groww is never hammered. Unknown-symbol errors never stop it.
- Every 175-day chunk is saved as it arrives; a re-run resumes where it stopped.
- A lock (data/fetch.lock) prevents two fetches overlapping; the daily job skips and says so by Telegram.
- The summary prints requests, rate-limit hits and the final pace, so the real limit can be learned.

### Review #6: first run on real data (merged with review #7 below)
Result of the first real download: 553 requests, 0 rate-limit hits (so the default 0.6s pace is safe), but
all 50 stocks failed with `float(None)`: a candle came back with empty prices and the parser crashed on it.
- The parser now skips any candle with missing, non-finite or non-positive prices (or an unreadable date),
  records it, and keeps the rest. Nothing is repaired or guessed. `fetch` prints how many were skipped and
  an example; many skipped candles in one symbol trigger a Telegram alert. Quality checks still flag gaps.
- The fetch no longer asks for today's candle before 15:45 IST (it is still forming and would be discarded
  anyway). Likely cause of the real failure: a 09:16 run received today's empty candle. Not confirmed.
- Candles saved before the failure were kept, so the re-run resumes with about one request per stock.

### Review #7: reconciling two copies of the fix, plus `probe`
While fixing the empty-price crash I found the working folder already contained an earlier, documented fix
(review #6 above) that I had not seen; my new patch collided with it and briefly left two competing mechanisms.
They were merged into ONE: skipped candles are `(date, reason)` pairs, handed over per stock by `take_skipped()`,
reported in the summary field `dropped`. Nothing from review #6 was removed. Added on top:
- Format-change guard: if more than half of a response's candles are unusable (5+ candles), the stock fails
  with `MalformedResponse` instead of silently saving almost nothing; three stocks in a row stop the run.
- `python -m app.run probe ADANIENT [--start 2020-01-01]`: read-only, saves nothing. Downloads one stock and
  prints every unusable candle (date, reason, chunk). Use it to see exactly what Groww sends.

### Review #8: stock candles from NSE bhavcopy
A scan of all 51 symbols (`scripts/scan_fetch_ranges.py`) showed Groww's NIFTY candles are complete through
2026-10-26, but every NIFTY 50 stock came back mostly empty from 2025-10-05 on (about 18 of 118 and 2 of 142
candles usable). With no 2026 stock bars, breadth could not be computed and every day ended `UNKNOWN`.
- `STOCK_SOURCE=bhavcopy` (in `.env.example`): stock candles after `GROWW_EQUITY_UNTIL` come from NSE's official
  end-of-day file, one download per trading day for all stocks (series EQ only). Groww still supplies NIFTY and
  the stock history up to that date, and is no longer asked for stocks past it, so the daily MalformedResponse
  alert stops. A stock whose stored history ends more than 10 days before the cutoff is not extended (no gap is
  bridged); a missing older file stops the download instead of skipping the day. `TMPV`, which Groww never
  returned, now fills from its listing in mid-October 2025.
- A day is processed only once 80% of index stocks have a candle for it; until then it waits for the next run
  instead of being used up as `UNKNOWN`. The daily job retries while today's bhavcopy is missing, and the timer
  moved from 16:05 to 18:30 IST.
- Bhavcopy prices are as traded. If Groww's older history was adjusted for a split that happened later, the
  join shows up as the existing "possible unadjusted split/bonus" data issue and that stock is excluded.
- Not verified from the Lightsail server: NSE sometimes refuses cloud IPs. A refusal stops the download with
  a clear error (HTTP 403) and a Telegram alert.

### Review #9: optional pullback volume gate
The 2024-01 to 2025-10 replay had 34 pullback trades (7 winners, net -Rs8,218), while the other strategies
were slightly positive. Volume only added to the pullback score and never blocked an entry.
- `PULLBACK_VOLUME_GATE=on`: the bounce day needs at least average volume (vs the previous 20 days) and the
  4 dip days need below-average volume (vs the 20 days before). Off by default; compare replays with it on and
  off before enabling it in `.env`.
- The daily Telegram report adds the capital used (invested vs cash), each open position's value as a share of
  equity with its open profit/loss, every stock sold that day with its net % after costs, and running stats
  for all closed trades (win rate, average win %, average loss %, best and worst).
