"""Experiment: trade WITH the absorber (reverse) vs through it, at maker fees.

Same absorbed-wall signals, but compares the breakout direction (through) against
the fade direction (with the absorber), each at three cost levels:
  taker 0.11% rt / maker 0.04% rt / maker-rebate 0% rt.

One replay pass per coin/day; all configs/directions simulated on the cached
signals.

    python scripts/research/icebreaker_reverse_exp.py --store /root/trading/data/icebreaker \
        --symbols TAOUSDT ZECUSDT SUIUSDT --start 2026-03-01 --end 2026-03-31
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "icebreaker_signal_backtest", Path(__file__).with_name("icebreaker_signal_backtest.py"))
bt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bt
_spec.loader.exec_module(bt)

# (label, simulate kwargs without reverse)
CONFIGS = [
    ("grid TP1.2/SL0.2", dict(tp=0.012, sl=0.002, horizon_ms=600_000)),
    ("time-exit 30s",    dict(tp=0.0, sl=0.0, horizon_ms=60_000, time_exit_ms=30_000)),
    ("time-exit 10s",    dict(tp=0.0, sl=0.0, horizon_ms=60_000, time_exit_ms=10_000)),
]
FEES = [("taker", 0.0011), ("maker", 0.0004), ("mkr-rebate", 0.0)]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--min-notional", type=float, default=100_000.0)
    p.add_argument("--absorb-frac", type=float, default=1.0)
    args = p.parse_args()

    # (config, direction) -> [gross_sum, n]
    acc = defaultdict(lambda: [0.0, 0])
    for symbol in args.symbols:
        for date in bt.daterange(args.start, args.end):
            if not bt._parts(args.store, symbol, date, "book_diff"):
                continue
            ts_s, px_s, by_price = bt.load_trades(args.store, symbol, date)
            sigs = bt.find_signals(bt.iter_book_diffs(args.store, symbol, date),
                                   by_price, args.min_notional, args.absorb_frac)
            for clabel, kw in CONFIGS:
                for dlabel, rev in (("through", False), ("with-absorber", True)):
                    res = bt.simulate(sigs, ts_s, px_s, entry_delay_ms=200,
                                      reverse=rev, **kw)
                    for _, _, pnl in res:
                        acc[(clabel, dlabel)][0] += pnl
                        acc[(clabel, dlabel)][1] += 1
            print(f"[{symbol} {date}] signals={len(sigs)}")

    print("\n" + "=" * 78)
    print(f"  REVERSE EXPERIMENT  ({'+'.join(args.symbols)}, {args.start}..{args.end})")
    print("=" * 78)
    hdr = f"  {'config':18s} {'dir':14s} {'n':>4} {'gross':>8}"
    for fl, _ in FEES:
        hdr += f" {'net@'+fl:>11}"
    print(hdr)
    for clabel, _ in CONFIGS:
        for dlabel in ("through", "with-absorber"):
            g, n = acc[(clabel, dlabel)]
            row = f"  {clabel:18s} {dlabel:14s} {n:>4} {g*100:>+7.2f}%"
            for _, f in FEES:
                row += f" {(g - n*f)*100:>+10.2f}%"
            print(row)


if __name__ == "__main__":
    main()
