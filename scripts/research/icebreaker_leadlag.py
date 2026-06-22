"""Cross-venue lead-lag: does a $100k wall event on KuCoin predict Bybit's move?

The customer's actual premise (described, never backtested by them) is cross-exchange:
a $100k density appears/disappears on a tier-3 venue (KuCoin/MEXC) and you enter on
Bybit. We can't win the sub-second race, so we ask the RETAIL-timescale question:
after a KuCoin wall disappears, does Bybit price move in the predicted direction over
the next 1/3/5 minutes? If yes -> tradeable for us. If flat -> the premise is dead at
our latency too.

  detect (KuCoin lv50 archive)  bid-wall gone -> SHORT, ask-wall gone -> LONG
  measure (our Bybit trade store) signed forward return at each horizon
  baseline                      same-day random timestamps (is the event > noise?)

    python scripts/research/icebreaker_leadlag.py \
        --kc-dir /root/trading/tmp/kc --bybit-store /root/trading/data/icebreaker \
        --pairs TAOUSDTM:TAOUSDT ZECUSDTM:ZECUSDT SUIUSDTM:SUIUSDT \
        --start 2026-03-01 --end 2026-03-07
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import zipfile
from bisect import bisect_left
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


sbt = _load("icebreaker_signal_backtest", "scripts/research/icebreaker_signal_backtest.py")
mc = _load("icebreaker_micro", "scripts/research/icebreaker_micro.py")

MULT = {"TAOUSDTM": 0.01, "ZECUSDTM": 0.01, "SUIUSDTM": 1.0, "SIRENUSDTM": 1.0}


def detect_wall_events(zip_path, mult, wall_usd, drop_frac, cooldown_ms):
    """KuCoin lv50 snapshots -> wall-disappearance events.

    A wall = a single price level whose resting notional >= wall_usd. When a wall
    present in one snapshot drops below drop_frac*wall_usd (eaten or pulled) in a
    later snapshot, emit (ts, side, price, notional). bid-wall gone -> 'short'
    (support broke), ask-wall gone -> 'long' (resistance broke). Coin-level cooldown."""
    z = zipfile.ZipFile(zip_path)
    lines = z.read(z.namelist()[0]).decode("utf-8", "replace").splitlines()
    walls = {"bid": {}, "ask": {}}      # price -> last notional (while >= thr)
    events, last_evt = [], -10 ** 18
    for ln in lines:
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        ts = int(o.get("ts") or o.get("timestamp"))
        for side, book_key, sig in (("bid", "bids", "short"), ("ask", "asks", "long")):
            cur = {}
            for px_s, sz in o.get(book_key, []):
                px = float(px_s)
                notion = px * float(sz) * mult
                if notion >= wall_usd:
                    cur[px] = notion
            # disappearance: was a wall, now gone/shrunk
            for px, notion in walls[side].items():
                still = cur.get(px, 0.0)
                if still < drop_frac * wall_usd and ts - last_evt >= cooldown_ms:
                    events.append((ts, sig, px, notion))
                    last_evt = ts
            walls[side] = cur
    return events


def fwd_returns(ts_arr, pr_arr, t0, horizons_ms):
    """Bybit price return from t0 to t0+h for each horizon (None if out of range)."""
    i = bisect_left(ts_arr, t0)
    if i >= len(ts_arr):
        return None
    p0 = float(pr_arr[i])
    out = []
    for h in horizons_ms:
        j = bisect_left(ts_arr, t0 + h)
        out.append((float(pr_arr[j]) - p0) / p0 if j < len(ts_arr) else None)
    return out


def summarize(name, rows, horizons_ms):
    print(f"\n  {name}  n={len(rows)}")
    for hi, h in enumerate(horizons_ms):
        vals = [r[hi] for r in rows if r[hi] is not None]
        if not vals:
            continue
        vals_sorted = sorted(vals)
        mean = sum(vals) / len(vals)
        med = vals_sorted[len(vals) // 2]
        hit = sum(1 for v in vals if v > 0) / len(vals)
        print(f"    +{h//60000}m  mean={mean*100:+.3f}%  med={med*100:+.3f}%  "
              f"hit={hit:.1%}  n={len(vals)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--kc-dir", required=True, help="dir with <SYM>-orderbooklv50-<date>.zip")
    p.add_argument("--bybit-store", required=True)
    p.add_argument("--pairs", nargs="+", required=True, help="KCSYM:BYBITSYM ...")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--wall-usd", type=float, default=100_000)
    p.add_argument("--drop-frac", type=float, default=0.5)
    p.add_argument("--cooldown-ms", type=int, default=60_000)
    p.add_argument("--horizons", nargs="+", type=int, default=[60, 180, 300],
                   help="forward horizons in seconds")
    p.add_argument("--n-random", type=int, default=2000)
    p.add_argument("--dump", type=Path, default=None)
    args = p.parse_args()

    horizons_ms = [s * 1000 for s in args.horizons]
    kc_dir = Path(args.kc_dir)
    bybit = Path(args.bybit_store)
    rng = np.random.RandomState(42)
    all_signal, all_base, dump = [], [], []

    for pair in args.pairs:
        kc_sym, by_sym = pair.split(":")
        mult = MULT.get(kc_sym, 1.0)
        sig_rows, base_rows, n_events = [], [], 0
        for date in sbt.daterange(args.start, args.end):
            zp = kc_dir / f"{kc_sym}-orderbooklv50-{date}.zip"
            if not zp.exists():
                continue
            if not sbt._parts(bybit, by_sym, date, "trades"):
                print(f"[{by_sym} {date}] no bybit trades", flush=True)
                continue
            t_ts, t_pr, _, _ = mc.load_trades_arr(bybit, by_sym, date)
            if len(t_ts) == 0:
                continue
            events = detect_wall_events(zp, mult, args.wall_usd, args.drop_frac,
                                        args.cooldown_ms)
            n_events += len(events)
            for ts, sig, px, notion in events:
                fr = fwd_returns(t_ts, t_pr, ts, horizons_ms)
                if fr is None:
                    continue
                dirn = 1.0 if sig == "long" else -1.0
                signed = [dirn * x if x is not None else None for x in fr]
                sig_rows.append(signed)
                dump.append({"kc": kc_sym, "date": date, "ts": ts, "sig": sig,
                             "px": px, "notional": notion,
                             "fwd": {f"{h}s": (dirn * fr[i] if fr[i] is not None else None)
                                     for i, h in enumerate(args.horizons)}})
            # baseline: random ts, random direction (signed by same convention)
            if len(t_ts) > 10:
                idx = rng.randint(0, len(t_ts), size=args.n_random)
                dirs = rng.choice([-1.0, 1.0], size=args.n_random)
                for k in range(args.n_random):
                    fr = fwd_returns(t_ts, t_pr, int(t_ts[idx[k]]), horizons_ms)
                    if fr is None:
                        continue
                    base_rows.append([dirs[k] * x if x is not None else None for x in fr])
            print(f"[{by_sym} {date}] wall-events={len(events)}", flush=True)
        print(f"\n=== {kc_sym} -> {by_sym}  events={n_events} ===")
        summarize("SIGNAL (KuCoin wall -> Bybit, in predicted dir)", sig_rows, horizons_ms)
        summarize("BASELINE (random ts/dir)", base_rows, horizons_ms)
        all_signal += sig_rows
        all_base += base_rows

    print("\n" + "=" * 70)
    print("  POOLED (all coins)")
    summarize("SIGNAL", all_signal, horizons_ms)
    summarize("BASELINE", all_base, horizons_ms)
    if args.dump:
        with open(args.dump, "w") as f:
            for r in dump:
                f.write(json.dumps(r) + "\n")
        print(f"\n  dumped {len(dump)} events -> {args.dump}")


if __name__ == "__main__":
    main()
