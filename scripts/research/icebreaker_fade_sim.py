"""Fade (bounce) counter-test of the icebreaker breakout edge.

Hypothesis (from the mombin monotonicity: LOW mom-k0 breaks mean-revert, net-negative as
breakouts): the setups the breakout rule DISCARDS (mom < 2.5%) might be tradeable in the
OPPOSITE direction — fade the weak break, betting the level HOLDS / price reverts.

Same detector + same mom-k0 as icebreaker_mom_gate_kl. For each detected break we run:
  * breakout exit  — existing simulate_exit_bars in the break direction (CALIBRATION: must
                     reproduce the known certified numbers so we trust the harness).
  * fade exit      — mirror managed exit in the OPPOSITE direction, stop at a FIXED distance
                     BEYOND entry (the level is now in our favour, so it can't be the stop).
                     Same tp0.6->BE / trail0.4 / h30 config as the breakout — NOT tuned for
                     the fade, so a positive result is not an overfit.

Entry convention identical to mom_gate_kl (entry = open of the bar at ts_close = break close;
no lookahead). Reports net binned by mom-k0 (the mombin edges) x fee tier, all-coins + CLEAN.

    python scripts/research/icebreaker_fade_sim.py --klines data/klines_2y \
        --symbols BTCUSDT ETHUSDT ... --stop-bufs 0.003 0.005 0.008 --dump data/ib_fade_2y.jsonl
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


major = _load("icebreaker_major", "scripts/research/icebreaker_major.py")
gate = _load("icebreaker_mom_gate_kl", "scripts/research/icebreaker_mom_gate_kl.py")

CFG = {"buffer": 0.001, "tp1": 0.006, "f1": 0.5, "trail_giveback": 0.004,
       "horizon_ms": 1_800_000}
MOM_WIN = 30
MOM_EDGES = [-1e9, 0.0, 0.003, 0.006, 0.010, 0.015, 0.025, 0.050, 1e9]
MOM_LABELS = ["<0", "0-0.3", "0.3-0.6", "0.6-1", "1-1.5", "1.5-2.5", "2.5-5", ">=5"]


def simulate_fade_exit(bars, ts_index, ts0, break_side, cfg, fee_side, stop_buf):
    """Managed exit in the OPPOSITE direction to the break. Stop is a fixed fraction beyond
    entry (break continuation = we're wrong); TP/trail/horizon mirror the breakout config."""
    i = ts_index.get(ts0)
    if i is None:
        return None
    entry = bars[i]["open"]
    if entry <= 0:
        return None
    long = break_side == "short"           # break DOWN -> fade LONG (bounce up); break UP -> fade SHORT
    dirn = 1.0 if long else -1.0
    stop0 = entry * (1 - dirn * stop_buf)  # adverse-direction stop at fixed distance from entry
    tp1_px = entry * (1 + dirn * cfg["tp1"])
    gb, f1 = cfg["trail_giveback"], cfg["f1"]
    t_end = ts0 + cfg["horizon_ms"]
    legs, remaining, tp1_done = [], 1.0, False
    best = entry
    reason = "horizon"
    j, last = i, entry
    while j < len(bars) and bars[j]["ts"] <= t_end:
        hi, lo = bars[j]["high"], bars[j]["low"]
        last = bars[j]["close"]
        adverse = lo if long else hi
        favor = hi if long else lo
        if tp1_done:
            trail = best * (1 - dirn * gb)
            eff = max(trail, entry) if long else min(trail, entry)
            if dirn * (adverse - eff) <= 0:
                legs.append((remaining, eff)); remaining = 0.0; reason = "trail"; break
            if dirn * (favor - best) > 0:
                best = favor
        else:
            if dirn * (adverse - stop0) <= 0:
                legs.append((remaining, stop0)); remaining = 0.0; reason = "stop"; break
            if dirn * (favor - tp1_px) >= 0:
                legs.append((f1, tp1_px)); remaining -= f1; tp1_done = True; best = favor
        j += 1
    if remaining > 1e-9:
        legs.append((remaining, last))
    gross = sum(g * dirn * (pp - entry) / entry for g, pp in legs)
    fee_units = 1.0 + sum(g for g, _ in legs)
    return {"gross": gross, "fee_units": fee_units, "net": gross - fee_side * fee_units,
            "reason": reason, "mfe": dirn * (best - entry) / entry}


def bin_of(mom):
    for k in range(len(MOM_EDGES) - 1):
        if MOM_EDGES[k] <= mom < MOM_EDGES[k + 1]:
            return k
    return len(MOM_LABELS) - 1


def summarize(rows, key, fee_side, label):
    """Print a mom-bin table for the given net key at the given taker/maker fee."""
    print(f"\n=== {label} (fee/side={fee_side*100:.3f}%) ===")
    print(f"{'mom bin':>8} | {'n':>5} | {'net%':>8} | {'WR%':>5} | {'med%':>7} | {'sum%':>7}")
    for k, lab in enumerate(MOM_LABELS):
        sub = [r for r in rows if r["bin"] == k]
        if not sub:
            continue
        nets = [r[key]["gross"] - fee_side * r[key]["fee_units"] for r in sub]
        nets.sort()
        n = len(nets)
        mean = sum(nets) / n
        wr = sum(x > 0 for x in nets) / n * 100
        med = nets[n // 2]
        print(f"{lab:>8} | {n:5d} | {mean*100:+8.3f} | {wr:5.0f} | {med*100:+7.3f} | {sum(nets)*100:+7.1f}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--klines", required=True)
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--bar-ms", type=int, default=60_000)
    p.add_argument("--taker", type=float, default=0.00055)   # VIP0 taker per side
    p.add_argument("--maker", type=float, default=0.0002)    # VIP0 maker per side (fill-optimistic)
    p.add_argument("--stop-bufs", nargs="+", type=float, default=[0.005])
    p.add_argument("--min-bars", type=int, default=300_000)
    p.add_argument("--dump", default="")
    args = p.parse_args()
    kdir = Path(args.klines)

    rows, dropped = [], []
    for sym in args.symbols:
        path = kdir / f"{sym}.jsonl"
        if not path.exists():
            dropped.append(f"{sym}(missing)"); continue
        bars = gate.load_bars(path)
        if len(bars) < args.min_bars:
            dropped.append(f"{sym}({len(bars)})"); continue
        ts_index = {b["ts"]: k for k, b in enumerate(bars)}
        bar_cl = [b["close"] for b in bars]
        bks = major.detect_major_breakouts(
            bars, args.bar_ms, lookback=480, swing_w=5, tol=0.0015, min_touches=4,
            min_span_bars=120, brk=0.0015, cooldown=30, near_bars=30,
            near_tol=0.003, one_sided=0.70)
        nk = 0
        for bk in bks:
            ts0 = bk["ts_close"]; side = bk["side"]; lvl = bk["level"]
            ei = ts_index.get(ts0)
            if ei is None or ei < MOM_WIN:
                continue
            sign = 1.0 if side == "long" else -1.0
            mom = sign * (bar_cl[ei] / bar_cl[ei - MOM_WIN] - 1.0)
            brk = gate.simulate_exit_bars(bars, ts_index, ts0, side, lvl, CFG, 0.0)
            if brk is None:
                continue
            fades = {}
            for sb in args.stop_bufs:
                f = simulate_fade_exit(bars, ts_index, ts0, side, CFG, 0.0, sb)
                if f is None:
                    break
                fades[sb] = f
            if len(fades) != len(args.stop_bufs):
                continue
            rows.append({"sym": sym, "ts": ts0,
                         "month": datetime.fromtimestamp(ts0 / 1000, timezone.utc).strftime("%Y-%m"),
                         "mom": mom, "clean": gate.is_clean(bk), "bin": bin_of(mom),
                         "brk": {"gross": brk["gross"], "fee_units": brk["fee_units"]},
                         "fade": {str(sb): {"gross": fades[sb]["gross"],
                                            "fee_units": fades[sb]["fee_units"]}
                                  for sb in args.stop_bufs}})
            nk += 1
        print(f"  [{sym}] bars={len(bars)} setups={len(bks)} kept={nk}", flush=True)

    print(f"\nTOTAL {len(rows)} setups over {len(args.symbols)-len(dropped)} coins")
    if dropped:
        print(f"dropped: {', '.join(dropped)}")
    if not rows:
        return

    # calibration: breakout net at taker, mom>=2.5%, CLEAN — should match the certified ~+0.10%
    clean_hi = [r for r in rows if r["clean"] and r["mom"] >= 0.025]
    if clean_hi:
        bn = [r["brk"]["gross"] - args.taker * r["brk"]["fee_units"] for r in clean_hi]
        print(f"\nCALIBRATION breakout CLEAN&mom>=2.5% taker: n={len(bn)} "
              f"net={sum(bn)/len(bn)*100:+.3f}% WR={sum(x>0 for x in bn)/len(bn)*100:.0f}%")

    # fade tables, per stop-buf, all-coins then CLEAN
    for sb in args.stop_bufs:
        key = str(sb)
        rr = [{"bin": r["bin"], key: r["fade"][key]} for r in rows]
        summarize(rr, key, args.taker, f"FADE stop={sb*100:.1f}% ALL-coins taker")
        summarize(rr, key, args.maker, f"FADE stop={sb*100:.1f}% ALL-coins maker")
        rc = [{"bin": r["bin"], key: r["fade"][key]} for r in rows if r["clean"]]
        if rc:
            summarize(rc, key, args.taker, f"FADE stop={sb*100:.1f}% CLEAN taker")

    if args.dump:
        with open(args.dump, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        print(f"\ndumped {len(rows)} rows -> {args.dump}")


if __name__ == "__main__":
    main()
