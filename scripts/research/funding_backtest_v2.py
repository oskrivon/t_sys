"""
Funding arb backtest v2 — три сценария:

1) Spot+perp threshold (realistic fees: 24bps RT = 10bps spot × 2 + 2bps perp maker × 2)
2) Cross-exchange funding arb (perp-only, 8bps RT = 2bps perp maker × 4 legs)
3) Buy-and-hold (одноразовый вход, без exit — копим funding)

Вход: data/raw/funding/{exchange}/{BASE}.parquet
Выход: data/reports/funding_backtest_v2.md
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

IN = Path("data/raw/funding")
OUT = Path("data/reports")

EXCHANGES = ["binance", "bybit", "okx"]

# реалистичные VIP0 комиссии
SPOT_MAKER_BPS = 10.0    # Binance/Bybit/OKX базовый spot maker
PERP_MAKER_BPS = 2.0     # Binance/Bybit USDT-M maker
SPOT_PERP_RT_BPS = 2 * (SPOT_MAKER_BPS + PERP_MAKER_BPS)  # 24 bps
CROSS_EXCH_RT_BPS = 4 * PERP_MAKER_BPS                     # 8 bps


def load(exch: str, base: str) -> pd.DataFrame | None:
    p = IN / exch / f"{base}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p).sort_values("ts_ms").reset_index(drop=True)
    if df.empty:
        return None
    return df


def infer_events_per_day(df: pd.DataFrame) -> float:
    if len(df) < 2:
        return 3.0
    deltas = df["ts_ms"].diff().dropna()
    median_sec = deltas.median() / 1000
    return 86400 / max(median_sec, 1)


# ── Scenario 3: Buy-and-hold ──────────────────────────────

def backtest_hold(df: pd.DataFrame, entry_cost_bps: float) -> dict:
    """Вход в позицию один раз в начале, выход в конце. PnL = cum(funding) - entry_cost."""
    rates = df["funding_rate"].astype(float).values
    cum = rates.cumsum()
    total_received = float(cum[-1]) if len(cum) else 0.0
    cost = entry_cost_bps / 10_000
    net = total_received - cost
    # max drawdown от entry cost
    running = cum - cost
    peak = running.max()
    dd = (running - pd.Series(running).cummax()).min()
    span_days = (df["ts_ms"].max() - df["ts_ms"].min()) / 86_400_000 if len(df) > 1 else 1
    events_per_day = infer_events_per_day(df)
    apr = (total_received * events_per_day * 365 / len(rates)) * 100 if len(rates) else 0

    return {
        "total_received_bps": total_received * 10_000,
        "entry_cost_bps": entry_cost_bps,
        "net_bps": net * 10_000,
        "net_pct": net * 100,
        "apr_pct": apr,
        "max_dd_pct": float(dd) * 100,
        "span_days": span_days,
        "n_events": len(rates),
    }


# ── Scenario 2: Cross-exchange funding arb ────────────────

def backtest_cross_exchange(
    df_a: pd.DataFrame, df_b: pd.DataFrame,
    exch_a: str, exch_b: str,
    entry_div_bps: float, exit_div_bps: float, rt_bps: float
) -> dict:
    """
    Арбитраж divergence funding между двумя биржами.
    Стратегия: когда funding_B - funding_A > threshold → short perp B + long perp A.
    (Ловим "B платит больше" → мы шортим на B, лонгим на A.)
    """
    # merge на ближайшее время
    merged = pd.merge_asof(
        df_a[["ts_ms", "funding_rate"]].rename(columns={"funding_rate": "rate_a"}),
        df_b[["ts_ms", "funding_rate"]].rename(columns={"funding_rate": "rate_b"}),
        on="ts_ms", tolerance=3_600_000, direction="nearest"
    ).dropna()
    if merged.empty:
        return {"n_trades": 0, "total_pct": 0, "avg_net_bps": 0}

    merged["div"] = merged["rate_b"] - merged["rate_a"]  # positive = B pays more
    entry_rate = entry_div_bps / 10_000 / (365 * 3)  # per-event threshold
    exit_rate = exit_div_bps / 10_000 / (365 * 3)
    rt_cost = rt_bps / 10_000

    position = 0
    trades = []
    current_received = 0.0
    current_len = 0
    for _, row in merged.iterrows():
        div = row["div"]
        if position == 0:
            if div > entry_rate:
                position = 1  # short B, long A → receive div
                current_received = 0.0
                current_len = 0
        else:
            current_received += div
            current_len += 1
            if div < exit_rate:
                net = current_received - rt_cost
                trades.append({"net": net, "len": current_len})
                position = 0
    if position == 1 and current_len > 0:
        trades.append({"net": current_received - rt_cost, "len": current_len})

    if not trades:
        return {"n_trades": 0, "total_pct": 0, "avg_net_bps": 0, "hit_rate": 0}
    total = sum(t["net"] for t in trades)
    hit = sum(1 for t in trades if t["net"] > 0) / len(trades) * 100
    return {
        "n_trades": len(trades),
        "total_pct": total * 100,
        "avg_net_bps": total / len(trades) * 10_000,
        "hit_rate": hit,
        "avg_len": sum(t["len"] for t in trades) / len(trades),
    }


def run() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Funding backtest v2",
        "",
        "## Fee model",
        f"- Spot+perp round-trip: **{SPOT_PERP_RT_BPS:.0f} bps** "
        f"(spot maker {SPOT_MAKER_BPS:.0f} × 2 + perp maker {PERP_MAKER_BPS:.0f} × 2)",
        f"- Cross-exchange perp-only round-trip: **{CROSS_EXCH_RT_BPS:.0f} bps** "
        f"(perp maker {PERP_MAKER_BPS:.0f} × 4 legs)",
        "",
    ]

    # ─── Scenario 3: Buy-and-hold (most basic) ───────────
    lines += [
        "## Scenario 3: Buy-and-hold (вход один раз, сидим)",
        "",
        "Покупаем spot + short perp → держим forever → PnL = cum(funding) - entry fee",
        "",
        "| Exchange | Base | Net bps | Net % | APR % | Max DD % | Span (d) | Events |",
        "|---|---|---|---|---|---|---|---|",
    ]
    hold_rows = []
    bases = set()
    for exch in EXCHANGES:
        d = IN / exch
        if not d.exists():
            continue
        for p in sorted(d.glob("*.parquet")):
            base = p.stem
            bases.add(base)
            df = load(exch, base)
            if df is None:
                continue
            r = backtest_hold(df, SPOT_PERP_RT_BPS)
            r["exchange"] = exch
            r["base"] = base
            hold_rows.append(r)
    hold_rows.sort(key=lambda x: -x["net_pct"])
    for r in hold_rows:
        lines.append(
            f"| {r['exchange']:7s} | {r['base']:5s} | "
            f"{r['net_bps']:+.0f} | {r['net_pct']:+.2f}% | {r['apr_pct']:+.1f}% | "
            f"{r['max_dd_pct']:.2f}% | {r['span_days']:.0f} | {r['n_events']} |"
        )
    lines.append("")

    # Summary: positive net across pairs
    pos_hold = [r for r in hold_rows if r["net_pct"] > 0]
    neg_hold = [r for r in hold_rows if r["net_pct"] <= 0]
    lines += [
        f"**Profitable hold positions:** {len(pos_hold)}/{len(hold_rows)}",
        "",
    ]

    # ─── Scenario 2: Cross-exchange funding arb ──────────
    lines += [
        "## Scenario 2: Cross-exchange funding arb (perp-only)",
        "",
        f"Short perp on high-funding exchange, long perp on low-funding exchange. "
        f"RT fee: {CROSS_EXCH_RT_BPS:.0f} bps.",
        "",
        "Entry: |divergence| > 10% APR per-event, Exit: divergence < 0",
        "",
        "| Pair | Short-exch | Long-exch | Trades | Total % | Avg net bps | Hit rate | Avg len |",
        "|---|---|---|---|---|---|---|---|",
    ]
    cross_rows = []
    for base in sorted(bases):
        # try all exchange pairs
        for i, ea in enumerate(EXCHANGES):
            for eb in EXCHANGES[i+1:]:
                da = load(ea, base)
                db = load(eb, base)
                if da is None or db is None:
                    continue
                # try both directions: short B long A, and short A long B
                for short_exch, long_exch, dshort, dlong in [
                    (eb, ea, db, da), (ea, eb, da, db)
                ]:
                    r = backtest_cross_exchange(dlong, dshort, long_exch, short_exch, 10.0, 0.0, CROSS_EXCH_RT_BPS)
                    if r["n_trades"] > 0:
                        r["base"] = base
                        r["short_exch"] = short_exch
                        r["long_exch"] = long_exch
                        cross_rows.append(r)
    cross_rows.sort(key=lambda x: -x["total_pct"])
    for r in cross_rows[:30]:  # top 30
        lines.append(
            f"| {r['base']:5s} | {r['short_exch']:7s} | {r['long_exch']:7s} | "
            f"{r['n_trades']} | {r['total_pct']:+.2f}% | {r['avg_net_bps']:+.1f} | "
            f"{r['hit_rate']:.0f}% | {r['avg_len']:.1f} |"
        )
    lines.append("")
    pos_cross = [r for r in cross_rows if r["total_pct"] > 0]
    lines.append(
        f"**Profitable cross-exchange pairs:** {len(pos_cross)}/{len(cross_rows)}"
    )
    lines.append("")

    out_path = OUT / "funding_backtest_v2.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Report: {out_path}")


if __name__ == "__main__":
    run()
