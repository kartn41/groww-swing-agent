#!/usr/bin/env python3
"""Read-only: test which date ranges return usable daily candles for each symbol.

Usage (from project root, with .venv):
  .venv/bin/python scripts/scan_fetch_ranges.py
  .venv/bin/python scripts/scan_fetch_ranges.py --quick
  .venv/bin/python scripts/scan_fetch_ranges.py --ranges "2020-01-01:2025-10-04,2026-03-30:today"

Does not write to market.db. Uses Groww API (paced like fetch).
"""
from __future__ import annotations
import argparse
import os
import sys
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

# project root on path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.config import DATA_DIR, PROJECT_ROOT  # noqa: E402
from app.data.groww_source import GrowwCandleSource, groww_client_from_env, parse_daily_candles  # noqa: E402
from app.env import load_env_file  # noqa: E402
from app.paper.engine import INDEX_SYMBOL, gsym  # noqa: E402
from app.universe import load_universe  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
FINAL = dtime(15, 45)


def last_complete_day(now: datetime) -> date:
    now = now.astimezone(IST)
    return now.date() if now.time() >= FINAL else now.date() - timedelta(days=1)


def parse_ranges(spec: str, today: date) -> list[tuple[str, date, date]]:
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        a, b = part.split(":")
        start = date.fromisoformat(a.strip())
        b = b.strip().lower()
        end = today if b in ("today", "now") else date.fromisoformat(b)
        if start > end:
            start, end = end, start
        label = f"{start}..{end}"
        out.append((label, start, end))
    return out


def check_range(source: GrowwCandleSource, sym: str, start: date, end: date) -> dict:
    skipped: list = []
    total = usable = 0
    bad_chunks = 0
    chunks = 0
    for _s, _e, resp in source.iter_raw(sym, start, end):
        chunks += 1
        candles = (resp or {}).get("candles") or []
        before = len(skipped)
        rows = parse_daily_candles(resp, skipped)
        bad = len(skipped) - before
        t = len(candles)
        total += t
        usable += len(rows)
        if t >= 5 and bad > t * 0.5:
            bad_chunks += 1
    pct = (100.0 * usable / total) if total else 0.0
    ok = total > 0 and bad_chunks == 0 and pct >= 50.0
    return {"ok": ok, "total": total, "usable": usable, "pct": pct, "bad_chunks": bad_chunks, "chunks": chunks}


def main() -> int:
    p = argparse.ArgumentParser(description="Scan which candle date ranges work per symbol (read-only).")
    p.add_argument("--quick", action="store_true", help="NIFTY + first 5 universe symbols only")
    p.add_argument(
        "--ranges",
        default="2020-01-01:2025-10-04,2025-10-05:2026-03-29,2026-03-30:today",
        help="comma-separated start:end pairs; end may be 'today'",
    )
    args = p.parse_args()

    load_env_file(str(PROJECT_ROOT / ".env"))
    today = last_complete_day(datetime.now(IST))
    ranges = parse_ranges(args.ranges, today)
    if not ranges:
        print("No ranges parsed.", file=sys.stderr)
        return 2

    uni_path = DATA_DIR / "universe" / "ind_nifty50list.csv"
    uni, warns = load_universe(str(uni_path))
    for w in warns:
        print("warn:", w)

    syms = [INDEX_SYMBOL] + sorted(gsym(c.symbol) for c in uni)
    if args.quick:
        syms = syms[:6]

    pace = float(os.environ.get("GROWW_DATA_PACE", "0.6"))
    source = GrowwCandleSource(groww_client_from_env(), throttle_s=pace)

    print(f"Today (last complete day): {today}  |  pace: {pace}s  |  symbols: {len(syms)}")
    print("Ranges:", ", ".join(r[0] for r in ranges))
    print()

    # header
    hdr = ["symbol"] + [lbl.split("..")[0][:10] + "…" for lbl, _, _ in ranges]
    print("\t".join(hdr))
    matrix: dict[str, list[bool]] = {}

    for sym in syms:
        row_ok = []
        parts = [sym]
        for label, start, end in ranges:
            end = min(end, today)
            if start > end:
                r = {"ok": False, "total": 0, "usable": 0, "pct": 0.0, "bad_chunks": 0, "chunks": 0}
            else:
                r = check_range(source, sym, start, end)
            row_ok.append(r["ok"])
            mark = "OK" if r["ok"] else "FAIL"
            parts.append(f"{mark}({r['usable']}/{r['total']},{r['bad_chunks']}badCh)")
        matrix[sym] = row_ok
        print("\t".join(parts))

    print()
    for i, (label, _, _) in enumerate(ranges):
        n_ok = sum(1 for sym in syms if matrix[sym][i])
        failed = [sym for sym in syms if not matrix[sym][i]]
        print(f"Range {label}: {n_ok}/{len(syms)} symbols OK")
        if failed and len(failed) <= 15:
            print(f"  failed: {', '.join(failed)}")
        elif failed:
            print(f"  failed: {', '.join(failed[:12])} … (+{len(failed) - 12} more)")

    good_ranges = []
    for i, (label, _, _) in enumerate(ranges):
        if all(matrix[sym][i] for sym in syms):
            good_ranges.append(label)
    print()
    if good_ranges:
        print("RECOMMENDED (all tested symbols OK): fetch these ranges separately:")
        for g in good_ranges:
            print(f"  - {g}")
    else:
        print("RECOMMENDED: no single range passed for ALL symbols; use split fetch (see OK columns above).")
        # intersection: prefix of ranges that are OK for most
        for i, (label, start, end) in enumerate(ranges):
            n_ok = sum(matrix[s][i] for s in syms)
            if n_ok >= len(syms) * 0.9:
                print(f"  ~{n_ok}/{len(syms)} OK for {label} — consider fetch for most names, fix outliers individually.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
