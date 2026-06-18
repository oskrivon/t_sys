"""Faithful icebreaker backtest on the unified parquet store (Bybit, free archive).

Implements the strategy's actual signal:
  1. Wall = a book level with notional >= threshold.
  2. When the wall disappears, sum *trade* volume at that price in the eating
     direction over the wall's lifetime. If it covers >= `absorb_frac` of the
     wall's peak size, the wall was ABSORBED (not pulled) -> signal.
  3. bid wall absorbed by sells -> SHORT ; ask wall absorbed by buys -> LONG.
  4. Enter `entry_delay_ms` after the event at the next trade price; exit on
     TP/SL walked over the real trade tape; net at Bybit taker round-trip.

Reads day-partitioned book_diff + trades written by the downloader / collector,
so it runs identically on archive and live-collected data.

    python scripts/research/icebreaker_signal_backtest.py \
        --store /root/trading/data/icebreaker --symbols TAOUSDT SIRENUSDT \
        --start 2026-03-01 --end 2026-03-31 --min-notional 100000
"""
from __future__ import annotations

import argparse
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.icebreaker.book import BookState


def daterange(start: str, end: str):
    d = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    while d <= d1:
        yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def _parts(store: Path, symbol: str, date: str, kind: str):
    p = store / "exchange=bybit" / f"symbol={symbol}" / f"date={date}" / kind
    return sorted(p.glob("part-*.parquet"))


def load_book_diffs(store, symbol, date):
    """Ordered (recv_ts_ns, update_id, kind, side, price, qty) rows."""
    rows = []
    for f in _parts(store, symbol, date, "book_diff"):
        t = pq.read_table(f, columns=["recv_ts_ns", "exch_ts_ms", "update_id",
                                      "kind", "side", "price", "qty"])
        d = t.to_pydict()
        for i in range(t.num_rows):
            rows.append((d["exch_ts_ms"][i], d["update_id"][i], d["kind"][i],
                         d["side"][i], d["price"][i], d["qty"][i]))
    return rows


def load_trades(store, symbol, date):
    """Return (ts_sorted, px_sorted) global path + price->(ts,side,qty) index."""
    ts_all, px_all = [], []
    by_price: dict[float, list] = defaultdict(list)
    for f in _parts(store, symbol, date, "trades"):
        t = pq.read_table(f, columns=["exch_ts_ms", "price", "qty", "side"])
        d = t.to_pydict()
        for i in range(t.num_rows):
            ts, px, qty, side = (d["exch_ts_ms"][i], d["price"][i],
                                 d["qty"][i], d["side"][i])
            ts_all.append(ts)
            px_all.append(px)
            by_price[round(px, 10)].append((ts, side, qty))
    order = sorted(range(len(ts_all)), key=lambda i: ts_all[i])
    ts_sorted = [ts_all[i] for i in order]
    px_sorted = [px_all[i] for i in order]
    for k in by_price:
        by_price[k].sort()
    return ts_sorted, px_sorted, by_price


@dataclass
class Signal:
    side: str
    price: float
    peak_size: float
    absorbed_qty: float
    born_ts: int
    end_ts: int


def find_signals(book_rows, by_price, min_notional, absorb_frac):
    book = BookState()
    active: dict[tuple, dict] = {}
    signals: list[Signal] = []
    cur_snap_uid = None

    def eaten_volume(side, price, t0, t1):
        eat_side = "sell" if side == "bid" else "buy"
        lst = by_price.get(round(price, 10))
        if not lst:
            return 0.0
        lo = bisect_left(lst, (t0, "", 0.0))
        hi = bisect_right(lst, (t1, "\xff", float("inf")))
        return sum(q for _, s, q in lst[lo:hi] if s == eat_side)

    for ts, uid, kind, side, price, qty in book_rows:
        if kind == "snapshot" and uid != cur_snap_uid:
            # new full snapshot group -> reset the book and pending walls
            book.bids.clear(); book.asks.clear(); active.clear()
            cur_snap_uid = uid
            book.seeded = True
        book._apply_level(book.bids if side == "bid" else book.asks, price, qty)
        book.last_update_id = uid

        # is this level a wall now?
        sz = (book.bids if side == "bid" else book.asks).get(price, 0.0)
        notional = price * sz
        key = (side, price)
        if notional >= min_notional:
            info = active.get(key)
            if info is None:
                active[key] = {"born_ts": ts, "peak_size": sz}
            else:
                info["peak_size"] = max(info["peak_size"], sz)
        elif key in active:
            info = active.pop(key)
            vol = eaten_volume(side, price, info["born_ts"], ts)
            if vol >= absorb_frac * info["peak_size"]:
                signals.append(Signal(side, price, info["peak_size"], vol,
                                      info["born_ts"], ts))
    return signals


