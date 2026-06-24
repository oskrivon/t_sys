"""Approach DYNAMICS at the touch: does how the book evolves WHILE price walks into the
level separate real breaks from fakeouts? (Phase 2 Angle 3)

book_gate (Angle 2) tested a SNAPSHOT at ts_cross + a 2s pull -> AUC 0.50. flow_gate
(Angle 1) tested the TAPE before the cross -> AUC 0.50. Untested: the TRAJECTORY of
resting liquidity over the APPROACH (tens of seconds before the touch). Three user
hypotheses, one experiment:

  Q1  book-depth derivative on the approach (confident accumulation -> break, hesitant ->
      bounce): erosion/slope of run-side depth & wall as price closes on the level.
  Q2  MM accumulation footprint before a manufactured break: erosion of the BASE-side
      support (the side price is leaving) over the approach. Plus a real MULTIVARIATE
      classifier (logreg + GBT, CV) over book-approach + tape-flow -- does any COMBINATION
      separate, where every univariate AUC was ~0.50?
  Q3  the inverse / MM's informational proxy: an MM PULLING the run-side wall is ambiguous
      (manufactured break OR auto risk-mgmt), but an MM HOLDING the wall as price arrives is
      informative -- they want to defend -> the break gets absorbed (fakeout). Conditional
      2x2: among setups with a real wall ahead, {wall held vs pulled} x {real vs fakeout}.

For each armed cross we reconstruct the book at a ladder ts_cross-{30,15,10,5,2,0}s (all
from events ts<=cut, no lookahead) and build approach features. March only (book store =
FART/WIF/PEPE March); NO OOS holdout (April book not collected) -> any signal here is a
discovery to validate later, not a tradeable result.

    python scripts/research/icebreaker_approach.py \
        --dump /root/trading/tmp/ib_lc_book3.jsonl --store /root/trading/data/ib_book
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from collections import defaultdict
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


mc = _load("icebreaker_micro", "scripts/research/icebreaker_micro.py")
fg = _load("icebreaker_flow_gate", "scripts/research/icebreaker_flow_gate.py")
BID, ASK = 0, 1
OFFS_MS = [30000, 15000, 10000, 5000, 2000, 0]      # ladder: -30s .. touch


def side_book(s_ts, s_pr, s_qty, s_side, L, t, tol, band):
    """(depth_within_band, max_wall_at_level) per side from events ts<=t (already
    restricted to this level's neighbourhood)."""
    keep = s_ts <= t
    res = {}
    for bs in (BID, ASK):
        m = keep & (s_side == bs)
        uniq, last = mc.latest_per_price(s_pr[m], s_qty[m])
        nz = last > 0
        uniq, last = uniq[nz], last[nz]
        if len(uniq) == 0:
            res[bs] = (0.0, 0.0)
            continue
        notion = uniq * last
        sel = (uniq >= L * (1 - band)) & (uniq <= L * (1 + band))
        wall = float(notion[np.abs(uniq - L) <= L * tol].max(initial=0.0))
        res[bs] = (float(notion[sel].sum()), wall)
    return res


def approach_features(surv, L, is_long, ts_cross, tol, band):
    """Resting-book trajectory over the approach ladder. run = side price runs INTO."""
    s_ts, s_pr, s_qty, s_side = surv
    sub = np.abs(s_pr - L) <= L * band * 2          # this level's neighbourhood only (fast)
    st, sp, sq, ss = s_ts[sub], s_pr[sub], s_qty[sub], s_side[sub]
    run_s = ASK if is_long else BID
    base_s = BID if is_long else ASK
    rw, rd, bw, bd = [], [], [], []
    for off in OFFS_MS:
        b = side_book(st, sp, sq, ss, L, ts_cross - off, tol, band)
        rd.append(b[run_s][0]); rw.append(b[run_s][1])
        bd.append(b[base_s][0]); bw.append(b[base_s][1])
    eps = 1e-9
    rw, rd, bw, bd = map(np.asarray, (rw, rd, bw, bd))
    x = -np.asarray(OFFS_MS, dtype=float) / 1000.0   # seconds, ascending toward 0
    def slope(y):                                     # per-second change, normalised by mean
        m = y.mean()
        return float(np.polyfit(x, y, 1)[0] / (m + eps)) if m > 0 else 0.0
    return {
        # Q1/Q3 run side: erosion (+ = wall/depth shrank approaching = path clearing)
        "run_wall_first": float(rw[0]),
        "run_wall_now": float(rw[-1]),
        "run_wall_erosion": float((rw[0] - rw[-1]) / (rw[0] + eps)),
        "run_depth_erosion": float((rd[0] - rd[-1]) / (rd[0] + eps)),
        "run_wall_slope": slope(rw),
        "run_depth_slope": slope(rd),
        # Q2 base side: support erosion over the approach (MM stepping out of the way)
        "base_wall_erosion": float((bw[0] - bw[-1]) / (bw[0] + eps)),
        "base_depth_erosion": float((bd[0] - bd[-1]) / (bd[0] + eps)),
        "base_depth_slope": slope(bd),
        # snapshot at touch (baseline; book_gate parity)
        "run_depth_now": float(rd[-1]),
        "imbalance_now": float((bd[-1] - rd[-1]) / (bd[-1] + rd[-1] + eps)),
    }


APPROACH_FEATS = ["run_wall_first", "run_wall_now", "run_wall_erosion", "run_depth_erosion",
                  "run_wall_slope", "run_depth_slope", "base_wall_erosion",
                  "base_depth_erosion", "base_depth_slope", "run_depth_now", "imbalance_now"]


def conditional_split(rows):
    """Q3: among crosses with a real wall ahead at -30s (top tercile of run_wall_first),
    split by whether that wall was HELD vs PULLED on the approach; report good/win/mean_net."""
    have = [r for r in rows if r["run_wall_first"] > 0]
    if len(have) < 30:
        print("\n  [Q3] too few crosses with a wall ahead at -30s for the split")
        return
    thr = fg.quantile([r["run_wall_first"] for r in rows], 0.667)
    big = [r for r in have if r["run_wall_first"] >= thr]
    print(f"\n  --- [Q3] MM wall ahead, held vs pulled (n_big={len(big)}, wall_first>=top-tercile {thr:.4g}) ---")
    print(f"  {'group':14s} {'n':>5s} {'good%':>6s} {'win%':>6s} {'mean_net%':>10s}")
    def stat(g, lab):
        if not g:
            print(f"  {lab:14s} {0:5d}")
            return
        gr = sum(1 for r in g if r["mfe"] >= 0.01) / len(g)
        wr = sum(1 for r in g if r["net"] > 0) / len(g)
        mn = sum(r["net"] for r in g) / len(g)
        print(f"  {lab:14s} {len(g):5d} {gr:6.1%} {wr:6.1%} {mn*100:+10.3f}")
    stat(big, "all-big-wall")
    for cut, lab in ((0.5, "PULLED>=50%"), (None, "HELD<50%")):
        if cut is not None:
            stat([r for r in big if r["run_wall_erosion"] >= cut], lab)
        else:
            stat([r for r in big if r["run_wall_erosion"] < 0.5], lab)


def classify(rows, flow_keys):
    """Q2: does a MULTIVARIATE model separate where univariate AUC was ~0.50? CV AUC."""
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.model_selection import cross_val_score
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
    except Exception as e:
        print(f"\n  [Q2] sklearn unavailable ({e}) -- skipping multivariate classifier")
        return
    feats = APPROACH_FEATS + flow_keys
    X = np.array([[r.get(f, 0.0) for f in feats] for r in rows], dtype=float)
    X = np.nan_to_num(X, posinf=0.0, neginf=0.0)
    for tgt_name, y in (("good(MFE>=1%)", np.array([1 if r["mfe"] >= 0.01 else 0 for r in rows])),
                        ("win(net>0)", np.array([1 if r["net"] > 0 else 0 for r in rows]))):
        if y.sum() < 10 or (len(y) - y.sum()) < 10:
            print(f"\n  [Q2] {tgt_name}: too few positives ({y.sum()}) -- skip")
            continue
        lr = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000, class_weight="balanced"))
        gb = GradientBoostingClassifier(n_estimators=120, max_depth=3, subsample=0.8)
        a_lr = cross_val_score(lr, X, y, cv=5, scoring="roc_auc")
        a_gb = cross_val_score(gb, X, y, cv=5, scoring="roc_auc")
        print(f"\n  [Q2] multivariate CV-AUC vs {tgt_name} ({len(feats)} feats, n={len(y)}, pos={int(y.sum())}):"
              f"  logreg {a_lr.mean():.3f}±{a_lr.std():.3f}   GBT {a_gb.mean():.3f}±{a_gb.std():.3f}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dump", required=True)
    p.add_argument("--store", required=True)
    p.add_argument("--symbols", nargs="+", default=None)
    p.add_argument("--tol", type=float, default=0.0015)
    p.add_argument("--band", type=float, default=0.005)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    rows = [json.loads(l) for l in open(args.dump) if l.strip()]
    if args.symbols:
        rows = [r for r in rows if r["symbol"] in args.symbols]
    rows = [r for r in rows if "ts_cross" in r and "level" in r and "side" in r]
    flow_keys = [k for k in ("ofi2000", "dfrac2000", "nrate2000", "ofi5000", "dfrac5000",
                             "nrate5000", "ofi15000", "dfrac15000", "nrate15000", "tib_imb",
                             "burst") if k in rows[0]]
    for r in rows:
        r["date"] = time.strftime("%Y-%m-%d", time.gmtime(r["ts_cross"] / 1000.0))
    by_day = defaultdict(list)
    for r in rows:
        by_day[(r["symbol"], r["date"])].append(r)

    store = Path(args.store)
    feat_rows, no_book = [], 0
    for (sym, date), crosses in sorted(by_day.items()):
        levels = sorted(set(c["level"] for c in crosses))
        surv = mc.filter_book(store, sym, date, levels, args.band * 2)
        if len(surv[0]) == 0:
            no_book += len(crosses)
            continue
        for c in crosses:
            feat = approach_features(surv, c["level"], c["side"] == "long", c["ts_cross"],
                                     args.tol, args.band)
            feat_rows.append({**c, **feat})
        print(f"[{sym} {date}] crosses={len(crosses)}", flush=True)

    n = len(feat_rows)
    if n == 0:
        print("no crosses with book -- nothing to analyze")
        return
    base_good = sum(1 for r in feat_rows if r["mfe"] >= 0.01) / n
    base_win = sum(1 for r in feat_rows if r["net"] > 0) / n
    print("=" * 92)
    print(f"  APPROACH DYNAMICS  n={n}  ({no_book} no book)  base good(MFE>=1%)={base_good:.1%}  "
          f"base win={base_win:.1%}  mean_net={sum(r['net'] for r in feat_rows)/n*100:+.3f}%  (MARCH, no OOS)")
    print("=" * 92)
    print(f"  {'feature':18s} {'AUC_good':>9s} {'AUC_win':>8s} {'mean@good':>11s} {'mean@bad':>11s}")
    ranked = []
    for f in APPROACH_FEATS:
        gp = [r[f] for r in feat_rows if r["mfe"] >= 0.01]; gn = [r[f] for r in feat_rows if r["mfe"] < 0.01]
        wp = [r[f] for r in feat_rows if r["net"] > 0]; wn = [r[f] for r in feat_rows if r["net"] <= 0]
        a_g, a_w = fg.auc(gp, gn), fg.auc(wp, wn)
        mg = sum(gp) / len(gp) if gp else 0.0; mb = sum(gn) / len(gn) if gn else 0.0
        ranked.append((a_g, f, a_w, mg, mb))
    for a_g, f, a_w, mg, mb in sorted(ranked, key=lambda x: -abs((x[0] if x[0] == x[0] else 0.5) - 0.5)):
        print(f"  {f:18s} {a_g:9.3f} {a_w:8.3f} {mg:11.4g} {mb:11.4g}")

    conditional_split(feat_rows)
    classify(feat_rows, flow_keys)

    if args.out:
        def _native(o):
            if isinstance(o, np.generic):
                return o.item()
            raise TypeError
        with open(args.out, "w") as fo:
            for r in feat_rows:
                fo.write(json.dumps(r, default=_native) + "\n")
        print(f"\n  dumped {n} rows+approach features -> {args.out}")


if __name__ == "__main__":
    main()
