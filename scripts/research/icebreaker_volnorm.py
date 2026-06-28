"""Icebreaker Phase 3: vol-normalized 'good' — is the month decline a vol artifact?

Phase 3 'good' = MFE >= 1.5% (fixed %). Per-cell good-rate tracks realized vol
(Pearson +0.61), so a higher-vol month mechanically yields more 'good' regardless
of level quality. Here we redefine:

    good_vn = (MFE / daily_realized_vol) >= k

with a SINGLE global k calibrated so the pooled good-count matches the original
(~11%). Then we ask:
  (A) Does the MAR->APR->MAY decline flatten under vol-normalization?
      -> if yes, the 'regime' decline was a volatility artifact.
  (B) Does the spacing-clean (one_sided>=0.9 & spacing>60) lift survive?
      -> if yes, spacing is a real level-quality signal independent of vol.

No network; all inputs local.
"""
import json
import math
import os

MONTHS = [
    ("MAR", "data/ib_major_liq.jsonl", "data/klines"),
    ("APR", "data/ib_major_apr_liq.jsonl", "data/klines_apr"),
    ("MAY", "data/ib_major_may_liq.jsonl", "data/klines_may"),
]


def load(f):
    return [json.loads(l) for l in open(f) if l.strip()]


def spacing(r):
    t, sb = r.get("touches"), r.get("span_bars")
    if not t or t < 2 or sb is None:
        return None
    return sb / (t - 1)


def is_clean(r):
    sp = spacing(r)
    return r.get("one_sided", 0) >= 0.9 and sp is not None and sp > 60


_vol_cache = {}


def rvol(kd, sym):
    key = (kd, sym)
    if key in _vol_cache:
        return _vol_cache[key]
    p = None
    if os.path.isdir(kd):
        for fn in os.listdir(kd):
            if fn.startswith(sym) and fn.endswith(".jsonl"):
                p = os.path.join(kd, fn)
                break
    v = None
    if p:
        cl = []
        for l in open(p):
            l = l.strip()
            if not l:
                continue
            d = json.loads(l)
            c = d.get("close") if isinstance(d, dict) else d[4]
            if c is not None:
                cl.append(float(c))
        if len(cl) >= 10:
            r = [math.log(cl[i] / cl[i - 1]) for i in range(1, len(cl)) if cl[i - 1] > 0]
            if r:
                m = sum(r) / len(r)
                var = sum((x - m) ** 2 for x in r) / len(r)
                v = math.sqrt(var) * math.sqrt(1440) * 100
    _vol_cache[key] = v
    return v


def quantile(sorted_xs, q):
    if not sorted_xs:
        return float("nan")
    idx = q * (len(sorted_xs) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_xs[lo]
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (idx - lo)


def main():
    # attach vol-normalized MFE to every setup
    pooled = []
    data = {}
    coins = set()
    for tag, f, kd in MONTHS:
        rows = load(f)
        for r in rows:
            v = rvol(kd, r["symbol"])
            r["_vn"] = (r["mfe"] / v) if (v and r.get("mfe") is not None) else None
        data[tag] = rows
        coins.update(r["symbol"] for r in rows)
        pooled += [r for r in rows if r["_vn"] is not None]
    coins = sorted(coins)

    # original pooled good fraction -> calibrate global k
    orig_good = sum(1 for r in pooled if r.get("good"))
    frac = orig_good / len(pooled)
    vns = sorted(r["_vn"] for r in pooled)
    k = quantile(vns, 1 - frac)
    print(f"Pooled setups w/ vol: {len(pooled)}  orig good frac: {frac*100:.1f}%")
    print(f"Calibrated global k (MFE/vol threshold) = {k:.3f}")
    print(f"  (interpretation: 'good' = MFE >= {k:.2f} x daily realized vol)\n")

    def gv(r):
        return r["_vn"] is not None and r["_vn"] >= k

    def rate(rows, pred):
        sel = [r for r in rows if pred(r)]
        if not sel:
            return (0, 0, float("nan"))
        g = sum(1 for r in sel if gv(r))
        return (g, len(sel), g / len(sel) * 100)

    # (A) month-level decline: original good vs vol-normalized good
    print("=" * 70)
    print("(A) MONTH DECLINE — original 'good' vs vol-normalized 'good_vn'")
    print(f"{'month':<6} {'orig good-rate':>16} {'vol-norm good-rate':>20}")
    print("-" * 70)
    for tag, f, kd in MONTHS:
        rows = [r for r in data[tag] if r["_vn"] is not None]
        og = sum(1 for r in rows if r.get("good")) / len(rows) * 100
        g, n, vr = rate(rows, lambda r: True)
        print(f"{tag:<6} {og:>15.1f}% {vr:>19.1f}%")
    print("-" * 70)
    print("  -> if vol-norm column is FLAT, the decline was a vol artifact\n")

    # (B) spacing-clean lift under vol-normalized good, per month
    print("=" * 70)
    print("(B) SPACING-CLEAN lift under vol-normalized 'good_vn'")
    print(f"{'month':<6} {'base good_vn':>14} {'clean good_vn':>16} {'lift':>8}")
    print("-" * 70)
    for tag, f, kd in MONTHS:
        rows = [r for r in data[tag] if r["_vn"] is not None]
        _, _, base = rate(rows, lambda r: True)
        cg, cn, clean = rate(rows, is_clean)
        lift = clean - base
        print(f"{tag:<6} {base:>13.1f}% {clean:>14.1f}% ({cn:>3}) {lift:>+6.1f}pp")
    print("-" * 70)
    print("  -> if lift stays +ve all 3 months, spacing is real (not vol proxy)\n")

    # (C) per-coin vol-norm good-rate trend (does cross-coin decline flatten?)
    print("=" * 70)
    print("(C) per-coin vol-normalized good-rate (good_vn) by month")
    print(f"{'coin':<16} {'MAR':>10} {'APR':>10} {'MAY':>10}   trend")
    print("-" * 70)
    ndown = 0
    for c in coins:
        rs = []
        for tag, f, kd in MONTHS:
            rows = [r for r in data[tag] if r["symbol"] == c and r["_vn"] is not None]
            _, _, vr = rate(rows, lambda r: True)
            rs.append(vr)
        d = rs[2] - rs[0]
        if d < 0:
            ndown += 1
        print(f"{c:<16} {rs[0]:>9.1f}% {rs[1]:>9.1f}% {rs[2]:>9.1f}%   {d:+.1f}pp")
    print("-" * 70)
    print(f"  coins declining MAR->MAY under vol-norm: {ndown}/{len(coins)}")
    print("  (was 7/9 under fixed-% good)")


if __name__ == "__main__":
    main()
