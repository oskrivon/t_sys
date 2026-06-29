"""Does the CLEAN x mom edge track volatility? Per (coin, month) cell: realized vol vs
realized net of the selected setups -> Pearson + vol-tercile buckets.

Echoes Phase 3 (good-rate ~ vol, Pearson +0.61) but on the EXTRACTABLE edge (realized net),
not just MFE good-rate. Confirms whether the edge is a vol play (-> regime/vol gate) and how
much of the per-month swing is vol.

    python scripts/research/icebreaker_vol_pearson.py \
        --dump data/ib_momdump_wide.jsonl --klines data/klines_wide --topq 0.33
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def rvol_month(closes):
    """Annualized-ish realized vol: std of 1m log returns * sqrt(1440) * 100 (daily %)."""
    cl = [c for c in closes if c > 0]
    if len(cl) < 30:
        return None
    r = [math.log(cl[k] / cl[k - 1]) for k in range(1, len(cl)) if cl[k - 1] > 0]
    if len(r) < 30:
        return None
    m = sum(r) / len(r)
    v = sum((x - m) ** 2 for x in r) / len(r)
    return math.sqrt(v) * math.sqrt(1440) * 100


def pearson(xs, ys, ws=None):
    n = len(xs)
    if n < 3:
        return float("nan")
    if ws is None:
        ws = [1.0] * n
    sw = sum(ws)
    mx = sum(w * x for w, x in zip(ws, xs)) / sw
    my = sum(w * y for w, y in zip(ws, ys)) / sw
    cov = sum(w * (x - mx) * (y - my) for w, x, y in zip(ws, xs, ys))
    vx = sum(w * (x - mx) ** 2 for w, x in zip(ws, xs))
    vy = sum(w * (y - my) ** 2 for w, y in zip(ws, ys))
    return cov / math.sqrt(vx * vy) if vx > 0 and vy > 0 else float("nan")


def month_of(ts):
    # ts ms -> "YYYY-MM" (UTC) without datetime import churn
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts / 1000, _dt.timezone.utc).strftime("%Y-%m")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    ap.add_argument("--klines", required=True)
    ap.add_argument("--topq", type=float, default=0.33)
    ap.add_argument("--min-clean", type=int, default=8, help="min clean setups/cell to include")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.dump) if l.strip()]
    # per-(sym,month) realized vol from klines closes grouped by month
    kdir = Path(args.klines)
    syms = sorted(set(r["sym"] for r in rows))
    vol = {}
    for sym in syms:
        p = kdir / f"{sym}.jsonl"
        if not p.exists():
            continue
        bym = defaultdict(list)
        for line in open(p):
            if line.strip():
                b = json.loads(line)
                bym[month_of(b["ts"])].append(b["close"])
        for mo, cl in bym.items():
            v = rvol_month(cl)
            if v is not None:
                vol[(sym, mo)] = v

    # per-cell edge = mean net of CLEAN top-q by mom
    cells = defaultdict(list)
    for r in rows:
        if r["clean"]:
            cells[(r["sym"], r["month"])].append(r)

    pts = []  # (vol, edge, n, gross, sym, month)
    for key, rs in cells.items():
        if key not in vol or len(rs) < args.min_clean:
            continue
        rs = sorted(rs, key=lambda r: r["mom"], reverse=True)
        k = max(1, int(round(len(rs) * args.topq)))
        sel = rs[:k]
        edge = sum(r["net_trail"] for r in sel) / len(sel)
        gross = sum(r["gross_trail"] for r in sel) / len(sel)
        pts.append((vol[key], edge, len(sel), gross, key[0], key[1]))

    if len(pts) < 3:
        print(f"only {len(pts)} cells, need >=3"); return
    vols = [p[0] for p in pts]
    edges = [p[1] for p in pts]
    grosses = [p[3] for p in pts]
    ns = [p[2] for p in pts]

    print(f"cells={len(pts)}  (coin x month, clean>={args.min_clean}, edge=CLEAN top-{args.topq:.0%} net)")
    print(f"Pearson(vol, net)   unweighted = {pearson(vols, edges):+.3f}   "
          f"setup-weighted = {pearson(vols, edges, ns):+.3f}")
    print(f"Pearson(vol, gross) unweighted = {pearson(vols, grosses):+.3f}   "
          f"setup-weighted = {pearson(vols, grosses, ns):+.3f}")

    # vol-tercile buckets
    order = sorted(pts, key=lambda p: p[0])
    t = len(order) // 3
    for name, grp in (("vol-LOW", order[:t]), ("vol-MID", order[t:2 * t]), ("vol-HIGH", order[2 * t:])):
        if not grp:
            continue
        tot_n = sum(p[2] for p in grp)
        w_edge = sum(p[1] * p[2] for p in grp) / tot_n
        w_gross = sum(p[3] * p[2] for p in grp) / tot_n
        vlo, vhi = grp[0][0], grp[-1][0]
        cells_pos = sum(1 for p in grp if p[1] > 0)
        print(f"  {name:<8} cells={len(grp):<3} vol[{vlo:5.1f}-{vhi:5.1f}]  "
              f"net={w_edge*100:+.3f}%  gross={w_gross*100:+.3f}%  cells+={cells_pos}/{len(grp)}")


if __name__ == "__main__":
    main()
