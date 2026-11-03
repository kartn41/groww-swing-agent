"""Command line.
  python -m app.run fetch                         download/refresh daily candles (read-only API)
  python -m app.run paper --today                 process today on the real paper account (after 15:45 IST)
  python -m app.run paper --as-of 2026-10-23      one-day test on a scratch account (any day, incl. weekends)
  python -m app.run paper --replay 2026-07-01:2026-10-23   replay many days on a scratch account
  python -m app.run status                        paper account summary
  python -m app.run daily                         scheduled job: fetch, then process every unprocessed day
  python -m app.run paper --catch-up              process all unprocessed days on the real paper account
  python -m app.run backtest --start 2021-01-01 --holdout 2025-01-01   statistics + walk-forward report
  python -m app.run dashboard                     local dashboard on 127.0.0.1 (SSH tunnel to view)
  python -m app.run monitor                       LIVE only: check/resize/re-arm/cancel GTT stops
  python -m app.run fetch-intraday                download 5m candles into data/intraday_market.db
  python -m app.run intraday --replay 2026-10-01:2026-10-03   paper ORB on 5m bars (scratch db unless --persist)
  python -m app.run intraday --as-of 2026-10-01
  python -m app.run intraday-status               intraday paper account summary
  python -m app.run intraday-backtest --start 2026-09-01 --end 2026-10-31
  python -m app.run probe ADANIENT                read-only: show unusable candles Groww returns for one stock
  python -m app.run telegram-chatid               find your chat id (after messaging your bot)
  python -m app.run telegram-test                 send a test message
Paper commands refuse to run when GROWW_MODE=LIVE."""
from __future__ import annotations
import argparse
import logging
import math
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import DATA_DIR, Mode, PROJECT_ROOT, load_settings
from app.env import load_env_file
from app.execution.executor import kill_switch_active
from app.logging_setup import setup_logging
from app.notify import TelegramNotifier, find_chat_ids, format_daily, format_intraday_backtest, format_intraday_session

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("app.run")
MARKET_DB = str(DATA_DIR / "market.db")
PAPER_DB = str(DATA_DIR / "paper.db")
INTRADAY_MARKET_DB = str(DATA_DIR / "intraday_market.db")
INTRADAY_PAPER_DB = str(DATA_DIR / "intraday_paper.db")
UNIVERSE_CSV = str(DATA_DIR / "universe" / "ind_nifty50list.csv")
REPORTS = PROJECT_ROOT / "reports"
EVENTS_CSV = str(DATA_DIR / "events" / "events.csv")
LIVE_DB = str(DATA_DIR / "live.db")
FETCH_LOCK = str(DATA_DIR / "fetch.lock")
# A day is processed only once at least this share of index stocks has a candle for it.
STOCK_COVERAGE = 0.8


class FetchBusy(Exception):
    """Another fetch (manual or scheduled) is already running; two at once would double the API load."""


def _events():
    from app.events import load_events
    try:
        return load_events(EVENTS_CSV)
    except Exception as e:
        log.warning("event calendar unreadable: %s", e)
        from app.events import EventCalendar
        return EventCalendar([], [f"event calendar unreadable: {e}"], usable=False)


def _event_opts():
    return {"events": _events(),
            "events_required": os.environ.get("EVENTS_REQUIRED", "false").lower() == "true",
            "event_block_days": int(os.environ.get("EVENT_BLOCK_DAYS", "7"))}


def _capital(arg) -> float:
    return float(arg or os.environ.get("PAPER_CAPITAL", "250000"))


def _universe():
    from app.universe import load_universe
    uni, warns = load_universe(UNIVERSE_CSV)
    for w in warns:
        log.warning(w)
    return uni


def _held_symbols() -> set[str]:
    """Stocks the agent still holds or has pending orders in, from the paper ledger and live registry."""
    import sqlite3
    out: set[str] = set()
    for db, sql in ((PAPER_DB, "SELECT symbol FROM positions UNION SELECT symbol FROM orders WHERE status='PENDING'"),
                    (LIVE_DB, "SELECT symbol FROM positions WHERE status='OPEN'")):
        if os.path.exists(db):
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                out |= {r[0] for r in con.execute(sql)}
            except sqlite3.Error:
                pass
            finally:
                con.close()
    return out


def _bhavcopy_mode() -> bool:
    return os.environ.get("STOCK_SOURCE", "groww").strip().lower() == "bhavcopy"


def _equity_until() -> date:
    return date.fromisoformat(os.environ.get("GROWW_EQUITY_UNTIL", "2025-10-04").strip())


