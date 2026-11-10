"""ETF commands for app.run: etf-daily, etf-monitor, etf-status, etf-backtest.
PAPER by default. LIVE needs GROWW_MODE=LIVE + data/LIVE_ARMED + ETF_LIVE_ENABLED=true + the static IP."""
from __future__ import annotations
import logging
import os
import sys
from datetime import date, datetime, timedelta

from app.config import DATA_DIR, Mode, load_settings
from app.execution.executor import kill_switch_active
from app.notify import TelegramNotifier

log = logging.getLogger("app.etf")
ETF_PAPER_DB = str(DATA_DIR / "etf_paper.db")
ETF_LIVE_DB = str(DATA_DIR / "etf_live.db")


def etf_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("ETF_ENABLED", "true")).strip().lower() in ("1", "true", "on", "yes")


def etf_groww_symbols() -> list[str]:
    from app.etf.config import load_etf_config
    return [f"NSE-{s}" for s in load_etf_config().symbol_list] if etf_enabled() else []


def _mode(settings) -> tuple[bool, str]:
    """(live?, refusal reason or '')."""
    if settings.mode is not Mode.LIVE:
        return False, ""
    if os.environ.get("ETF_LIVE_ENABLED", "false").strip().lower() != "true":
        return False, "GROWW_MODE=LIVE but ETF_LIVE_ENABLED is not true: refusing (paper ETF runs need GROWW_MODE=PAPER)"
    return True, ""


def _frames(store, symbols, end: date) -> dict:
    from app.etf.analysis import sanitize
    out = {}
    for s in symbols:
        df = store.load(f"NSE-{s}", end=end)
        out[s] = sanitize(df)[0] if len(df) else df
    return out


def etf_eod(now: datetime, notifier=None) -> int:
    """Process every completed session since the last one (first run: only the latest)."""
    from app.data.store import CandleStore
    from app.etf.config import load_etf_config
    from app.etf.engine import end_of_day, simulate_session
    from app.etf.ledger import EtfLedger
    from app.etf.report import format_eod, render_md
    from app.run import FINAL_AFTER, MARKET_DB, REPORTS
    settings = load_settings()
    live, why = _mode(settings)
    if why:
        print(why, file=sys.stderr)
        return 2
    cfg = load_etf_config()
    syms = cfg.symbol_list
    store = CandleStore(MARKET_DB)
    cutoff = now.date() if now.time() >= FINAL_AFTER else now.date() - timedelta(days=1)
    days = sorted({d for s in syms for d in store.load(f"NSE-{s}", end=cutoff).index})
    ledger = EtfLedger(ETF_LIVE_DB if live else ETF_PAPER_DB, cfg.capital)
    last = ledger.get("last_eod")
    todo = [d for d in days if last is None or d > date.fromisoformat(last)]
    if last is None:
        todo = todo[-1:]
    if not todo:
        print("ETF account is up to date (no new completed session).")
        return 0
    REPORTS.mkdir(exist_ok=True)
    mode = "LIVE" if live else "PAPER"
    for i, d in enumerate(todo):
        frames = _frames(store, syms, d)
        note = ""
        ran = (ledger.get("last_cycle") or "")[:10] == d.isoformat()
        if not live and not ran and (ledger.positions() or ledger.open_orders()):
            simulate_session(d, ledger, frames, cfg)
            note = ("The market-hours monitor did not run on this day: the session was simulated from the daily "
                    "candle (stops/targets/planned buys).")
        bench = float(frames[syms[0]]["close"].iloc[-1]) if len(frames[syms[0]]) else None
        try:
            res = end_of_day(d, ledger, frames, cfg, kill_switch=kill_switch_active(settings), benchmark=bench)
            ledger.commit()
        except Exception:
            ledger.con.rollback()
            raise
        (REPORTS / f"etf_{d}.md").write_text(render_md(res, ledger, mode, note))
        print(f"ETF {d}: {len(res.new_orders)} planned, {len(res.exits_flagged)} exits flagged, equity Rs{res.equity:,.0f}")
        if notifier is not None and i == len(todo) - 1:
            msg = format_eod(res, ledger, mode, note)
            if len(todo) > 1:
                msg = f"(Caught up {len(todo)} sessions: {todo[0]} to {d}.)\n" + msg
            if not notifier.send(msg):
                log.warning("ETF Telegram report not delivered (file saved)")
    return 0


