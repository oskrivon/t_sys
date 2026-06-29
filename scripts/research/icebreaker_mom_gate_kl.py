"""Klines version of icebreaker_mom_gate: wide-universe + long-history test of the
CLEAN x mom-HIGH selection edge, run on 1m OHLC klines (light, no OOM, no tape needed).

Entry = open of the bar after the break (= break close, same as tape mom_gate). Managed
exit walks bar HIGH/LOW with the SAME config (buf0.1/tp0.6->BE/tr0.4/h30), pessimistic
intra-bar ordering (adverse extreme checked before favorable). mom-k0 = signed 30m close
return ending at the entry bar (identical definition to the tape mom_gate). Dumps per-setup
rows compatible with icebreaker_selectivity.py (net_trail/gross_trail/mom/clean/month/sym).

Bar-fill is slightly optimistic vs tick; CALIBRATE against the tape mom_gate on the
9-meme x 4-month overlap before trusting the wide numbers.

    python scripts/research/icebreaker_mom_gate_kl.py --klines data/klines_wide \
        --symbols BTCUSDT ETHUSDT ... --dump data/ib_momdump_wide.jsonl
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

CFG = {"buffer": 0.001, "tp1": 0.006, "f1": 0.5, "trail_giveback": 0.004,
       "horizon_ms": 1_800_000}
MOM_WIN = 30


def simulate_exit_bars(bars, ts_index, ts0, side, level, cfg, fee_side):
    """Bar-walk mirror of tape simulate_exit. entry = open of the bar at ts0 (= break
    close). Pessimistic: within a bar the adverse extreme is hit before the favorable."""
    i = ts_index.get(ts0)
    if i is None:
        return None
    entry = bars[i]["open"]
    if entry <= 0:
        return None
    long = side == "long"
    dirn = 1.0 if long else -1.0
    stop0 = level * (1 - cfg["buffer"]) if long else level * (1 + cfg["buffer"])
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
        adverse = lo if long else hi          # worst price in the bar for us
        favor = hi if long else lo            # best price in the bar
        if tp1_done:
            trail = best * (1 - dirn * gb)
            eff = max(trail, entry) if long else min(trail, entry)
            if dirn * (adverse - eff) <= 0:   # trail/BE stop hit
                legs.append((remaining, eff)); remaining = 0.0; reason = "trail"; break
            if dirn * (favor - best) > 0:
                best = favor
        else:
            if dirn * (adverse - stop0) <= 0:  # hard stop hit first (pessimistic)
                legs.append((remaining, stop0)); remaining = 0.0; reason = "stop"; break
            if dirn * (favor - tp1_px) >= 0:   # TP1 reached
                legs.append((f1, tp1_px)); remaining -= f1; tp1_done = True; best = favor
        j += 1
    if remaining > 1e-9:
        legs.append((remaining, last))
    gross = sum(g * dirn * (pp - entry) / entry for g, pp in legs)
    fee_units = 1.0 + sum(g for g, _ in legs)
    return {"gross": gross, "fee_units": fee_units, "net": gross - fee_side * fee_units,
            "mfe": dirn * (best - entry) / entry, "reason": reason}


def spacing(t, sb):
    return sb / (t - 1) if t and t > 1 and sb is not None else 0.0


def is_clean(b):
    return b.get("one_sided", 0) >= 0.9 and spacing(b.get("touches"), b.get("span_bars")) > 60


def load_bars(path):
    bars = [json.loads(l) for l in open(path) if l.strip()]
    bars.sort(key=lambda b: b["ts"])
    return bars


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--klines", required=True, help="dir of <SYM>.jsonl 1m OHLC")
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--bar-ms", type=int, default=60_000)
    p.add_argument("--fee-side", type=float, default=0.00055)
    p.add_argument("--min-bars", type=int, default=400_000, help="coverage filter (~278d)")
    p.add_argument("--dump", required=True)
    args = p.parse_args()
    kdir = Path(args.klines)

    rows, kept_syms, dropped = [], [], []
    for sym in args.symbols:
        path = kdir / f"{sym}.jsonl"
        if not path.exists():
            dropped.append(f"{sym}(missing)"); continue
        bars = load_bars(path)
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
            r = simulate_exit_bars(bars, ts_index, ts0, side, lvl, CFG, args.fee_side)
            if r is None:
                continue
            rows.append({"sym": sym, "ts": ts0,
                         "month": datetime.fromtimestamp(ts0 / 1000, timezone.utc).strftime("%Y-%m"),
                         "mom": mom, "clean": is_clean(bk),
                         "net_trail": r["net"], "gross_trail": r["gross"], "mfe": r["mfe"]})
            nk += 1
        kept_syms.append(sym)
        print(f"  [{sym}] bars={len(bars)} setups={len(bks)} kept={nk}", flush=True)

    with open(args.dump, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"\nkept {len(kept_syms)} coins, {len(rows)} setups -> {args.dump}")
    if dropped:
        print(f"dropped (coverage<{args.min_bars}): {', '.join(dropped)}")


if __name__ == "__main__":
    main()
