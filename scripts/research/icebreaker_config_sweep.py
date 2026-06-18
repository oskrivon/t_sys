"""Sweep multiple exit configs in ONE replay pass per coin/day.

Signals (find_signals) depend only on min_notional/absorb_frac, not on the exit
rule — so we replay the book once and simulate every config. Tests the configs
from BOTH of the strategy's spreadsheets:
  * description sheet:      TP 0.5% / SL 0.15%
  * wall-analysis grid-best: TP 1.2% / SL 0.2%
  * wall-analysis time-exit: hold 30s / 10s

    python scripts/research/icebreaker_config_sweep.py --store /root/trading/data/icebreaker \
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

# (label, kwargs for simulate)
CONFIGS = [
    ("descr TP0.5/SL0.15", dict(tp=0.005, sl=0.0015, horizon_ms=60_000)),
    ("grid  TP1.2/SL0.2",  dict(tp=0.012, sl=0.002, horizon_ms=600_000)),
    ("grid  TP0.8/SL0.3",  dict(tp=0.008, sl=0.003, horizon_ms=600_000)),
    ("time-exit 30s",      dict(tp=0.0, sl=0.0, horizon_ms=60_000, time_exit_ms=30_000)),
    ("time-exit 10s",      dict(tp=0.0, sl=0.0, horizon_ms=60_000, time_exit_ms=10_000)),
]
FEE = 0.0011


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--min-notional", type=float, default=100_000.0)
    p.add_argument("--absorb-frac", type=float, default=1.0)
    args = p.parse_args()

    # config -> [sum_pnl, n, wins]
    totals = {label: [0.0, 0, 0] for label, _ in CONFIGS}
    for symbol in args.symbols:
        for date in bt.daterange(args.start, args.end):
            if not bt._parts(args.store, symbol, date, "book_diff"):
                continue
            ts_s, px_s, by_price = bt.load_trades(args.store, symbol, date)
            sigs = bt.find_signals(bt.iter_book_diffs(args.store, symbol, date),
                                   by_price, args.min_notional, args.absorb_frac)
            for label, kw in CONFIGS:
                res = bt.simulate(sigs, ts_s, px_s, entry_delay_ms=200, **kw)
                for _, _, pnl in res:
                    totals[label][0] += pnl
                    totals[label][1] += 1
                    totals[label][2] += 1 if pnl > 0 else 0
            print(f"[{symbol} {date}] signals={len(sigs)}")

    print("\n" + "=" * 66)
    print(f"  CONFIG SWEEP  ({'+'.join(args.symbols)}, {args.start}..{args.end}, "
          f"min_notional=${args.min_notional:,.0f})")
    print("=" * 66)
    print(f"  {'config':22s} {'n':>4} {'WR':>5} {'gross':>8} {'net':>8}")
    for label, _ in CONFIGS:
        s, n, w = totals[label]
        net = s - n * FEE
        wr = w / n if n else 0
        print(f"  {label:22s} {n:>4} {wr:>4.0%} {s*100:>+7.2f}% {net*100:>+7.2f}%")


if __name__ == "__main__":
    main()
