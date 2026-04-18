"""
Backtest Multi-TF strategy: D1 levels + 4H/1H entries.

Hypothesis: signals near D1 levels are stronger than signals on local TF levels only.
Tests:
  1. 4H entries at D1 levels (D1 confluence)
  2. 1H scalp entries at D1 levels
  3. 4H entries at 4H+D1 confluence (level exists on both TFs)
  4. Baseline comparisons

Usage:
    python scripts/research/backtest_multi_tf.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import ccxt

from src.strategy.levels import get_rolling_levels, find_swing_points, cluster_points
from src.strategy.signals import detect_breakouts, detect_retests, detect_zakol, build_signal
from src.strategy.features import compute_features
from src.strategy.models import Breakout, Level

DATA = ROOT / "data" / "processed" / "candles"
REPORTS = ROOT / "data" / "reports"

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "ARB/USDT", "PEPE/USDT", "SUI/USDT", "DOGE/USDT",
    "AVAX/USDT", "OP/USDT", "NEAR/USDT", "FIL/USDT",
]

FEE_BPS = 10
INITIAL_CAPITAL = 10_000


def fetch_ohlcv(symbol: str, tf: str, months: int) -> pd.DataFrame:
    exchange = ccxt.binance({"enableRateLimit": True})
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - months * 30 * 86400 * 1000
    all_c, cur = [], since_ms
    while True:
        try:
            c = exchange.fetch_ohlcv(symbol, tf, since=cur, limit=1000)
        except Exception:
            break
        if not c:
            break
        all_c.extend(c)
        last = c[-1][0]
        if last <= cur or len(c) < 1000:
            break
        cur = last + 1
        time.sleep(0.1)
    if not all_c:
        return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


def load_data(tf: str, months: int) -> dict[str, pd.DataFrame]:
    DATA.mkdir(parents=True, exist_ok=True)
    datasets = {}
    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        path = DATA / f"{key}_{tf}.parquet"
        if path.exists():
            datasets[symbol] = pd.read_parquet(path)
            continue
        sys.stdout.write(f"  Downloading {symbol} {tf}...")
        sys.stdout.flush()
        df = fetch_ohlcv(symbol, tf, months)
        if not df.empty:
            df.to_parquet(path, index=False)
            sys.stdout.write(f" {len(df)} candles\n")
            datasets[symbol] = df
        else:
            sys.stdout.write(" FAILED\n")
    return datasets


def get_d1_levels_at_time(d1_df: pd.DataFrame, current_ts, lookback: int = 120) -> list[Level]:
    """Get D1 levels valid at a given timestamp (no look-ahead)."""
    # Find D1 index at or before current_ts
    mask = d1_df["ts"] <= current_ts
    if mask.sum() < 50:
        return []
    d1_subset = d1_df[mask]
    current_idx = len(d1_subset) - 1
    return get_rolling_levels(
        d1_subset, current_idx,
        lookback=lookback,
        min_touches=2,
        tolerance_pct=1.5,  # wider for D1
        min_level_age=5,    # 5 days
        swing_order=3,      # fewer candles needed on D1
    )


def level_near_d1(level: Level, d1_levels: list[Level], proximity_pct: float = 2.0) -> bool:
    """Check if a lower-TF level is near any D1 level."""
    for d1 in d1_levels:
        if abs(level.price - d1.price) / level.price * 100 < proximity_pct:
            return True
    return False


def run_backtest(
    entry_df: pd.DataFrame,
    d1_df: pd.DataFrame,
    symbol: str,
    tf: str,
    mode: str,  # "base", "d1_only", "confluence", "d1_filter"
    # Entry TF params
    level_lookback: int = 200,
    level_tolerance_pct: float = 1.0,
    retest_window: int = 15,
    min_level_age: int = 20,
    trend_sma: int = 50,
    rr_ratio: float = 3.0,
    max_risk_pct: float = 3.0,
    check_interval: int = 6,
    max_hold: int = 150,
    # D1 params
    d1_proximity_pct: float = 2.0,
) -> dict:
    """
    Modes:
      base       - standard: entry TF levels only (baseline)
      d1_only    - use D1 levels for signals, entry TF for timing
      d1_filter  - entry TF levels, but only if near a D1 level
      confluence - entry TF levels that align with D1 levels (strictest)
    """
    sma = entry_df["close"].rolling(trend_sma).mean()
    trend = pd.Series(0, index=entry_df.index)
    trend[entry_df["close"] > sma] = 1
    trend[entry_df["close"] < sma] = -1

    capital = INITIAL_CAPITAL
    trades = []
    active_trade = None
    recent_breakouts: list[Breakout] = []
    cached_levels = []
    cached_d1_levels = []
    last_check = 0
    last_d1_check_ts = None
    warmup = max(level_lookback, trend_sma) + 10

    for i in range(warmup, len(entry_df)):
        # -- Manage active trade --
        if active_trade is not None:
            h, l = entry_df["high"].iloc[i], entry_df["low"].iloc[i]
            at = active_trade
            outcome, exit_price = None, None
            if at["is_long"]:
                if l <= at["sl"]: outcome, exit_price = "loss", at["sl"]
                elif h >= at["tp"]: outcome, exit_price = "win", at["tp"]
                elif i - at["entry_idx"] > max_hold: outcome, exit_price = "timeout", entry_df["close"].iloc[i]
            else:
                if h >= at["sl"]: outcome, exit_price = "loss", at["sl"]
                elif l <= at["tp"]: outcome, exit_price = "win", at["tp"]
                elif i - at["entry_idx"] > max_hold: outcome, exit_price = "timeout", entry_df["close"].iloc[i]

            if outcome:
                ep = at["entry_price"]
                pnl_pct = ((exit_price - ep) / ep if at["is_long"] else (ep - exit_price) / ep) - 2 * FEE_BPS / 10000
                pnl_usd = at["position_size"] * pnl_pct
                capital += pnl_usd
                trades.append({
                    "outcome": outcome, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                    "capital_after": capital, "type": at["type"],
                    "hold_candles": i - at["entry_idx"],
                    "d1_aligned": at.get("d1_aligned", False),
                })
                active_trade = None
            else:
                continue

        # -- Update D1 levels (once per day) --
        current_ts = entry_df["ts"].iloc[i]
        if last_d1_check_ts is None or (current_ts - last_d1_check_ts).total_seconds() > 86400:
            cached_d1_levels = get_d1_levels_at_time(d1_df, current_ts)
            last_d1_check_ts = current_ts

        # -- Update entry TF levels --
        if i - last_check >= check_interval:
            if mode == "d1_only":
                # Use D1 levels projected onto entry TF
                cached_levels = cached_d1_levels
            else:
                cached_levels = get_rolling_levels(
                    entry_df, i, level_lookback, 2,
                    level_tolerance_pct, min_level_age,
                )

                if mode == "d1_filter":
                    # Keep only entry-TF levels near D1 levels
                    cached_levels = [lv for lv in cached_levels
                                     if level_near_d1(lv, cached_d1_levels, d1_proximity_pct)]
                elif mode == "confluence":
                    # Keep only levels that exist on BOTH TFs
                    cached_levels = [lv for lv in cached_levels
                                     if level_near_d1(lv, cached_d1_levels, d1_proximity_pct)]

            last_check = i

            new_brk = detect_breakouts(entry_df, i, cached_levels)
            recent_breakouts.extend(new_brk)
            recent_breakouts = [b for b in recent_breakouts if i - b.idx <= retest_window]

        # -- Find signals --
        close_i = entry_df["close"].iloc[i]
        retest = detect_retests(entry_df, i, recent_breakouts, retest_window, trend)
        zakol = detect_zakol(entry_df, i, cached_levels, trend)
        best = retest if retest and (not zakol or retest[2] >= zakol[2]) else zakol

        if best and capital > 100:
            sig_type, lv, _ = best
            signal = build_signal(symbol, sig_type, lv, close_i, rr_ratio)
            if signal is None:
                continue

            sl_dist = abs(close_i - signal.sl)
            risk_amt = capital * max_risk_pct / 100
            pos_size = min(risk_amt / (sl_dist / close_i), capital * 0.90)

            d1_aligned = level_near_d1(lv, cached_d1_levels, d1_proximity_pct)

            active_trade = {
                "entry_idx": i, "entry_price": close_i,
                "sl": signal.sl, "tp": signal.tp,
                "is_long": signal.is_long,
                "position_size": pos_size,
                "type": sig_type.value,
                "d1_aligned": d1_aligned,
            }

    if not trades:
        return {"symbol": symbol, "mode": mode, "n_trades": 0, "win_rate": 0,
                "total_return": 0, "profit_factor": 0, "trades": []}

    tdf = pd.DataFrame(trades)
    wins = tdf[tdf["outcome"] == "win"]
    losses = tdf[tdf["outcome"] == "loss"]
    gw = wins["pnl_usd"].sum() if len(wins) > 0 else 0
    gl = abs(losses["pnl_usd"].sum()) if len(losses) > 0 else 1

    return {
        "symbol": symbol, "mode": mode,
        "n_trades": len(tdf),
        "win_rate": len(wins) / len(tdf),
        "total_return": (tdf["capital_after"].iloc[-1] - INITIAL_CAPITAL) / INITIAL_CAPITAL,
        "profit_factor": gw / gl if gl > 0 else 0,
        "avg_hold": tdf["hold_candles"].mean(),
        "d1_aligned_pct": tdf["d1_aligned"].mean() * 100 if "d1_aligned" in tdf.columns else 0,
        "trades": trades,
    }


def run_all_modes(entry_datasets, d1_datasets, tf, tf_params):
    """Run all modes for a given entry TF."""
    modes = ["base", "d1_filter", "d1_only"]

    for mode in modes:
        print(f"\n{'='*70}")
        print(f"  {tf.upper()} / {mode}: R:R=1:{tf_params['rr_ratio']}, hold<={tf_params['max_hold']}")
        print(f"{'='*70}")

        all_trades = []
        results = []

        for symbol in SYMBOLS:
            if symbol not in entry_datasets or symbol not in d1_datasets:
                continue

            r = run_backtest(
                entry_datasets[symbol], d1_datasets[symbol],
                symbol, tf, mode, **tf_params,
            )
            results.append(r)
            for t in r["trades"]:
                t["symbol"] = symbol
                all_trades.append(t)

            if r["n_trades"] > 0:
                d1a = f", D1={r['d1_aligned_pct']:.0f}%" if mode == "base" else ""
                print(f"  {symbol:12s}: {r['n_trades']:3d}t, {r['win_rate']*100:.0f}% WR, "
                      f"{r['total_return']*100:+.1f}%, PF {r['profit_factor']:.2f}{d1a}")

        if not all_trades:
            print("  No trades!")
            continue

        tdf = pd.DataFrame(all_trades)
        wins = tdf[tdf["outcome"] == "win"]
        losses = tdf[tdf["outcome"] == "loss"]
        n = len(tdf)
        wr = len(wins) / n
        aw = wins["pnl_pct"].mean() if len(wins) > 0 else 0
        al = abs(losses["pnl_pct"].mean()) if len(losses) > 0 else 0
        exp = wr * aw - (1 - wr) * al
        gw = wins["pnl_usd"].sum() if len(wins) > 0 else 0
        gl = abs(losses["pnl_usd"].sum()) if len(losses) > 0 else 1
        pf = gw / gl if gl > 0 else 0

        hpc = 1 if tf == "1h" else 4
        date_range = max(1, (tdf["hold_candles"].count()))  # approx
        period_months = len(entry_datasets[SYMBOLS[0]]) * hpc / (24 * 30) * 0.85
        tpm = n / period_months if period_months > 0 else 0

        monthly = tpm * exp * tf_params["max_risk_pct"] / 100
        annual = (1 + monthly) ** 12 - 1

        avg_h = tdf["hold_candles"].mean() * hpc

        # D1 alignment analysis (only for base mode)
        if mode == "base" and "d1_aligned" in tdf.columns:
            d1_aligned = tdf[tdf["d1_aligned"] == True]
            d1_not = tdf[tdf["d1_aligned"] == False]
            if len(d1_aligned) > 5 and len(d1_not) > 5:
                wr_d1 = (d1_aligned["outcome"] == "win").mean()
                wr_no = (d1_not["outcome"] == "win").mean()
                print(f"\n  D1 alignment analysis:")
                print(f"    With D1:    {len(d1_aligned):3d}t, {wr_d1*100:.0f}% WR")
                print(f"    Without D1: {len(d1_not):3d}t, {wr_no*100:.0f}% WR")
                print(f"    Delta:      {(wr_d1-wr_no)*100:+.1f}pp")

        print(f"\n  TOTAL: {n}t ({tpm:.0f}/mo), WR {wr*100:.1f}%, PF {pf:.2f}, "
              f"Exp {exp*100:+.3f}%, Monthly {monthly*100:+.2f}%, Annual {annual*100:+.1f}%, "
              f"Hold {avg_h:.0f}h")

        return {"mode": mode, "tf": tf, "n": n, "tpm": tpm, "wr": wr,
                "pf": pf, "exp": exp, "monthly": monthly, "annual": annual, "hold_h": avg_h}


def main():
    REPORTS.mkdir(parents=True, exist_ok=True)
    print("=== Multi-TF Backtest: D1 Levels + Entry TF ===\n")

    # Load D1 data
    print("Loading D1 data...")
    d1_data = load_data("1d", 12)
    print(f"  {len(d1_data)} symbols\n")

    # 4H params
    params_4h = dict(
        level_lookback=200, level_tolerance_pct=1.0, retest_window=15,
        min_level_age=20, trend_sma=50, rr_ratio=3.0, max_risk_pct=3.0,
        check_interval=6, max_hold=150, d1_proximity_pct=2.0,
    )

    # 1H scalp params
    params_1h = dict(
        level_lookback=72, level_tolerance_pct=0.5, retest_window=6,
        min_level_age=6, trend_sma=20, rr_ratio=2.0, max_risk_pct=2.0,
        check_interval=2, max_hold=24, d1_proximity_pct=2.0,
    )

    summaries = []

    # 4H tests
    print("Loading 4H data...")
    data_4h = load_data("4h", 8)
    for mode in ["base", "d1_filter", "d1_only"]:
        print(f"\n{'='*70}")
        print(f"  4H / {mode}")
        print(f"{'='*70}")

        all_trades = []
        for symbol in SYMBOLS:
            if symbol not in data_4h or symbol not in d1_data:
                continue
            r = run_backtest(data_4h[symbol], d1_data[symbol], symbol, "4h", mode, **params_4h)
            for t in r["trades"]:
                t["symbol"] = symbol
                all_trades.append(t)
            if r["n_trades"] > 0:
                print(f"  {symbol:12s}: {r['n_trades']:3d}t, {r['win_rate']*100:.0f}% WR, "
                      f"{r['total_return']*100:+.1f}%, PF {r['profit_factor']:.2f}")

        if all_trades:
            tdf = pd.DataFrame(all_trades)
            wins = tdf[tdf["outcome"] == "win"]
            losses = tdf[tdf["outcome"] == "loss"]
            n = len(tdf)
            wr = len(wins) / n
            aw = wins["pnl_pct"].mean() if len(wins) > 0 else 0
            al = abs(losses["pnl_pct"].mean()) if len(losses) > 0 else 0
            exp = wr * aw - (1 - wr) * al
            gw = wins["pnl_usd"].sum() if len(wins) > 0 else 0
            gl = abs(losses["pnl_usd"].sum()) if len(losses) > 0 else 1
            pf = gw / gl
            period_months = len(data_4h[SYMBOLS[0]]) * 4 / (24 * 30) * 0.85
            tpm = n / period_months
            monthly = tpm * exp * 3.0 / 100
            annual = (1 + monthly) ** 12 - 1
            avg_h = tdf["hold_candles"].mean() * 4

            # D1 alignment analysis for base mode
            if mode == "base" and "d1_aligned" in tdf.columns:
                d1y = tdf[tdf["d1_aligned"] == True]
                d1n = tdf[tdf["d1_aligned"] == False]
                if len(d1y) > 5 and len(d1n) > 5:
                    print(f"\n  D1 alignment (in base mode):")
                    print(f"    Near D1:    {len(d1y):3d}t, {(d1y['outcome']=='win').mean()*100:.0f}% WR")
                    print(f"    Not near:   {len(d1n):3d}t, {(d1n['outcome']=='win').mean()*100:.0f}% WR")

            print(f"\n  TOTAL: {n}t ({tpm:.0f}/mo), WR {wr*100:.1f}%, PF {pf:.2f}, "
                  f"Exp {exp*100:+.3f}%, Annual {annual*100:+.1f}%, Hold {avg_h:.0f}h")
            summaries.append({"name": f"4H/{mode}", "n": n, "tpm": tpm, "wr": wr,
                              "pf": pf, "exp": exp, "annual": annual, "hold_h": avg_h})

    # 1H tests
    print("\nLoading 1H data...")
    data_1h = load_data("1h", 4)
    for mode in ["base", "d1_filter", "d1_only"]:
        print(f"\n{'='*70}")
        print(f"  1H / {mode}")
        print(f"{'='*70}")

        all_trades = []
        for symbol in SYMBOLS:
            if symbol not in data_1h or symbol not in d1_data:
                continue
            r = run_backtest(data_1h[symbol], d1_data[symbol], symbol, "1h", mode, **params_1h)
            for t in r["trades"]:
                t["symbol"] = symbol
                all_trades.append(t)
            if r["n_trades"] > 0:
                print(f"  {symbol:12s}: {r['n_trades']:3d}t, {r['win_rate']*100:.0f}% WR, "
                      f"{r['total_return']*100:+.1f}%, PF {r['profit_factor']:.2f}")

        if all_trades:
            tdf = pd.DataFrame(all_trades)
            wins = tdf[tdf["outcome"] == "win"]
            losses = tdf[tdf["outcome"] == "loss"]
            n = len(tdf)
            wr = len(wins) / n
            aw = wins["pnl_pct"].mean() if len(wins) > 0 else 0
            al = abs(losses["pnl_pct"].mean()) if len(losses) > 0 else 0
            exp = wr * aw - (1 - wr) * al
            gw = wins["pnl_usd"].sum() if len(wins) > 0 else 0
            gl = abs(losses["pnl_usd"].sum()) if len(losses) > 0 else 1
            pf = gw / gl
            period_months = len(data_1h[SYMBOLS[0]]) / (24 * 30) * 0.85
            tpm = n / period_months
            monthly = tpm * exp * 2.0 / 100
            annual = (1 + monthly) ** 12 - 1
            avg_h = tdf["hold_candles"].mean()

            if mode == "base" and "d1_aligned" in tdf.columns:
                d1y = tdf[tdf["d1_aligned"] == True]
                d1n = tdf[tdf["d1_aligned"] == False]
                if len(d1y) > 5 and len(d1n) > 5:
                    print(f"\n  D1 alignment (in base mode):")
                    print(f"    Near D1:    {len(d1y):3d}t, {(d1y['outcome']=='win').mean()*100:.0f}% WR")
                    print(f"    Not near:   {len(d1n):3d}t, {(d1n['outcome']=='win').mean()*100:.0f}% WR")

            print(f"\n  TOTAL: {n}t ({tpm:.0f}/mo), WR {wr*100:.1f}%, PF {pf:.2f}, "
                  f"Exp {exp*100:+.3f}%, Annual {annual*100:+.1f}%, Hold {avg_h:.0f}h")
            summaries.append({"name": f"1H/{mode}", "n": n, "tpm": tpm, "wr": wr,
                              "pf": pf, "exp": exp, "annual": annual, "hold_h": avg_h})

    # Comparison
    if summaries:
        print(f"\n{'='*70}")
        print(f"  COMPARISON")
        print(f"{'='*70}")
        print(f"  {'Config':<16} {'Trades':>7} {'T/mo':>6} {'WR':>6} {'PF':>6} {'Exp':>9} {'Annual':>8} {'Hold':>6}")
        for s in summaries:
            print(f"  {s['name']:<16} {s['n']:>7} {s['tpm']:>5.0f} {s['wr']*100:>5.1f}% "
                  f"{s['pf']:>5.2f} {s['exp']*100:>+8.3f}% {s['annual']*100:>+7.1f}% {s['hold_h']:>5.0f}h")


if __name__ == "__main__":
    main()
