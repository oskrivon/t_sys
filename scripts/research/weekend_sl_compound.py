"""Weekend SL comparison with COMPOUND returns at 3x leverage."""
from __future__ import annotations

import sys
from math import sqrt
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from scripts.research.validate_weekend_macro import load_macro_data, build_weekend_dataset
from scripts.research.weekend_mfe_trailing import (
    augment_dataset_with_missing_predictors,
    load_btc_1h,
    simulate_weekend_trades,
)

DATA = ROOT / "data" / "processed" / "candles"
LEVERAGE = 3
FEE_RT = 0.08  # %


def compound_equity(pnls_1x, leverage, fee_pct):
    """Compute equity curve with compounding."""
    equity = [1.0]
    for p in pnls_1x:
        trade_ret = p * leverage / 100 - fee_pct / 100
        equity.append(equity[-1] * (1 + trade_ret))
    return np.array(equity)


def compound_stats(label, pnls_1x, leverage, fee_pct, years):
    eq = compound_equity(pnls_1x, leverage, fee_pct)
    total_ret = (eq[-1] / eq[0] - 1) * 100
    cagr = (eq[-1] ** (1 / years) - 1) * 100 if years > 0 else 0
    # Max drawdown
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak * 100
    mdd = dd.min()
    # Trade returns
    trade_rets = np.diff(eq) / eq[:-1] * 100
    worst = trade_rets.min()
    wr = (trade_rets > 0).mean() * 100
    freq = len(pnls_1x) / years
    sh = trade_rets.mean() / trade_rets.std() * sqrt(freq) if trade_rets.std() > 0 else 0
    final_usd = 1000 * eq[-1]
    print(f"  {label:<25} CAGR={cagr:>+6.1f}%  Tot={total_ret:>+9.1f}%  "
          f"Sh={sh:>+5.2f}  MDD={mdd:>+6.1f}%  "
          f"Worst={worst:>+6.1f}%  WR={wr:>4.1f}%  "
          f"$1000->${final_usd:>8,.0f}")