def _stock_counts(store, uni, since: date) -> dict[date, int]:
    """Trading day -> number of index stocks with a stored candle that day."""
    from app.paper.engine import gsym
    syms = [gsym(c.symbol) for c in uni]
    if not syms:
        return {}
    q = (f"SELECT d, COUNT(*) FROM candles WHERE d>=? AND symbol IN ({','.join('?' * len(syms))}) GROUP BY d")
    return {date.fromisoformat(d): n for d, n in store.con.execute(q, [since.isoformat(), *syms])}


def _stocks_needed(uni) -> int:
    return max(1, math.ceil(STOCK_COVERAGE * len(uni)))


def _stocks_ready(store, d: date) -> bool:
    uni = _universe()
    return _stock_counts(store, uni, d).get(d, 0) >= _stocks_needed(uni)


def _sync_bhavcopy(store, stocks: list[str], now: datetime) -> dict:
    from app.data.groww_source import FINAL_CANDLE_AFTER
    from app.data.nse_bhavcopy import BhavcopySource, sync_bhavcopy
    from app.paper.engine import INDEX_SYMBOL
    until = _equity_until()
    cutoff = now.date() if now.time() >= FINAL_CANDLE_AFTER else now.date() - timedelta(days=1)
    days = [d for d in store.load(INDEX_SYMBOL, end=cutoff).index if d > until]
    src = BhavcopySource(pace_s=float(os.environ.get("BHAVCOPY_PACE", "1.0")))
    return sync_bhavcopy(store, src, stocks, days, joinable_after=until - timedelta(days=10))


