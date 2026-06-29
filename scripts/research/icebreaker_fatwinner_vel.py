"""Final-round task (1) NAPKIN, part 2: the momentum/velocity/trend dimension the
geometry/liquidity/time napkin (icebreaker_fatwinner.py) did NOT test. This is the part
the customer actually asked about ("what distinguishes the best setups") — kalman/pre-break
velocity, EMA-stack trend, realized vol, BTC trend.

All features computed on 5m closes STRICTLY BEFORE ts_close (no lookahead), SIGNED by break
side (positive = momentum/trend in the break direction). "Fat" label = top-decile MFE within
(symbol, month) cell, identical to part 1. AUC 0.5 = no separation.

Local data only: major dumps + 5m klines (data/klines{,_apr,_may}) + BTC from klines_major_*.

    python scripts/research/icebreaker_fatwinner_vel.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DUMPS = [
    ("MAR", ROOT / "data" / "ib_major_liq.jsonl", ROOT / "data" / "klines",
     ROOT / "data" / "klines_major_mar" / "BTCUSDT.jsonl"),
    ("APR", ROOT / "data" / "ib_major_apr_liq.jsonl", ROOT / "data" / "klines_apr",
     ROOT / "data" / "klines_major_apr" / "BTCUSDT.jsonl"),
    ("MAY", ROOT / "data" / "ib_major_may_liq.jsonl", ROOT / "data" / "klines_may",
     ROOT / "data" / "klines_major_may" / "BTCUSDT.jsonl"),
]
TOP_Q = 0.10


def kalman_vel(close):
    """Constant-velocity 1D Kalman -> per-bar velocity (price units/bar). No filterpy."""
    F = np.array([[1.0, 1.0], [0.0, 1.0]])
    H = np.array([[1.0, 0.0]])
    R = np.array([[1.0]])
    Q = np.array([[0.01, 0.0], [0.0, 0.01]])
    x = np.array([[close[0]], [0.0]])
    P = np.eye(2) * 100.0
    vel = np.zeros(len(close))
    for i, z in enumerate(close):
        x = F @ x
        P = F @ P @ F.T + Q
        y = np.array([[z]]) - H @ x
        S = H @ P @ H.T + R
        K = P @ H.T @ np.linalg.inv(S)
        x = x + K @ y
        P = (np.eye(2) - K @ H) @ P
        vel[i] = x[1, 0]
    return vel


def load_closes(path):
    ts, cl = [], []
    for line in open(path):
        if not line.strip():
            continue
        r = json.loads(line)
        ts.append(r["ts"]); cl.append(r["close"])
    return np.array(ts), np.array(cl)


def auc(pos, neg):
    pos = [v for v in pos if v == v]; neg = [v for v in neg if v == v]
    if not pos or not neg:
        return float("nan")
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    ranks = {}
    i = 0
    while i < len(allv):
        j = i
        while j < len(allv) and allv[j][0] == allv[i][0]:
            j += 1
        r = (i + j - 1) / 2.0 + 1.0
        for k in range(i, j):
            ranks[k] = r
        i = j
    rs = sum(ranks[idx] for idx, (_, lab) in enumerate(allv) if lab == 1)
    n1 = len(pos)
    return (rs - n1 * (n1 + 1) / 2.0) / (n1 * len(neg))


def feats_at(idx, cl, vel, btc_cl, btc_idx, sign):
    """Ex-ante features at break bar idx (all bars <= idx), signed by break side."""
    f = {}
    if idx < 12:
        return None
    c = cl[idx]
    f["mom15"] = sign * (c / cl[idx - 3] - 1)        # 3×5m = 15m pre-break return
    f["mom30"] = sign * (c / cl[idx - 6] - 1)
    f["mom60"] = sign * (c / cl[idx - 12] - 1)
    f["kvel"] = sign * vel[idx] / c                   # kalman velocity, price-normed
    f["kaccel"] = sign * (vel[idx] - vel[idx - 3]) / c
    signs = np.sign(vel[idx - 10:idx + 1])
    f["kconsist"] = abs(signs.mean())                 # trend persistence (unsigned)
    rets = np.diff(cl[idx - 12:idx + 1]) / cl[idx - 12:idx]
    f["rvol"] = float(np.std(rets))                   # realized vol (unsigned)
    ema_f = _ema(cl[max(0, idx - 30):idx + 1], 6)
    ema_s = _ema(cl[max(0, idx - 30):idx + 1], 24)
    f["ema_stack"] = sign * (ema_f - ema_s) / c       # fast-vs-slow trend alignment
    if btc_idx is not None and btc_idx >= 6:
        f["btc_mom30"] = sign * (btc_cl[btc_idx] / btc_cl[btc_idx - 6] - 1)
    else:
        f["btc_mom30"] = float("nan")
    return f


def _ema(arr, span):
    a = 2.0 / (span + 1)
    e = arr[0]
    for v in arr[1:]:
        e = a * v + (1 - a) * e
    return e


FEATURES = ["mom15", "mom30", "mom60", "kvel", "kaccel", "kconsist", "rvol",
            "ema_stack", "btc_mom30"]


def main():
    rows = []
    for tag, dump, kdir, btc_path in DUMPS:
        if not dump.exists():
            print(f"MISSING {dump}"); continue
        recs = [json.loads(l) for l in open(dump) if l.strip()]
        for r in recs:
            r["month"] = tag
        # group by symbol -> load that coin's 5m closes once
        btc_ts, btc_cl = load_closes(btc_path) if btc_path.exists() else (None, None)
        by_sym = defaultdict(list)
        for r in recs:
            by_sym[r["symbol"]].append(r)
        for sym, srecs in by_sym.items():
            kpath = kdir / f"{sym}.jsonl"
            if not kpath.exists():
                continue
            ts, cl = load_closes(kpath)
            vel = kalman_vel(cl)
            for r in srecs:
                idx = int(np.searchsorted(ts, r["ts_close"], side="right") - 1)
                if idx < 0 or idx >= len(cl):
                    continue
                bidx = (int(np.searchsorted(btc_ts, r["ts_close"], side="right") - 1)
                        if btc_ts is not None else None)
                sign = 1.0 if r["side"] == "long" else -1.0
                f = feats_at(idx, cl, vel, btc_cl, bidx, sign)
                if f is None:
                    continue
                r.update(f)
                rows.append(r)
    print(f"joined {len(rows)} setups with velocity features")

    cells = defaultdict(list)
    for r in rows:
        cells[(r["symbol"], r["month"])].append(r)
    for cr in cells.values():
        cr.sort(key=lambda r: r["mfe"], reverse=True)
        nf = max(1, int(round(len(cr) * TOP_Q)))
        for i, r in enumerate(cr):
            r["fat"] = 1 if i < nf else 0

    fat = [r for r in rows if r["fat"]]
    rest = [r for r in rows if not r["fat"]]
    print(f"fat = {len(fat)}  rest = {len(rest)}\n")
    print(f"  {'feature':>11s} {'AUC_pool':>9s} {'MAR':>6s} {'APR':>6s} {'MAY':>6s}  "
          f"{'fat_med':>11s} {'rest_med':>11s}")
    for f in FEATURES:
        ap = auc([r.get(f) for r in fat], [r.get(f) for r in rest])
        per = []
        for tag, *_ in DUMPS:
            per.append(auc([r.get(f) for r in fat if r["month"] == tag],
                           [r.get(f) for r in rest if r["month"] == tag]))
        fv = sorted(r[f] for r in fat if r.get(f) == r.get(f))
        rv = sorted(r[f] for r in rest if r.get(f) == r.get(f))
        fm = fv[len(fv) // 2] if fv else float("nan")
        rm = rv[len(rv) // 2] if rv else float("nan")
        print(f"  {f:>11s} {ap:9.3f} {per[0]:6.2f} {per[1]:6.2f} {per[2]:6.2f}  "
              f"{fm:11.5f} {rm:11.5f}")


if __name__ == "__main__":
    main()