def main():
    btc_4h = pd.read_parquet(DATA / "BTCUSDT_4h.parquet")
    btc_4h["ts"] = pd.to_datetime(btc_4h["ts"], utc=True)
    btc_4h = btc_4h.set_index("ts").sort_index()
    macro = load_macro_data()
    ds = build_weekend_dataset(btc_4h, macro)
    ds = augment_dataset_with_missing_predictors(ds)
    btc_1h = load_btc_1h()
    df = simulate_weekend_trades(btc_1h, ds)

    pnls_raw = df.pnl.values
    years = (df.date.max() - df.date.min()).days / 365.25

    print("=" * 85)
    print(f"COMPOUND RETURNS ({LEVERAGE}x leverage, {FEE_RT}% fee/trade)")
    print("=" * 85)
    print(f"\nTrades: {len(df)}, Years: {years:.1f}\n")

    compound_stats("No SL", pnls_raw, LEVERAGE, FEE_RT, years)
    print()
    for sl in [0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 5.0]:
        pnls_sl = np.where(df.mae.values >= sl, -sl, pnls_raw)
        compound_stats(f"SL {sl}%", pnls_sl, LEVERAGE, FEE_RT, years)

    # ── Equity path ──
    print(f"\n{'=' * 85}")
    print("EQUITY PATH: $1000 start")
    print(f"{'=' * 85}\n")
    eq_nosl = compound_equity(pnls_raw, LEVERAGE, FEE_RT) * 1000
    eq_sl2 = compound_equity(
        np.where(df.mae.values >= 2.0, -2.0, pnls_raw), LEVERAGE, FEE_RT
    ) * 1000
    eq_sl25 = compound_equity(
        np.where(df.mae.values >= 2.5, -2.5, pnls_raw), LEVERAGE, FEE_RT
    ) * 1000

    hdr = f"  {'Trade':<8}{'No SL':>10}{'SL 2%':>10}{'SL 2.5%':>10}  {'PnL(noSL)':>10}"
    print(hdr)
    print(f"  {'-' * 55}")

    worst_idx = set(np.argsort(pnls_raw)[:5])
    checkpoints = sorted(set(list(range(0, len(df), 10)) + list(worst_idx) + [len(df) - 1]))
    for i in checkpoints:
        if i < len(df):
            p = pnls_raw[i] * LEVERAGE
            mark = " *** WORST" if i in worst_idx else ""
            print(f"  T={i:<5}${eq_nosl[i+1]:>8,.0f} ${eq_sl2[i+1]:>8,.0f} "
                  f"${eq_sl25[i+1]:>8,.0f}  {p:>+6.2f}%{mark}")

    # ── Recovery analysis ──
    print(f"\n{'=' * 85}")
    print("RECOVERY AFTER WORST TRADES (No SL)")
    print(f"{'=' * 85}\n")
    eq = compound_equity(pnls_raw, LEVERAGE, FEE_RT)
    trade_rets = np.diff(eq) / eq[:-1]
    worst_5 = np.argsort(trade_rets)[:5]
    for idx in sorted(worst_5):
        loss_pct = trade_rets[idx] * 100
        pre_level = eq[idx]
        recovered = False
        for j in range(idx + 1, len(eq)):
            if eq[j] >= pre_level:
                recovery_trades = j - idx - 1
                print(f"  Trade {idx}: {loss_pct:+.1f}% loss -> "
                      f"recovered in {recovery_trades} trades "
                      f"(~{recovery_trades} weeks)")
                recovered = True
                break
        if not recovered:
            remaining = len(eq) - idx - 2
            print(f"  Trade {idx}: {loss_pct:+.1f}% loss -> "
                  f"NOT recovered ({remaining} trades left)")

    # ── Risk metrics ──
    print(f"\n{'=' * 85}")
    print("RISK ANALYSIS: what if worst trades cluster?")
    print(f"{'=' * 85}\n")

    # Simulate: 2 worst trades back to back
    worst_2_pnls = sorted(pnls_raw)[:2]
    double_hit = 1.0
    for p in worst_2_pnls:
        double_hit *= (1 + p * LEVERAGE / 100)
    double_hit_loss = (1 - double_hit) * 100
    print(f"  2 worst trades back-to-back: -{double_hit_loss:.1f}% of capital")
    print(f"    (trade 1: {worst_2_pnls[0]*LEVERAGE:+.1f}%, "
          f"trade 2: {worst_2_pnls[1]*LEVERAGE:+.1f}%)")

    # 3 worst
    worst_3_pnls = sorted(pnls_raw)[:3]
    triple_hit = 1.0
    for p in worst_3_pnls:
        triple_hit *= (1 + p * LEVERAGE / 100)
    triple_hit_loss = (1 - triple_hit) * 100
    print(f"  3 worst trades back-to-back: -{triple_hit_loss:.1f}% of capital")

    # What % of capital survives worst-case?
    print(f"\n  Catastrophe scenarios (starting $10,000):")
    for scenario, trades in [
        ("Worst 1 trade", sorted(pnls_raw)[:1]),
        ("Worst 2 consecutive", sorted(pnls_raw)[:2]),
        ("Worst 3 consecutive", sorted(pnls_raw)[:3]),
        ("Worst 5 consecutive", sorted(pnls_raw)[:5]),
    ]:
        cap = 10000
        for p in trades:
            cap *= (1 + p * LEVERAGE / 100)
        print(f"    {scenario:<25} -> ${cap:,.0f} "
              f"(lost ${10000-cap:,.0f}, -{(1-cap/10000)*100:.1f}%)")

    # Compare with SL 2.5% catastrophe
    print(f"\n  Same scenarios with SL 2.5%:")
    for scenario, trades in [
        ("Worst 1 trade", sorted(pnls_raw)[:1]),
        ("Worst 2 consecutive", sorted(pnls_raw)[:2]),
        ("Worst 3 consecutive", sorted(pnls_raw)[:3]),
        ("Worst 5 consecutive", sorted(pnls_raw)[:5]),
    ]:
        cap = 10000
        for p in trades:
            p_capped = max(p, -2.5)
            cap *= (1 + p_capped * LEVERAGE / 100)
        print(f"    {scenario:<25} -> ${cap:,.0f} "
              f"(lost ${10000-cap:,.0f}, -{(1-cap/10000)*100:.1f}%)")

    # ── Final verdict ──
    print(f"\n{'=' * 85}")
    print("VERDICT")
    print(f"{'=' * 85}\n")

    eq_nosl_final = compound_equity(pnls_raw, LEVERAGE, FEE_RT)
    eq_sl25_final = compound_equity(
        np.where(df.mae.values >= 2.5, -2.5, pnls_raw), LEVERAGE, FEE_RT
    )
    nosl_cagr = (eq_nosl_final[-1] ** (1/years) - 1) * 100
    sl25_cagr = (eq_sl25_final[-1] ** (1/years) - 1) * 100

    peak_nosl = np.maximum.accumulate(eq_nosl_final)
    mdd_nosl = ((eq_nosl_final - peak_nosl) / peak_nosl * 100).min()
    peak_sl25 = np.maximum.accumulate(eq_sl25_final)
    mdd_sl25 = ((eq_sl25_final - peak_sl25) / peak_sl25 * 100).min()

    print(f"  No SL:     CAGR={nosl_cagr:+.1f}%/yr, MDD={mdd_nosl:+.1f}%, "
          f"$1000 -> ${eq_nosl_final[-1]*1000:,.0f}")
    print(f"  SL 2.5%:   CAGR={sl25_cagr:+.1f}%/yr, MDD={mdd_sl25:+.1f}%, "
          f"$1000 -> ${eq_sl25_final[-1]*1000:,.0f}")
    print(f"\n  Difference: No SL earns {nosl_cagr - sl25_cagr:+.1f}%/yr more")
    print(f"  But worst drawdown is {abs(mdd_nosl) - abs(mdd_sl25):.1f}pp deeper")

    print(f"\n{'=' * 85}")
    print("DONE")
    print(f"{'=' * 85}")


if __name__ == "__main__":
    main()