def cmd_etf_daily(args) -> int:
    from app.run import IST, FetchBusy, _alert_fetch_errors, _fetch
    notifier = TelegramNotifier.from_env()
    try:
        if not args.no_fetch:
            try:
                s = _fetch(include_stocks=False)
                _alert_fetch_errors(s)
            except FetchBusy:
                print("another fetch is running; using stored candles", file=sys.stderr)
        return etf_eod(datetime.now(IST), notifier)
    except Exception as e:
        log.exception("ETF daily failed")
        if notifier:
            notifier.alert(f"ETF daily job FAILED: {type(e).__name__}. Check logs/agent.log.")
        return 1


def cmd_etf_monitor(args) -> int:
    from app.data.groww_source import groww_access_token_from_env
    from app.data.store import CandleStore
    from app.etf.config import load_etf_config
    from app.etf.intraday import LiveExecution, PaperExecution, run_cycle
    from app.etf.ledger import EtfLedger
    from app.etf.market import GrowwMarket
    from app.etf.report import format_cycle
    from app.run import IST, MARKET_DB
    settings = load_settings()
    live, why = _mode(settings)
    if why:
        print(why, file=sys.stderr)
        return 2
    notifier = TelegramNotifier.from_env()
    cfg = load_etf_config()
    from app.etf.intraday import PROTECT_END, PROTECT_START
    now = datetime.now(IST)
    if now.weekday() >= 5 or not (PROTECT_START <= now.time() <= PROTECT_END):
        print(f"outside market monitoring hours ({PROTECT_START:%H:%M}-{PROTECT_END:%H:%M} IST, Mon-Fri)")
        return 0
    try:
        if live:
            from app.broker.groww_adapter import GrowwBroker
            from app.net_guard import check_static_ip
            ok, msg = check_static_ip(os.environ.get("GROWW_STATIC_IP"))
            if not ok:
                if notifier:
                    notifier.alert(f"ETF monitor (LIVE) did not run: {msg}")
                print(msg, file=sys.stderr)
                return 2
            broker = GrowwBroker(settings, groww_access_token_from_env())
            market, execu = GrowwMarket(broker._g), LiveExecution(broker, cfg)
        else:
            from growwapi import GrowwAPI
            market, execu = GrowwMarket(GrowwAPI(groww_access_token_from_env())), PaperExecution(cfg)
        ledger = EtfLedger(ETF_LIVE_DB if live else ETF_PAPER_DB, cfg.capital)
        store = CandleStore(MARKET_DB)
        prev, stale = {}, []
        for s in cfg.symbol_list:
            df = store.load(f"NSE-{s}", end=now.date() - timedelta(days=1))
            if len(df):
                prev[s] = float(df["close"].iloc[-1])
                if (now.date() - df.index[-1]).days > 4:
                    stale.append(f"{s} last daily close is from {df.index[-1]}")
        res = run_cycle(now, ledger, market, execu, cfg, prev, kill_switch=kill_switch_active(settings))
        res.alerts += [f"stale reference price: {x} (run etf-daily)" for x in stale]
    except Exception as e:
        log.exception("ETF monitor failed")
        if notifier:
            notifier.alert(f"ETF monitor FAILED at {now:%H:%M}: {type(e).__name__}. Holdings were NOT checked this cycle.")
        return 1
    if res.skipped:
        print(f"skipped: {res.skipped}")
        return 0
    for sym, q in res.quotes.items():
        print(f"{sym} ltp {q.ltp:.2f} bid {q.bid} ask {q.ask}")
    for k, v in res.waiting.items():
        print(f"WAIT {k}: {v}")
    msg = format_cycle(res, "LIVE" if live else "PAPER")
    if msg:
        print(msg)
        if notifier:
            notifier.send(msg)
    return 0


