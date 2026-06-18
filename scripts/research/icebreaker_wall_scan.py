"""Icebreaker $0 probe — wall-eating scan on a Bybit ob200 archive day.

Replays a free Bybit historical order-book file (newline-delimited WS messages,
``quote-saver.bycsi.com/orderbook/linear/<SYM>/<date>_<SYM>_ob200.data``) through
the existing BookState, finds resting walls ≥ $threshold, detects when each wall
*ends*, classifies eaten-vs-pulled by whether price passed through it, and
simulates the strategy's TP/SL outcome on the mid series afterwards.

Single-exchange, order-book-only (no trade-absorption confirmation, no fees yet):
the goal is to see whether the core signal has any forward edge on real data, and
to validate the replay/detector pipeline before paying for / collecting more.

Usage:
    python scripts/research/icebreaker_wall_scan.py /path/2026-06-16_BANUSDT_ob200.data \
        --min-notional 100000 --tp 0.005 --sl 0.0015 --entry-delay-ms 200
"""
from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.icebreaker.book import BookState
from src.icebreaker.parse_bybit import parse_orderbook


@dataclass
class WallEvent:
    side: str            # "bid" | "ask"
    price: float
    peak_notional: float
    born_ts: int
    end_ts: int
    eaten: bool          # price passed through (vs pulled)


def scan(path: str, min_notional: float):
    book = BookState()
    active: dict[tuple[str, float], dict] = {}   # (side, price) -> {born_ts, peak_notional}
    events: list[WallEvent] = []
    ts_series: list[int] = []
    mid_series: list[float] = []
    n_msgs = 0

    with open(path) as fh:
        for line in fh:
            msg = json.loads(line)
            ob = parse_orderbook(msg)
            if ob is None:
                continue
            n_msgs += 1
            if ob.kind == "snapshot":
                book.apply_snapshot(ob.bids, ob.asks, ob.update_id)
            else:
                book.apply_delta(ob.bids, ob.asks, ob.update_id)
            ts = ob.exch_ts_ms

            # mid timeline (for forward TP/SL simulation)
            mid = book.mid
            if mid is not None and (not mid_series or mid != mid_series[-1]):
                ts_series.append(ts)
                mid_series.append(mid)

            # current walls ≥ threshold
            cur = {(w.side, w.price): w.notional for w in book.walls(min_notional)}

            # new walls
            for key, notional in cur.items():
                if key not in active:
                    active[key] = {"born_ts": ts, "peak_notional": notional}
                else:
                    active[key]["peak_notional"] = max(active[key]["peak_notional"], notional)

            # ended walls (were active, now below threshold)
            for key in list(active):
                if key in cur:
                    continue
                side, price = key
                info = active.pop(key)
                # eaten = price passed through the wall level
                if side == "bid":
                    eaten = book.best_bid is not None and book.best_bid < price
                else:
                    eaten = book.best_ask is not None and book.best_ask > price
                events.append(WallEvent(side, price, info["peak_notional"],
                                        info["born_ts"], ts, eaten))

    return events, ts_series, mid_series, n_msgs


def simulate(events, ts_series, mid_series, tp, sl, entry_delay_ms, horizon_ms):
    """For each eaten wall, simulate TP/SL on the mid series in the signal dir."""
    def mid_at(ts):
        i = bisect_left(ts_series, ts)
        if i >= len(ts_series):
            return None
        return mid_series[i]

    results = []
    for e in events:
        if not e.eaten:
            continue
        # bid wall eaten -> price broke down -> SHORT ; ask wall eaten -> LONG
        direction = -1 if e.side == "bid" else 1
        entry_ts = e.end_ts + entry_delay_ms
        entry = mid_at(entry_ts)
        if entry is None:
            continue
        tp_px = entry * (1 + direction * tp)
        sl_px = entry * (1 - direction * sl)
        outcome = "open"
        i = bisect_left(ts_series, entry_ts)
        while i < len(ts_series) and ts_series[i] <= entry_ts + horizon_ms:
            px = mid_series[i]
            if direction == 1:
                if px >= tp_px:
                    outcome = "tp"; break
                if px <= sl_px:
                    outcome = "sl"; break
            else:
                if px <= tp_px:
                    outcome = "tp"; break
                if px >= sl_px:
                    outcome = "sl"; break
            i += 1
        results.append((e, outcome))
    return results


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path")
    p.add_argument("--min-notional", type=float, default=100_000.0)
    p.add_argument("--tp", type=float, default=0.005)
    p.add_argument("--sl", type=float, default=0.0015)
    p.add_argument("--entry-delay-ms", type=int, default=200)
    p.add_argument("--horizon-ms", type=int, default=60_000)
    args = p.parse_args()

    events, ts_series, mid_series, n_msgs = scan(args.path, args.min_notional)
    eaten = [e for e in events if e.eaten]
    pulled = [e for e in events if not e.eaten]

    print(f"file: {Path(args.path).name}")
    print(f"messages replayed: {n_msgs}, mid ticks: {len(mid_series)}")
    print(f"walls >= ${args.min_notional:,.0f}: {len(events)}  "
          f"(eaten {len(eaten)}, pulled {len(pulled)})")
    if events:
        by_side = {}
        for e in events:
            by_side.setdefault(e.side, [0, 0])
            by_side[e.side][0] += 1
            by_side[e.side][1] += int(e.eaten)
        for side, (tot, ea) in by_side.items():
            print(f"  {side}: {tot} walls, {ea} eaten")

    results = simulate(events, ts_series, mid_series,
                       args.tp, args.sl, args.entry_delay_ms, args.horizon_ms)
    if not results:
        print("no eaten-wall signals to simulate")
        return
    tp = sum(1 for _, o in results if o == "tp")
    sl = sum(1 for _, o in results if o == "sl")
    op = sum(1 for _, o in results if o == "open")
    n = len(results)
    # gross pnl with their TP/SL, no fees
    gross = tp * args.tp - sl * args.sl
    # net at Bybit taker round-trip 0.11%
    fee = 0.0011
    net = gross - n * fee
    print(f"\nsignals simulated: {n}  (TP {tp}, SL {sl}, open {op})")
    print(f"hit-rate (TP/(TP+SL)): {tp/(tp+sl):.0%}" if (tp+sl) else "  n/a")
    print(f"gross PnL (no fees):   {gross*100:+.2f}%")
    print(f"net PnL (taker 0.11%): {net*100:+.2f}%  over {n} trades")


if __name__ == "__main__":
    main()
