"""Decompose Volume Ranking into explicit signals.

VR = implicit size exposure + volume timing.
Question: what if we build from explicit metrics?

Tests:
  1. Volume acceleration as market timing (aggregate signal)
  2. Size sort (vol proxy) alone
  3. Volume acceleration WITHIN size buckets (interaction)
  4. Volume acceleration as directional BTC signal
  5. Pure cross-sectional volume momentum (hedge size)

Usage:
    python scripts/research/vr_decompose_signals.py
"""
from __future__ import annotations

import sys
from math import sqrt
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from scripts.research.backtest_volume_ranking import load_daily_data

DATA = ROOT / "data" / "processed" / "candles"
FEE = 0.0004  # 4bps per side


def compute_sharpe(rets: np.ndarray) -> float:
    if len(rets) < 10 or rets.std() == 0:
        return 0.0
    return float(rets.mean() / rets.std() * sqrt(365))


def compute_stats(rets: np.ndarray, label: str) -> dict:
    total = float(np.prod(1 + rets) - 1)
    sharpe = compute_sharpe(rets)
    wr = float((rets > 0).mean())
    dd = _max_drawdown(rets)
    return {"label": label, "sharpe": sharpe, "total": total, "wr": wr, "dd": dd, "n": len(rets)}


def _max_drawdown(rets):
    eq = np.cumprod(1 + rets)
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / peak
    return float(dd.max())


def print_row(s: dict):
    print(f"  {s['label']:<50} Sharpe={s['sharpe']:>5.2f}  "
          f"Total={s['total']:>+6.1%}  WR={s['wr']:.0%}  DD={s['dd']:.1%}  N={s['n']}")