def cmd_etf_status(args) -> int:
    from app.etf.config import load_etf_config
    from app.etf.ledger import EtfLedger
    live, _ = _mode(load_settings())
    db = ETF_LIVE_DB if live else ETF_PAPER_DB
    if not os.path.exists(db):
        print("No ETF account yet. It is created by the first `etf-daily` run.")
        return 0
    cfg = load_etf_config()
    led = EtfLedger(db, cfg.capital)
    start = led.getf("start_capital")
    eq = led.equity_now()
    print(f"{'LIVE' if live else 'PAPER'} ETF account: equity Rs{eq:,.0f} ({eq / start - 1:+.2%}), cash Rs{led.getf('cash'):,.0f}, "
          f"unsettled Rs{led.getf('unsettled'):,.0f}, realized Rs{led.realized():+,.0f}, last close processed "
          f"{led.get('last_eod')}, last monitor cycle {led.get('last_cycle')}")
    for p in led.positions():
        print(f"  {p['symbol']}: {p['qty']} @ avg {p['avg']:.2f} ({p['tranches']}/3 tranches), last {p['last_close']:.2f}, "
              f"stop {p['stop']:.2f}, target {p['target']:.2f}" + (f", EXIT PENDING {p['exit_pending']}" if p["exit_pending"] else ""))
        for t in led.tranches(p["symbol"]):
            print(f"     T{t['tranche_no']} {t['kind']} {t['qty']} @ {t['price']:.2f} on {t['d']}: {t['reason'][:90]}")
    for o in led.open_orders():
        print(f"  ORDER {o['ref']} {o['side']} {o['qty']} {o['symbol']} @ {o['limit_price']:.2f} [{o['status']}]")
    return 0


def cmd_etf_backtest(args) -> int:
    from app.data.store import CandleStore
    from app.etf import backtest as bt
    from app.etf.config import load_etf_config
    from app.run import IST, MARKET_DB, REPORTS
    cfg = load_etf_config()
    store = CandleStore(MARKET_DB)
    frames = _frames(store, cfg.symbol_list, date(2100, 1, 1))
    missing = [s for s, f in frames.items() if len(f) < 250]
    if missing:
        print(f"Not enough daily candles for {missing}. Run `python -m app.run etf-daily` (it fetches) first.",
              file=sys.stderr)
        return 2
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else max(max(f.index) for f in frames.values())
    hold = date.fromisoformat(args.holdout) if args.holdout else None
    dev_end = date.fromordinal(hold.toordinal() - 1) if hold else end
    sections = []
    groups = [([s], s) for s in cfg.symbol_list] + ([(cfg.symbol_list, "Combined portfolio")]
                                                   if len(cfg.symbol_list) > 1 else [])
    for syms, name in groups:
        print(f"backtesting {name}...")
        dev = bt.evaluate(frames, syms, start, dev_end, cfg)
        test = bt.evaluate(frames, syms, hold, end, cfg) if hold else None
        folds = bt.walk_forward(frames, syms, start, dev_end, cfg, args.fold_months)
        sections.append((name, dev, test, folds))
    sens = {} if args.quick else bt.sensitivity(frames, cfg.symbol_list, start, dev_end, cfg)
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"etf_backtest_{datetime.now(IST).strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(bt.render(sections, sens, cfg.capital))
    print(f"report: {path}")
    lines = []
    for name, dev, test, folds in sections:
        t = test or {}
        lines.append(f"{name}: dev {dev.get('total_return', 0):+.2%} ({dev.get('trades', 0)} trades, PF "
                     f"{dev.get('profit_factor') or 0:.2f}, max DD {dev.get('max_drawdown', 0):.1%}); holdout "
                     f"{t.get('total_return', 0):+.2%} ({t.get('trades', 0)} trades, PF {t.get('profit_factor') or 0:.2f})")
        for f in bt.verdict(dev, test, folds):
            lines.append(f"  - {f}")
    print("\n".join(lines))
    if args.telegram:
        n = TelegramNotifier.from_env()
        if n:
            n.send("ETF BACKTEST (simulation, not real trades)\n" + "\n".join(lines) + f"\nFull report: {path.name}")
    return 0


def add_parsers(sub) -> dict:
    d = sub.add_parser("etf-daily", help="after the close: fetch ETF candles, review positions, plan next session")
    d.add_argument("--no-fetch", action="store_true")
    sub.add_parser("etf-monitor", help="market hours, every 15 min: sudden-fall/stop/target checks + planned buys")
    sub.add_parser("etf-status")
    b = sub.add_parser("etf-backtest")
    b.add_argument("--start", default="2021-01-01")
    b.add_argument("--end")
    b.add_argument("--holdout", help="untouched out-of-sample period starts here, e.g. 2024-07-01")
    b.add_argument("--fold-months", type=int, default=6)
    b.add_argument("--quick", action="store_true")
    b.add_argument("--telegram", action="store_true")
    return {"etf-daily": cmd_etf_daily, "etf-monitor": cmd_etf_monitor, "etf-status": cmd_etf_status,
            "etf-backtest": cmd_etf_backtest}