def simulate(signals, ts_sorted, px_sorted, tp, sl, entry_delay_ms, horizon_ms):
    out = []
    for s in signals:
        direction = -1 if s.side == "bid" else 1  # bid eaten -> short
        i = bisect_left(ts_sorted, s.end_ts + entry_delay_ms)
        if i >= len(ts_sorted):
            continue
        entry = px_sorted[i]
        tp_px = entry * (1 + direction * tp)
        sl_px = entry * (1 - direction * sl)
        outcome, t_end = "open", s.end_ts + entry_delay_ms + horizon_ms
        j = i
        while j < len(ts_sorted) and ts_sorted[j] <= t_end:
            px = px_sorted[j]
            if direction == 1:
                if px >= tp_px: outcome = "tp"; break
                if px <= sl_px: outcome = "sl"; break
            else:
                if px <= tp_px: outcome = "tp"; break
                if px >= sl_px: outcome = "sl"; break
            j += 1
        out.append((s, outcome))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--min-notional", type=float, default=100_000.0)
    p.add_argument("--absorb-frac", type=float, default=1.0)
    p.add_argument("--tp", type=float, default=0.005)
    p.add_argument("--sl", type=float, default=0.0015)
    p.add_argument("--entry-delay-ms", type=int, default=200)
    p.add_argument("--horizon-ms", type=int, default=60_000)
    p.add_argument("--fee", type=float, default=0.0011)
    args = p.parse_args()

    all_results = []
    per_symbol = defaultdict(lambda: [0, 0, 0])  # tp, sl, open
    for symbol in args.symbols:
        for date in daterange(args.start, args.end):
            if not _parts(args.store, symbol, date, "book_diff"):
                continue
            book_rows = load_book_diffs(args.store, symbol, date)
            ts_s, px_s, by_price = load_trades(args.store, symbol, date)
            sigs = find_signals(book_rows, by_price, args.min_notional, args.absorb_frac)
            res = simulate(sigs, ts_s, px_s, args.tp, args.sl,
                           args.entry_delay_ms, args.horizon_ms)
            for _, o in res:
                idx = {"tp": 0, "sl": 1, "open": 2}[o]
                per_symbol[symbol][idx] += 1
            all_results.extend(res)
            print(f"[{symbol} {date}] walls-absorbed={len(sigs)} "
                  f"signals={len(res)}")

    n = len(all_results)
    tp = sum(1 for _, o in all_results if o == "tp")
    sl = sum(1 for _, o in all_results if o == "sl")
    op = n - tp - sl
    gross = tp * args.tp - sl * args.sl
    net = gross - n * args.fee
    print("\n" + "=" * 60)
    print(f"  ICEBREAKER FAITHFUL BACKTEST  (min_notional=${args.min_notional:,.0f}, "
          f"absorb>={args.absorb_frac:.0%}, TP {args.tp:.2%}/SL {args.sl:.2%})")
    print("=" * 60)
    for sym, (a, b, c) in per_symbol.items():
        tot = a + b + c
        wr = a / (a + b) if (a + b) else 0
        print(f"  {sym:12s} signals={tot:4d}  TP={a:3d} SL={b:3d} open={c:3d}  WR={wr:.0%}")
    print(f"\n  TOTAL signals: {n}  (TP {tp}, SL {sl}, open {op})")
    if tp + sl:
        print(f"  hit-rate: {tp/(tp+sl):.1%}")
    print(f"  gross PnL: {gross*100:+.2f}%")
    print(f"  net PnL (fee {args.fee:.2%}/trade): {net*100:+.2f}%  over {n} trades")


if __name__ == "__main__":
    main()