def main():
    print("=" * 75)
    print("  VOLUME RANKING: SIGNAL DECOMPOSITION")
    print("=" * 75)

    datasets = load_daily_data()
    symbols = list(datasets.keys())
    print(f"  {len(symbols)} symbols loaded")

    # Build common date index
    all_dates = None
    for df in datasets.values():
        dates = set(df.index)
        all_dates = dates if all_dates is None else all_dates.intersection(dates)
    all_dates = sorted(all_dates)
    print(f"  {len(all_dates)} common trading days")

    # Precompute daily returns and signals for each symbol
    daily_ret = {}
    for sym, df in datasets.items():
        daily_ret[sym] = df["close"].pct_change()

    # ================================================================
    # Baseline: original VR
    # ================================================================
    print(f"\n{'='*75}")
    print("  BASELINE STRATEGIES")
    print(f"{'='*75}")

    results = []

    # 1. Original VR (7d/30d volume ratio)
    from scripts.research.backtest_volume_ranking import backtest_volume_ranking
    vr = backtest_volume_ranking(datasets, 7, 30)
    results.append(compute_stats(vr["net_ret"].values, "Original VR (7d/30d volume ratio)"))

    # ================================================================
    # SIGNAL 1: Volume acceleration as aggregate market timing
    # ================================================================
    print(f"\n{'='*75}")
    print("  SIGNAL 1: AGGREGATE VOLUME ACCELERATION -> BTC DIRECTION")
    print(f"{'='*75}")

    # Idea: when MOST coins see volume accelerating, market is trending
    # -> go long BTC. When decelerating -> short or flat.
    btc_ret = daily_ret.get("BTC/USDT")
    if btc_ret is None:
        print("  No BTC data!")
        return

    agg_rets = []
    for i in range(35, len(all_dates)):
        date = all_dates[i]
        prev = all_dates[i - 1]

        # Volume ratios
        ratios = {}
        for sym, df in datasets.items():
            if date not in df.index or sym == "BTC/USDT":
                continue
            mask = df.index <= prev
            if mask.sum() < 35:
                continue
            sub = df[mask]
            v7 = sub["volume"].iloc[-7:].mean()
            v30 = sub["volume"].iloc[-30:].mean()
            if v30 > 0:
                ratios[sym] = v7 / v30

        if len(ratios) < 10:
            continue

        # Fraction of coins with accelerating volume
        pct_accel = sum(1 for r in ratios.values() if r > 1.0) / len(ratios)

        # BTC return today
        if date in btc_ret.index and prev in btc_ret.index:
            ret = btc_ret[date]
            if pd.notna(ret):
                # Signal: go long when >60% accelerating, short when <40%
                if pct_accel > 0.6:
                    agg_rets.append(ret - FEE * 0.1)  # low turnover
                elif pct_accel < 0.4:
                    agg_rets.append(-ret - FEE * 0.1)
                else:
                    agg_rets.append(0)

    if agg_rets:
        # Only count traded days
        traded = [r for r in agg_rets if r != 0]
        results.append(compute_stats(np.array(agg_rets), "Agg volume -> BTC direction (60/40 thresh)"))
        print(f"  Traded {len(traded)}/{len(agg_rets)} days ({len(traded)/len(agg_rets):.0%})")

    # ================================================================
    # SIGNAL 2: Size-neutral volume ranking
    # ================================================================
    print(f"\n{'='*75}")
    print("  SIGNAL 2: SIZE-NEUTRAL VOLUME RANKING")
    print(f"{'='*75}")

    # Split into size buckets (by 20d vol), then rank within each bucket
    sn_rets = []
    prev_longs, prev_shorts = set(), set()

    for i in range(35, len(all_dates)):
        date = all_dates[i]
        prev = all_dates[i - 1]

        # Compute size (vol proxy) and volume ratio
        signals = {}
        for sym, df in datasets.items():
            if date not in df.index:
                continue
            mask = df.index <= prev
            if mask.sum() < 35:
                continue
            sub = df[mask]
            v7 = sub["volume"].iloc[-7:].mean()
            v30 = sub["volume"].iloc[-30:].mean()
            vol_20 = sub["close"].pct_change().iloc[-20:].std()

            if v30 > 0 and vol_20 > 0:
                signals[sym] = {"vol_ratio": v7 / v30, "size_vol": vol_20}

        if len(signals) < 10:
            continue

        # Split into 2 size buckets (high vol = small, low vol = large)
        sorted_by_size = sorted(signals.keys(), key=lambda s: signals[s]["size_vol"])
        mid = len(sorted_by_size) // 2
        large_bucket = sorted_by_size[:mid]
        small_bucket = sorted_by_size[mid:]

        # Within each bucket: long top volume ratio, short bottom
        longs, shorts = set(), set()
        for bucket in [large_bucket, small_bucket]:
            sorted_by_vr = sorted(bucket, key=lambda s: signals[s]["vol_ratio"], reverse=True)
            n_b = len(sorted_by_vr)
            half = n_b // 2
            longs.update(sorted_by_vr[:half])
            shorts.update(sorted_by_vr[half:])

        # Compute returns
        l_rets = [daily_ret[s][date] for s in longs
                  if s in daily_ret and date in daily_ret[s].index and pd.notna(daily_ret[s][date])]
        s_rets = [-daily_ret[s][date] for s in shorts
                  if s in daily_ret and date in daily_ret[s].index and pd.notna(daily_ret[s][date])]

        if l_rets and s_rets:
            gross = (np.mean(l_rets) + np.mean(s_rets)) / 2
            new_l = longs - prev_longs
            new_s = shorts - prev_shorts
            turnover = (len(new_l) + len(new_s)) / max(1, len(signals))
            fee = turnover * FEE * 2
            sn_rets.append(gross - fee)

        prev_longs, prev_shorts = longs, shorts

    if sn_rets:
        results.append(compute_stats(np.array(sn_rets), "Size-neutral VR (within-bucket ranking)"))

    # ================================================================
    # SIGNAL 3: Volume acceleration x Size interaction
    # ================================================================
    print(f"\n{'='*75}")
    print("  SIGNAL 3: VOLUME ACCEL x SIZE INTERACTION")
    print(f"{'='*75}")

    # Weight position size by volume ratio, but hedge size exposure
    # Long: high vol_ratio coins, short: low vol_ratio, but equal size in each bucket
    # This is basically VR with explicit size hedging

    # ================================================================
    # SIGNAL 4: Top volume acceleration quintile only (concentrated)
    # ================================================================
    print(f"\n{'='*75}")
    print("  SIGNAL 4: CONCENTRATED TOP/BOTTOM 20%")
    print(f"{'='*75}")

    conc_rets = []
    prev_l, prev_s = set(), set()

    for i in range(35, len(all_dates)):
        date = all_dates[i]
        prev = all_dates[i - 1]

        ratios = {}
        for sym, df in datasets.items():
            if date not in df.index:
                continue
            mask = df.index <= prev
            if mask.sum() < 35:
                continue
            sub = df[mask]
            v7 = sub["volume"].iloc[-7:].mean()
            v30 = sub["volume"].iloc[-30:].mean()
            if v30 > 0:
                ratios[sym] = v7 / v30

        if len(ratios) < 10:
            continue

        sorted_syms = sorted(ratios.keys(), key=lambda s: ratios[s], reverse=True)
        n = len(sorted_syms)
        q = max(2, n // 5)  # top/bottom 20%

        longs = set(sorted_syms[:q])
        shorts = set(sorted_syms[-q:])

        l_rets = [daily_ret[s][date] for s in longs
                  if s in daily_ret and date in daily_ret[s].index and pd.notna(daily_ret[s][date])]
        s_rets = [-daily_ret[s][date] for s in shorts
                  if s in daily_ret and date in daily_ret[s].index and pd.notna(daily_ret[s][date])]

        if l_rets and s_rets:
            gross = (np.mean(l_rets) + np.mean(s_rets)) / 2
            new_l = longs - prev_l
            new_s = shorts - prev_s
            turnover = (len(new_l) + len(new_s)) / max(1, n)
            fee = turnover * FEE * 2
            conc_rets.append(gross - fee)

        prev_l, prev_s = longs, shorts

    if conc_rets:
        results.append(compute_stats(np.array(conc_rets), "Concentrated top/bottom 20% VR"))

    # ================================================================
    # SIGNAL 5: Volume momentum (change in volume ratio)
    # ================================================================
    print(f"\n{'='*75}")
    print("  SIGNAL 5: VOLUME MOMENTUM (delta of vol ratio)")
    print(f"{'='*75}")

    vmom_rets = []
    prev_ratios: dict[str, float] = {}
    prev_l2, prev_s2 = set(), set()

    for i in range(36, len(all_dates)):
        date = all_dates[i]
        prev = all_dates[i - 1]

        ratios = {}
        for sym, df in datasets.items():
            if date not in df.index:
                continue
            mask = df.index <= prev
            if mask.sum() < 35:
                continue
            sub = df[mask]
            v7 = sub["volume"].iloc[-7:].mean()
            v30 = sub["volume"].iloc[-30:].mean()
            if v30 > 0:
                ratios[sym] = v7 / v30

        if len(ratios) < 10 or not prev_ratios:
            prev_ratios = ratios
            continue

        # Delta: change in volume ratio from yesterday
        deltas = {}
        for sym in ratios:
            if sym in prev_ratios:
                deltas[sym] = ratios[sym] - prev_ratios[sym]

        if len(deltas) < 10:
            prev_ratios = ratios
            continue

        sorted_syms = sorted(deltas.keys(), key=lambda s: deltas[s], reverse=True)
        n = len(sorted_syms)
        half = n // 2
        longs = set(sorted_syms[:half])
        shorts = set(sorted_syms[half:])

        l_rets = [daily_ret[s][date] for s in longs
                  if s in daily_ret and date in daily_ret[s].index and pd.notna(daily_ret[s][date])]
        s_rets = [-daily_ret[s][date] for s in shorts
                  if s in daily_ret and date in daily_ret[s].index and pd.notna(daily_ret[s][date])]

        if l_rets and s_rets:
            gross = (np.mean(l_rets) + np.mean(s_rets)) / 2
            new_l = longs - prev_l2
            new_s = shorts - prev_s2
            turnover = (len(new_l) + len(new_s)) / max(1, n)
            fee = turnover * FEE * 2
            vmom_rets.append(gross - fee)

        prev_l2, prev_s2 = longs, shorts
        prev_ratios = ratios

    if vmom_rets:
        results.append(compute_stats(np.array(vmom_rets), "Volume momentum (delta vol ratio)"))

    # ================================================================
    # SIGNAL 6: Pure size (long small, short large)
    # ================================================================
    size_rets = []
    prev_ls, prev_ss = set(), set()

    for i in range(25, len(all_dates)):
        date = all_dates[i]
        prev = all_dates[i - 1]

        vols = {}
        for sym, df in datasets.items():
            if date not in df.index:
                continue
            mask = df.index <= prev
            if mask.sum() < 25:
                continue
            vols[sym] = df[mask]["close"].pct_change().iloc[-20:].std()

        if len(vols) < 10:
            continue

        sorted_syms = sorted(vols.keys(), key=lambda s: vols[s], reverse=True)
        n = len(sorted_syms)
        half = n // 2
        longs = set(sorted_syms[:half])   # high vol = "small"
        shorts = set(sorted_syms[half:])  # low vol = "large"

        l_rets = [daily_ret[s][date] for s in longs
                  if s in daily_ret and date in daily_ret[s].index and pd.notna(daily_ret[s][date])]
        s_rets = [-daily_ret[s][date] for s in shorts
                  if s in daily_ret and date in daily_ret[s].index and pd.notna(daily_ret[s][date])]

        if l_rets and s_rets:
            gross = (np.mean(l_rets) + np.mean(s_rets)) / 2
            new_l = longs - prev_ls
            new_s = shorts - prev_ss
            turnover = (len(new_l) + len(new_s)) / max(1, n)
            fee = turnover * FEE * 2
            size_rets.append(gross - fee)

        prev_ls, prev_ss = longs, shorts

    if size_rets:
        results.append(compute_stats(np.array(size_rets), "Pure size (long high-vol, short low-vol)"))

    # ================================================================
    # Summary
    # ================================================================
    print(f"\n{'='*75}")
    print("  SUMMARY")
    print(f"{'='*75}")
    results.sort(key=lambda x: x["sharpe"], reverse=True)
    for r in results:
        print_row(r)

    # Correlation matrix
    print(f"\n  Correlation between top strategies:")
    # We'll just note the key finding


if __name__ == "__main__":
    main()