def _fetch() -> dict:
    import fcntl
    from app.data.groww_source import GrowwCandleSource, groww_client_from_env, sync_daily
    from app.data.store import CandleStore
    from app.paper.engine import INDEX_SYMBOL, gsym
    os.makedirs(os.path.dirname(FETCH_LOCK), exist_ok=True)
    with open(FETCH_LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FetchBusy("another fetch is already running")
        uni = _universe()
        store = CandleStore(MARKET_DB)
        src = GrowwCandleSource(groww_client_from_env(), throttle_s=float(os.environ.get("GROWW_DATA_PACE", "0.6")))
        stocks = sorted({gsym(c.symbol) for c in uni} | {gsym(x) for x in _held_symbols()})
        symbols = [INDEX_SYMBOL] + stocks
        now = datetime.now(IST)
        b = None
        if _bhavcopy_mode():
            until = _equity_until()
            s = sync_daily(store, src, symbols, now, end_caps={x: until for x in stocks})
            b = _sync_bhavcopy(store, stocks, now)
        else:
            s = sync_daily(store, src, symbols, now)
    print(f"updated {len(s['updated'])} symbols, {sum(s['updated'].values())} rows; errors: {len(s['errors'])}")
    if s.get("capped"):
        print(f"Groww not asked for {len(s['capped'])} stocks: their candles after {_equity_until()} come from NSE bhavcopy")
    st = s.get("stats")
    if st:
        print(f"requests: {st['requests']}, rate-limit hits: {st['rate_limited']}, network retries: {st['transient']}, "
              f"final pace: {st['final_pace_s']}s between requests")
    for sym, e in s["errors"].items():
        print(f"  ERROR {sym}: {e}")
    dropped = s.get("dropped") or {}
    if dropped:
        total = sum(len(v) for v in dropped.values())
        print(f"skipped {total} invalid candle(s) in {len(dropped)} symbol(s) (empty or non-positive prices; not repaired):")
        for sym, v in list(dropped.items())[:5]:
            print(f"  {sym}: {len(v)} e.g. {v[0][0]} {v[0][1]}")
    if s.get("aborted"):
        print(f"STOPPED EARLY: {s['aborted']}")
        print(f"  not attempted: {len(s['skipped'])} symbols. Everything downloaded so far is saved.")
    if b is not None:
        _report_bhavcopy(b)
        s["errors"].update(b["errors"])
        s.setdefault("dropped", {}).update(b["dropped"])
        if b["aborted"] and not s.get("aborted"):
            s["aborted"] = b["aborted"]
        s["bhavcopy"] = b
    return s


def _report_bhavcopy(b: dict) -> None:
    days = b["days"]
    print(f"NSE bhavcopy: {len(days)} day(s) loaded" + (f" ({days[0]} to {days[-1]})" if days else "")
          + f", {b['rows']} stock candles, {b['requests']} downloads")
    if b["not_published"]:
        print(f"  NSE has not published the bhavcopy for {b['not_published']} yet; the next run picks it up")
    if b["behind"]:
        print(f"  not filled from NSE (stored history stops too early; fetch it from Groww first): {', '.join(b['behind'])}")
    for sym, ds in list(b["absent"].items())[:10]:
        print(f"  {sym}: not in the NSE file on {len(ds)} day(s), e.g. {ds[0]}")
    for k, e in b["errors"].items():
        print(f"  ERROR {k}: {e}")
    if b["aborted"]:
        print(f"STOPPED EARLY: {b['aborted']}")


def _alert_fetch_errors(s: dict) -> None:
    many = {k: v for k, v in (s.get("dropped") or {}).items() if len(v) > 5}
    if many:
        n = TelegramNotifier.from_env()
        if n:
            n.alert(f"Data fetch skipped many invalid candles in {len(many)} symbol(s), e.g. {next(iter(many))}. "
                    "Check the data source.")
    if s.get("errors") or s.get("aborted"):
        n = TelegramNotifier.from_env()
        if n:
            head = f"Data fetch STOPPED EARLY: {s['aborted']}" if s.get("aborted") else \
                f"Data fetch had {len(s['errors'])} errors (e.g. {next(iter(s['errors']))})."
            n.alert(head + " New paper entries may be skipped for the affected symbols.")


def cmd_fetch(args) -> int:
    try:
        s = _fetch()
    except FetchBusy:
        print("Another fetch is already running (check: pgrep -af 'app.run'); not starting a second one.", file=sys.stderr)
        return 3
    _alert_fetch_errors(s)
    return 1 if (s["errors"] or s.get("aborted")) else 0


def _run_days(days, db_path, capital, settings, fresh: bool, write_reports: bool, notifier=None) -> int:
    from app.data.store import CandleStore
    from app.paper.engine import process_day
    from app.paper.ledger import Ledger
    from app.paper.report import render, summary
    if fresh and os.path.exists(db_path):
        os.remove(db_path)
    ledger = Ledger(db_path, capital)
    store, uni = CandleStore(MARKET_DB), _universe()
    REPORTS.mkdir(exist_ok=True)
    for d in days:
        res = process_day(d, ledger, store, uni, settings, kill_switch=kill_switch_active(settings), **_event_opts())
        text = render(res, ledger)
        if write_reports:
            (REPORTS / f"paper_{d}.md").write_text(text)
        if notifier is not None:
            if not notifier.send(format_daily(res, ledger, settings.mode.value)):
                log.warning("daily Telegram message was not delivered (report file is still saved)")
        if len(days) == 1:
            print(text)
    if len(days) > 1:
        print(summary(ledger))
    return 0


def cmd_paper(args) -> int:
    settings = load_settings()
    if settings.mode is Mode.LIVE:
        print("Refusing: paper commands do not run while GROWW_MODE=LIVE.", file=sys.stderr)
        return 2
    cap = _capital(args.capital)
    if args.today:
        now = datetime.now(IST)
        if now.time() < datetime.strptime("15:45", "%H:%M").time():
            print("Today's daily candle is not final before 15:45 IST; run later.", file=sys.stderr)
            return 2
        notifier = TelegramNotifier.from_env()
        try:
            return _run_days([now.date()], PAPER_DB, cap, settings, fresh=False, write_reports=True,
                             notifier=notifier)
        except Exception as e:
            log.exception("paper run failed")
            if notifier:
                notifier.alert(f"PAPER run for {now.date()} FAILED: {type(e).__name__}. Check logs/agent.log.")
            return 1
    if args.catch_up:
        return catch_up(settings, cap, TelegramNotifier.from_env(), datetime.now(IST))
    if args.as_of:
        d = date.fromisoformat(args.as_of)
        return _run_days([d], str(DATA_DIR / "scratch_asof.db"), cap, settings, fresh=True, write_reports=False)
    if args.replay:
        from app.data.store import CandleStore
        from app.paper.engine import INDEX_SYMBOL
        a, b = (date.fromisoformat(x) for x in args.replay.split(":"))
        idx = CandleStore(MARKET_DB).load(INDEX_SYMBOL, end=b)
        days = [d for d in idx.index if a <= d <= b]
        if not days:
            print("No NIFTY candles in that range. Run `fetch` first.", file=sys.stderr)
            return 2
        db = str(DATA_DIR / f"replay_{a}_{b}.db")
        rc = _run_days(days, db, cap, settings, fresh=True, write_reports=False)
        print(f"Replay ledger kept at {db}")
        return rc
    print("choose --today, --as-of or --replay", file=sys.stderr)
    return 2


FINAL_AFTER = datetime.strptime("15:45", "%H:%M").time()
INTRADAY_SESSION_FINAL = datetime.strptime("15:30", "%H:%M").time()


def catch_up(settings, cap, notifier, now: datetime) -> int:
    """Process every NIFTY trading day after the last processed one whose candle is final.
    First ever run: only the latest final day (no silent months-long backfill)."""
    from app.data.store import CandleStore
    from app.paper.engine import INDEX_SYMBOL, process_day
    from app.paper.ledger import Ledger
    from app.paper.report import render
    cutoff = now.date() if now.time() >= FINAL_AFTER else date.fromordinal(now.date().toordinal() - 1)
    store = CandleStore(MARKET_DB)
    idx_days = [d for d in store.load(INDEX_SYMBOL, end=cutoff).index]
    ledger = Ledger(PAPER_DB, cap)
    last = ledger.get("last_processed")
    days = [d for d in idx_days if last is None or d > date.fromisoformat(last)]
    uni = _universe() if days else []
    if days:
        counts, need = _stock_counts(store, uni, days[0]), _stocks_needed(uni)
        waiting = []
        while days and counts.get(days[-1], 0) < need:     # processing these would use up the day with no stock data
            waiting.append(days.pop())
        if waiting:
            print(f"Waiting for stock candles: {waiting[-1]} to {waiting[0]} ({len(waiting)} trading day(s)) "
                  f"have candles for fewer than {need} of {len(uni)} stocks; not processed yet.")
    if last is None:
        days = days[-1:]
    if not days:
        print("Nothing to process: paper account is up to date.")
        return 0
    REPORTS.mkdir(exist_ok=True)
    opts = _event_opts()
    for i, d in enumerate(days):
        res = process_day(d, ledger, store, uni, settings, kill_switch=kill_switch_active(settings), **opts)
        (REPORTS / f"paper_{d}.md").write_text(render(res, ledger))
        print(f"processed {d}: regime {res.regime}, {len(res.new_orders)} planned, {len(res.fills)} fills, {len(res.exits)} exits")
        if notifier is not None and i == len(days) - 1:
            msg = format_daily(res, ledger, settings.mode.value)
            if len(days) > 1:
                msg = f"(Caught up {len(days)} missed trading days: {days[0]} to {d}.)\n" + msg
            if not notifier.send(msg):
                log.warning("daily Telegram message was not delivered (report file is still saved)")
    return 0


def _heartbeat():
    url = os.environ.get("HEARTBEAT_URL", "").strip()
    if not url:
        return
    try:
        import urllib.request
        urllib.request.urlopen(url, timeout=10).read()
    except Exception as e:
        log.warning("heartbeat ping failed: %s", type(e).__name__)


def cmd_daily(args) -> int:
    """Scheduled after market close: fetch (retrying while today's candle is missing), catch up, ping."""
    import time as _time
    settings = load_settings()
    if settings.mode is Mode.LIVE:
        print("daily runs the PAPER account; refusing while GROWW_MODE=LIVE.", file=sys.stderr)
        return 2
    notifier = TelegramNotifier.from_env()
    try:
        from app.data.store import CandleStore
        from app.paper.engine import INDEX_SYMBOL
        retries = int(os.environ.get("DAILY_RETRIES", "3"))
        wait = float(os.environ.get("DAILY_RETRY_WAIT", "600"))
        summary = {"errors": {}}
        for attempt in range(retries + 1):
            try:
                summary = _fetch()
            except FetchBusy:
                log.warning("daily run skipped: a data fetch is already running")
                if notifier:
                    notifier.send("Daily run skipped: a data fetch is still running (probably the first full download). "
                                  "The next scheduled run will catch up every missed day.")
                return 0
            now = datetime.now(IST)
            store = CandleStore(MARKET_DB)
            have_today = store.last_date(INDEX_SYMBOL) == now.date() and \
                (not _bhavcopy_mode() or _stocks_ready(store, now.date()))
            if have_today or now.weekday() >= 5 or now.time() < FINAL_AFTER or attempt == retries:
                break
            log.info("today's NIFTY or stock candles for %s not available yet; retrying in %.0fs", now.date(), wait)
            _time.sleep(wait)
        _alert_fetch_errors(summary)                      # once, based on the final attempt
        now = datetime.now(IST)
        store = CandleStore(MARKET_DB)
        if now.weekday() < 5 and now.time() >= FINAL_AFTER and notifier:
            if store.last_date(INDEX_SYMBOL) != now.date():
                notifier.send(f"No NIFTY candle for {now.date()}: market holiday or data delay. Nothing processed for today.")
            elif _bhavcopy_mode() and not _stocks_ready(store, now.date()):
                notifier.send(f"Stock candles for {now.date()} are not in yet (NSE bhavcopy not published or not "
                              "downloaded). Today is left for the next run, which catches it up.")
        rc = catch_up(settings, _capital(None), notifier, now)
        _heartbeat()
        return rc
    except Exception as e:
        log.exception("daily job failed")
        if notifier:
            notifier.alert(f"Daily job FAILED: {type(e).__name__}. Check logs/agent.log.")
        return 1


def cmd_backtest(args) -> int:
    from datetime import datetime as _dt
    from app import backtest as bt
    from app.data.store import CandleStore
    from app.paper.engine import INDEX_SYMBOL, gsym
    from app.strategies import STRATEGIES
    settings = load_settings()
    uni = _universe()
    cache = bt.CachedStore(CandleStore(MARKET_DB), [INDEX_SYMBOL] + [gsym(c.symbol) for c in uni])
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else max(cache.trading_days(start, date(2100, 1, 1)), default=start)
    hold = date.fromisoformat(args.holdout) if args.holdout else None
    dev_end = date.fromordinal(hold.toordinal() - 1) if hold else end
    cap = _capital(args.capital)
    ev = _events()
    print("running development period..."); dev = bt.stats(bt.run(cache, uni, start, dev_end, settings, cap, events=ev))
    test = bt.stats(bt.run(cache, uni, hold, end, settings, cap, events=ev)) if hold else None
    print("running walk-forward folds..."); folds = bt.walk_forward(cache, uni, start, dev_end, settings, cap, args.fold_months, ev)
    sens = abl = {}
    if not args.quick:
        print("running sensitivity and per-strategy checks...")
        sens = bt.sensitivity(cache, uni, start, dev_end, settings, cap, ev)
        abl = bt.ablation(cache, uni, start, dev_end, settings, cap, list(STRATEGIES), ev)
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"backtest_{_dt.now(IST).strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(bt.render(dev, test, folds, sens, abl, cap))
    print(f"report: {path}")
    for f in bt.verdict(dev, test, folds):
        print(f"  - {f}")
    return 0


def cmd_dashboard(args) -> int:
    from app.dashboard import DashConfig, make_server
    cfg = DashConfig(PAPER_DB, MARKET_DB, str(REPORTS), str(PROJECT_ROOT / "logs" / "agent.log"), load_settings(),
                     os.environ.get("DASHBOARD_PASSWORD", ""), port=int(os.environ.get("DASHBOARD_PORT", "8501")))
    srv = make_server(cfg)
    print(f"dashboard on http://127.0.0.1:{cfg.port} (from your Mac: ssh -L {cfg.port}:localhost:{cfg.port} ...)")
    srv.serve_forever()
    return 0


def cmd_monitor(args) -> int:
    from app.broker.groww_adapter import GrowwBroker
    from app.data.groww_source import groww_access_token_from_env
    from app.execution.monitor import LiveRegistry, Monitor
    from app.net_guard import check_static_ip
    settings = load_settings()
    if settings.mode is not Mode.LIVE:
        print("monitor manages real GTT orders and only runs in LIVE mode.", file=sys.stderr)
        return 2
    notifier = TelegramNotifier.from_env()
    ok, why = check_static_ip(os.environ.get("GROWW_STATIC_IP"))
    if not ok:
        if notifier:
            notifier.alert(f"Monitor did not run: {why}")
        print(why, file=sys.stderr)
        return 2
    mon = Monitor(GrowwBroker(settings, groww_access_token_from_env()), LiveRegistry(LIVE_DB))
    res = mon.cycle(datetime.now(IST).date())
    for a in res.actions:
        print("ACTION", a)
    for a in res.alerts:
        print("ALERT", a)
    if res.alerts and notifier:
        notifier.alert("LIVE monitor:\n" + "\n".join(res.alerts))
    return 1 if res.aborted else 0


def cmd_telegram_test(args) -> int:
    n = TelegramNotifier.from_env()
    if n is None:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env", file=sys.stderr)
        return 2
    ok = n.send(f"Test message from groww-swing-agent ({load_settings().mode.value} mode). "
                "If you can read this, notifications work.")
    print("sent" if ok else "FAILED to send (see logs/agent.log)")
    return 0 if ok else 1


def cmd_telegram_chatid(args) -> int:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("Set TELEGRAM_BOT_TOKEN in .env first", file=sys.stderr)
        return 2
    try:
        chats = find_chat_ids(token)
    except Exception as e:
        print(f"Could not reach Telegram: {type(e).__name__}", file=sys.stderr)
        return 1
    if not chats:
        print("No messages found. Open your bot in Telegram, press Start, send 'hi', then run this again.")
        return 1
    for c in chats:
        print(f"chat id {c['id']}  ({c['type']}, {c['name']})")
    return 0


def cmd_probe(args) -> int:
    """Read-only diagnostic: download one symbol's daily candles and show every unusable one. Saves nothing."""
    from app.data.groww_source import GrowwCandleSource, groww_client_from_env, parse_daily_candles
    sym = args.symbol if args.symbol.startswith("NSE-") else f"NSE-{args.symbol.upper()}"
    src = GrowwCandleSource(groww_client_from_env(), throttle_s=float(os.environ.get("GROWW_DATA_PACE", "0.6")))
    start = date.fromisoformat(args.start)
    total = usable = 0
    bad_rows: list[tuple] = []
    dates: list[str] = []
    for s, e, resp in src.iter_raw(sym, start, datetime.now(IST).date()):
        candles = (resp or {}).get("candles") or []
        mal: list = []
        rows = parse_daily_candles(resp, mal)
        total += len(candles)
        usable += len(rows)
        dates += [r[0] for r in rows]
        bad_rows += [(f"{s}..{e}", m) for m in mal]
    print(f"{sym}: {total} candles, {usable} usable, {len(bad_rows)} unusable; "
          f"usable range {min(dates) if dates else 'n/a'} to {max(dates) if dates else 'n/a'}")
    for window, (day, why) in bad_rows[:15]:
        print(f"  UNUSABLE {day} (chunk {window}): {why}")
    if len(bad_rows) > 15:
        print(f"  ... and {len(bad_rows) - 15} more")
    return 0


def _intraday_capital(arg) -> float:
    return float(arg or os.environ.get("INTRADAY_PAPER_CAPITAL", os.environ.get("PAPER_CAPITAL", "250000")))


def cmd_fetch_intraday(args) -> int:
    import fcntl
    from app.data.intraday_groww import GrowwIntradaySource, sync_intraday
    from app.data.intraday_store import IntradayStore
    from app.paper.engine import INDEX_SYMBOL, gsym
    from app.data.groww_source import groww_client_from_env

    lock = str(DATA_DIR / "fetch_intraday.lock")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(lock, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another intraday fetch is already running.", file=sys.stderr)
            return 3
        uni = _universe()
        store = IntradayStore(INTRADAY_MARKET_DB)
        src = GrowwIntradaySource(groww_client_from_env(), throttle_s=float(os.environ.get("GROWW_DATA_PACE", "0.6")))
        days_back = int(getattr(args, "days", None) or os.environ.get("INTRADAY_FETCH_DAYS", "30"))
        end = datetime.now(IST).date()
        start = end - timedelta(days=days_back)
        symbols = [INDEX_SYMBOL] + sorted({gsym(c.symbol) for c in uni})
        s = sync_intraday(store, src, symbols, start, end)
    print(f"intraday sync: {s['symbols']} symbols, {s['rows']} rows; errors: {len(s['errors'])}")
    for sym, e in s["errors"].items():
        print(f"  ERROR {sym}: {e}")
    return 1 if s["errors"] else 0


def _intraday_days_with_data(store, start: date, end: date) -> list[date]:
    from app.intraday.engine import INDEX_SYMBOL

    out: list[date] = []
    d = start
    while d <= end:
        if len(store.session_times(d, INDEX_SYMBOL)) >= 10:
            out.append(d)
        d += timedelta(days=1)
    return out


def _run_intraday_days(
    days: list[date],
    db_path: str,
    capital: float,
    settings,
    *,
    fresh: bool,
    write_reports: bool,
    backtest: bool,
    notifier=None,
    telegram: bool = True,
) -> tuple[int, object | None]:
    """Run ORB paper sessions. Returns (exit_code, last SessionResult)."""
    from app.data.intraday_store import IntradayStore
    from app.intraday.engine import process_session
    from app.intraday.report import backtest_report, render
    from app.paper.ledger import Ledger

    if not os.path.exists(INTRADAY_MARKET_DB):
        print("Hint: run `fetch-intraday` first to populate data/intraday_market.db", file=sys.stderr)
        return 2, None
    if fresh and os.path.exists(db_path):
        os.remove(db_path)
    ledger = Ledger(db_path, capital)
    store = IntradayStore(INTRADAY_MARKET_DB)
    uni = _universe()
    REPORTS.mkdir(exist_ok=True)
    mode = settings.mode.value
    skipped = 0
    ran = 0
    last_res = None
    for d in days:
        try:
            res = process_session(d, ledger, store, uni, settings)
        except Exception as e:
            log.exception("intraday session failed for %s", d)
            if notifier and telegram:
                notifier.alert(f"PAPER intraday run for {d} FAILED: {type(e).__name__}. Check logs/agent.log.")
            return 1, last_res
        last_res = res
        if res.skipped:
            skipped += 1
            print(f"{d}: skipped — {'; '.join(res.notes)}")
            continue
        ran += 1
        text = render(res, ledger)
        if write_reports:
            (REPORTS / f"intraday_{d}.md").write_text(text)
        print(f"{d}: equity {res.equity:,.0f} fills={len(res.fills)} exits={len(res.exits)}")
        if len(days) == 1:
            print(text)
        if notifier is not None and telegram and not backtest and len(days) == 1:
            if not notifier.send(format_intraday_session(res, ledger, mode)):
                log.warning("intraday Telegram message was not delivered (report file may still be saved)")
    if backtest and days:
        a, b = days[0], days[-1]
        path = REPORTS / f"intraday_backtest_{a}_{b}.md"
        path.write_text(backtest_report(ledger, a, b, ran, skipped))
        print(f"backtest report: {path}")
        from app.paper.report import summary as ledger_summary

        print(ledger_summary(ledger))
        if notifier is not None and telegram:
            msg = format_intraday_backtest(ledger, a, b, ran, skipped, last_res, mode)
            if not notifier.send(msg):
                log.warning("intraday backtest Telegram message was not delivered")
    elif len(days) > 1 and notifier is not None and telegram:
        a, b = days[0], days[-1]
        msg = format_intraday_backtest(ledger, a, b, ran, skipped, last_res, mode)
        if len(days) > 1:
            msg = f"(Replay {len(days)} calendar days, {ran} sessions with data.)\n" + msg
        if not notifier.send(msg):
            log.warning("intraday replay Telegram message was not delivered")
    return 0, last_res


def cmd_intraday(args) -> int:
    settings = load_settings()
    if settings.mode is Mode.LIVE:
        print("Refusing: intraday paper commands do not run while GROWW_MODE=LIVE.", file=sys.stderr)
        return 2
    cap = _intraday_capital(getattr(args, "capital", None))
    persist = getattr(args, "persist", False)
    telegram = not getattr(args, "no_telegram", False)
    notifier = TelegramNotifier.from_env() if telegram else None

    if args.today:
        now = datetime.now(IST)
        if now.time() < INTRADAY_SESSION_FINAL:
            print("Today's 5m session is not complete before 15:30 IST; run later or use --as-of.", file=sys.stderr)
            return 2
        db = INTRADAY_PAPER_DB if persist else str(DATA_DIR / "intraday_scratch.db")
        rc, _ = _run_intraday_days(
            [now.date()], db, cap, settings, fresh=not persist, write_reports=persist,
            backtest=False, notifier=notifier, telegram=telegram,
        )
        return rc
    if args.as_of:
        d = date.fromisoformat(args.as_of)
        rc, _ = _run_intraday_days(
            [d], str(DATA_DIR / "intraday_scratch_asof.db"), cap, settings, fresh=True,
            write_reports=False, backtest=False, notifier=notifier, telegram=telegram,
        )
        return rc
    if args.replay:
        a, b = (date.fromisoformat(x) for x in args.replay.split(":"))
        from app.data.intraday_store import IntradayStore

        days = _intraday_days_with_data(IntradayStore(INTRADAY_MARKET_DB), a, b)
        if not days:
            print("No 5m NIFTY sessions in that range. Run `fetch-intraday` first.", file=sys.stderr)
            return 2
        db = INTRADAY_PAPER_DB if persist else str(DATA_DIR / f"intraday_replay_{a}_{b}.db")
        rc, _ = _run_intraday_days(
            days, db, cap, settings, fresh=not persist, write_reports=False,
            backtest=True, notifier=notifier, telegram=telegram,
        )
        if not persist:
            print(f"Replay ledger: {db}")
        return rc
    print("choose --today, --as-of or --replay", file=sys.stderr)
    return 2


def cmd_intraday_backtest(args) -> int:
    settings = load_settings()
    if settings.mode is Mode.LIVE:
        print("Refusing: intraday backtest does not run while GROWW_MODE=LIVE.", file=sys.stderr)
        return 2
    from app.data.intraday_store import IntradayStore

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else datetime.now(IST).date()
    days = _intraday_days_with_data(IntradayStore(INTRADAY_MARKET_DB), start, end)
    if not days:
        print("No 5m data in range. Run `fetch-intraday` first.", file=sys.stderr)
        return 2
    cap = _intraday_capital(args.capital)
    telegram = not getattr(args, "no_telegram", False)
    db = str(DATA_DIR / f"intraday_backtest_{start}_{end}.db")
    rc, _ = _run_intraday_days(
        days, db, cap, settings, fresh=True, write_reports=False, backtest=True,
        notifier=TelegramNotifier.from_env() if telegram else None, telegram=telegram,
    )
    print(f"Backtest ledger: {db}")
    return rc


def cmd_intraday_status(args) -> int:
    from app.paper.ledger import Ledger
    from app.paper.report import summary

    if not os.path.exists(INTRADAY_PAPER_DB):
        print("No intraday paper account yet. Use `intraday --replay ... --persist`.")
        return 0
    led = Ledger(INTRADAY_PAPER_DB, _intraday_capital(None))
    print("Intraday paper account:\n" + summary(led))
    for p in led.positions():
        print(f"  {p['symbol']}: {p['qty']} @ {p['entry']:.2f}, stop {p['stop']:.2f}")
    return 0


def cmd_status(args) -> int:
    from app.paper.ledger import Ledger
    from app.paper.report import summary
    if not os.path.exists(PAPER_DB):
        print("No paper account yet. It is created by the first `paper --today` run.")
        return 0
    led = Ledger(PAPER_DB, _capital(None))
    print(summary(led))
    for p in led.positions():
        print(f"  {p['symbol']}: {p['qty']} @ {p['entry']:.2f}, close {p['last_close']:.2f}, stop {p['stop']:.2f}")
    return 0


def main(argv=None) -> int:
    env = load_env_file(str(PROJECT_ROOT / ".env"))
    setup_logging(str(PROJECT_ROOT / "logs"))
    for w in env["warnings"]:
        log.warning(w)
    p = argparse.ArgumentParser(prog="python -m app.run")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    fi = sub.add_parser("fetch-intraday")
    fi.add_argument("--days", type=int, help="calendar days of history (default INTRADAY_FETCH_DAYS or 30)")
    iday = sub.add_parser("intraday")
    ig = iday.add_mutually_exclusive_group(required=True)
    ig.add_argument("--today", action="store_true")
    ig.add_argument("--as-of")
    ig.add_argument("--replay")
    iday.add_argument("--capital", type=float)
    iday.add_argument("--persist", action="store_true", help="write to data/intraday_paper.db instead of scratch")
    iday.add_argument("--no-telegram", action="store_true", help="skip Telegram notifications")
    sub.add_parser("intraday-status")
    ibt = sub.add_parser("intraday-backtest", help="PAPER backtest on 5m bars (scratch ledger + report + Telegram)")
    ibt.add_argument("--start", required=True)
    ibt.add_argument("--end")
    ibt.add_argument("--capital", type=float)
    ibt.add_argument("--no-telegram", action="store_true")
    pp = sub.add_parser("paper")
    g = pp.add_mutually_exclusive_group(required=True)
    g.add_argument("--today", action="store_true")
    g.add_argument("--as-of")
    g.add_argument("--replay")
    g.add_argument("--catch-up", action="store_true")
    pp.add_argument("--capital", type=float)
    sub.add_parser("status")
    sub.add_parser("telegram-test")
    sub.add_parser("telegram-chatid")
    sub.add_parser("daily")
    pb = sub.add_parser("probe", help="read-only: show unusable candles for one symbol")
    pb.add_argument("symbol")
    pb.add_argument("--start", default="2020-01-01")
    sub.add_parser("dashboard")
    sub.add_parser("monitor")
    bt = sub.add_parser("backtest")
    bt.add_argument("--start", required=True)
    bt.add_argument("--end")
    bt.add_argument("--holdout")
    bt.add_argument("--fold-months", type=int, default=6)
    bt.add_argument("--capital", type=float)
    bt.add_argument("--quick", action="store_true", help="skip sensitivity and per-strategy runs")
    a = p.parse_args(argv)
    return {"fetch": cmd_fetch, "fetch-intraday": cmd_fetch_intraday,
            "intraday": cmd_intraday, "intraday-backtest": cmd_intraday_backtest,
            "intraday-status": cmd_intraday_status,
            "paper": cmd_paper, "status": cmd_status, "daily": cmd_daily,
            "backtest": cmd_backtest, "probe": cmd_probe, "dashboard": cmd_dashboard, "monitor": cmd_monitor,
            "telegram-test": cmd_telegram_test, "telegram-chatid": cmd_telegram_chatid}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
