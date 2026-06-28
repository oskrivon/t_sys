"""Icebreaker Phase 3 open thread: why do months differ?

Decomposes the good-rate decline (base 12.4 -> 10.2 -> 10.3%, conjunction
23 -> 23 -> 13.6%) across March/April/May to answer:
  1. Is the decline across ALL coins or concentrated in some?
  2. Does per-coin/per-month good-rate track a regime proxy (realized vol)?
  3. Does the spacing-clean selection (one_sided>=0.9 & spacing>60) hold per coin?

All inputs are local per-setup dumps with the `good` label already computed.
No network. Realized vol is computed from the same klines used to build setups.
"""
import json
import math
import collections
import os

MONTHS = [
    ("MAR", "data/ib_major_liq.jsonl", "data/klines"),
    ("APR", "data/ib_major_apr_liq.jsonl", "data/klines_apr"),
    ("MAY", "data/ib_major_may_liq.jsonl", "data/klines_may"),
]


def load(f):
    return [json.loads(l) for l in open(f) if l.strip()]


def spacing(r):
    t = r.get("touches")
    sb = r.get("span_bars")
    if not t or t < 2 or sb is None:
        return None
    return sb / (t - 1)


def is_clean(r):
    sp = spacing(r)
    return r.get("one_sided", 0) >= 0.9 and sp is not None and sp > 60


def rate(rows, pred=lambda r: True):
    sel = [r for r in rows if pred(r)]
    if not sel:
        return (0, 0, float("nan"))
    g = sum(1 for r in sel if r.get("good"))
    return (g, len(sel), g / len(sel) * 100)


def realized_vol(kline_dir, symbol):
    """Annualized-ish realized vol from 1m closes: std of log returns * sqrt(N).

    Returns daily-scale vol (std of 1m logret * sqrt(1440)) averaged proxy.
    """
    path = None
    if os.path.isdir(kline_dir):
        for fn in os.listdir(kline_dir):
            if fn.startswith(symbol) and fn.endswith(".jsonl"):
                path = os.path.join(kline_dir, fn)
                break
    if not path:
        return None
    closes = []
    for l in open(path):
        l = l.strip()
        if not l:
            continue
        try:
            d = json.loads(l)
        except Exception:
            continue
        c = d.get("close") if isinstance(d, dict) else (d[4] if isinstance(d, list) and len(d) > 4 else None)
        if c is not None:
            closes.append(float(c))
    if len(closes) < 10:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if not rets:
        return None
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / len(rets)
    return math.sqrt(var) * math.sqrt(1440) * 100  # daily vol %


def main():
    coins = set()
    data = {}
    for tag, f, kd in MONTHS:
        rows = load(f)
        data[tag] = (rows, kd)
        coins.update(r["symbol"] for r in rows)
    coins = sorted(coins)

    # --- Table 1: base good-rate per coin per month ---
    print("=" * 78)
    print("TABLE 1 — base good-rate (good/n) per coin per month")
    print(f"{'coin':<16} {'MAR':>14} {'APR':>14} {'MAY':>14}   trend")
    print("-" * 78)
    decline_all = []
    for c in coins:
        cells = []
        rates = []
        for tag, f, kd in MONTHS:
            g, n, pr = rate(data[tag][0], lambda r, c=c: r["symbol"] == c)
            cells.append(f"{g:>3}/{n:<3} {pr:4.1f}%")
            rates.append(pr)
        arrow = "down" if rates[2] < rates[0] else "up "
        decline_all.append(rates[2] - rates[0])
        print(f"{c:<16} {cells[0]:>14} {cells[1]:>14} {cells[2]:>14}   {arrow} {rates[2]-rates[0]:+.1f}pp")
    print("-" * 78)
    for tag, f, kd in MONTHS:
        g, n, pr = rate(data[tag][0])
        print(f"  {tag} TOTAL: {g}/{n} {pr:.1f}%")
    ndown = sum(1 for d in decline_all if d < 0)
    print(f"\n  coins declining MAR->MAY: {ndown}/{len(coins)}")

    # --- Table 2: clean-selection good-rate per coin per month ---
    print("\n" + "=" * 78)
    print("TABLE 2 — CLEAN (one_sided>=0.9 & spacing>60) good-rate per coin")
    print(f"{'coin':<16} {'MAR':>14} {'APR':>14} {'MAY':>14}")
    print("-" * 78)
    for c in coins:
        cells = []
        for tag, f, kd in MONTHS:
            g, n, pr = rate(data[tag][0], lambda r, c=c: r["symbol"] == c and is_clean(r))
            cells.append(f"{g:>2}/{n:<3} {pr:4.1f}%" if n else f"{'--':>9}")
        print(f"{c:<16} {cells[0]:>14} {cells[1]:>14} {cells[2]:>14}")
    print("-" * 78)
    for tag, f, kd in MONTHS:
        g, n, pr = rate(data[tag][0], is_clean)
        print(f"  {tag} CLEAN TOTAL: {g}/{n} {pr:.1f}%  (lift vs base {pr - rate(data[tag][0])[2]:+.1f}pp)")

    # --- Table 3: regime proxy — realized vol + setup count per coin per month ---
    print("\n" + "=" * 78)
    print("TABLE 3 — regime proxy: daily realized vol % (setup count)")
    print(f"{'coin':<16} {'MAR':>16} {'APR':>16} {'MAY':>16}")
    print("-" * 78)
    vol_by_month = {tag: [] for tag, _, _ in MONTHS}
    for c in coins:
        cells = []
        for tag, f, kd in MONTHS:
            n = sum(1 for r in data[tag][0] if r["symbol"] == c)
            v = realized_vol(kd, c)
            if v is not None:
                vol_by_month[tag].append(v)
                cells.append(f"{v:5.1f}% (n={n:<3})")
            else:
                cells.append(f"  n/a (n={n:<3})")
        print(f"{c:<16} {cells[0]:>16} {cells[1]:>16} {cells[2]:>16}")
    print("-" * 78)
    for tag, _, _ in MONTHS:
        vs = vol_by_month[tag]
        if vs:
            print(f"  {tag} mean realized vol: {sum(vs)/len(vs):.2f}%")


if __name__ == "__main__":
    main()
