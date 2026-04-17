"""
Анализ исторических funding rates: APR распределения, sign flips,
кросс-биржевое расхождение, подсказки для threshold-стратегии.

Вход:  data/raw/funding/{exchange}/{BASE}.parquet (ts_ms, funding_rate, mark_price)
Выход: data/reports/funding_analysis.md
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

IN = Path("data/raw/funding")
OUT = Path("data/reports")

EXCHANGES = ["binance", "bybit", "okx"]
# events_per_day вычисляем per-pair — TON на Bybit/OKX имеет 4-часовой funding (6/день)


def load_all() -> dict[tuple[str, str], pd.DataFrame]:
    out: dict[tuple[str, str], pd.DataFrame] = {}
    for exch in EXCHANGES:
        d = IN / exch
        if not d.exists():
            continue
        for p in sorted(d.glob("*.parquet")):
            base = p.stem
            df = pd.read_parquet(p)
            if df.empty:
                continue
            out[(exch, base)] = df
    return out


def infer_events_per_day(df: pd.DataFrame) -> float:
    """Средний интервал между событиями → events per day."""
    if len(df) < 2:
        return 3.0
    deltas = df.sort_values("ts_ms")["ts_ms"].diff().dropna()
    median_delta_sec = deltas.median() / 1000
    if median_delta_sec <= 0:
        return 3.0
    return 86400 / median_delta_sec


def per_series_stats(df: pd.DataFrame) -> dict:
    r = df["funding_rate"].astype(float)
    positive = (r > 0).sum()
    negative = (r < 0).sum()
    zero = (r == 0).sum()
    events_per_day = infer_events_per_day(df)
    annual_factor = 365 * events_per_day
    # signed "APR" — if rate averaged for a year
    apr_mean = r.mean() * annual_factor * 100
    apr_median = r.median() * annual_factor * 100

    # sign flips — транзиции + → −  и наоборот
    sign = r.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    flips = (sign.diff().abs() >= 2).sum()  # жёсткая смена знака (not to/from 0)

    span_days = (df["ts_ms"].max() - df["ts_ms"].min()) / 86_400_000 if len(df) > 1 else 0

    return {
        "n_events": len(r),
        "span_days": span_days,
        "pct_positive": positive / len(r) * 100 if len(r) else 0,
        "pct_negative": negative / len(r) * 100 if len(r) else 0,
        "apr_mean_pct": apr_mean,
        "apr_median_pct": apr_median,
        "events_per_day": events_per_day,
        "apr_p10_pct": r.quantile(0.10) * annual_factor * 100 if len(r) else 0,
        "apr_p90_pct": r.quantile(0.90) * annual_factor * 100 if len(r) else 0,
        "max_event_pct": r.max() * 100 if len(r) else 0,
        "min_event_pct": r.min() * 100 if len(r) else 0,
        "std_pct": r.std() * annual_factor * 100 if len(r) else 0,
        "sign_flips": flips,
        "flips_per_month": flips / (span_days / 30) if span_days > 0 else 0,
    }


def backtest_threshold(
    df: pd.DataFrame, entry_bps: float, exit_bps: float, roundtrip_bps: float
) -> dict:
    """
    Простая стратегия на одной паре:
        when APR > entry: open (long spot + short perp)
        when APR < exit: close
    PnL считаем в "basis points relative" на цикл:
        received = sum(funding_rate) между open/close (в долях, не в %)
        transaction_cost = roundtrip_bps / 10000 (вход+выход)
        net_trade = received - roundtrip_bps/10000
    """
    events_per_day = infer_events_per_day(df)
    annual_factor = 365 * events_per_day
    r = df.sort_values("ts_ms")["funding_rate"].astype(float).values
    entry_rate = entry_bps / (annual_factor * 100)
    exit_rate = exit_bps / (annual_factor * 100)
    rt_cost = roundtrip_bps / 10_000

    position = 0
    accumulated = 0.0
    trades = []
    current_received = 0.0
    current_len = 0
    for rate in r:
        if position == 0:
            if rate > entry_rate:
                position = 1
                current_received = 0.0
                current_len = 0
        else:
            current_received += rate
            current_len += 1
            if rate < exit_rate:
                net = current_received - rt_cost
                trades.append({"received": current_received, "net": net, "len": current_len})
                accumulated += net
                position = 0
                current_received = 0.0
                current_len = 0
    # если в позиции на конце — закрываем без комиссии выхода для полуоткрытой оценки
    if position == 1 and current_len > 0:
        trades.append({"received": current_received, "net": current_received - rt_cost, "len": current_len})
        accumulated += current_received - rt_cost

    n = len(trades)
    if n == 0:
        return {"n_trades": 0, "total_pct": 0, "avg_net_bps": 0, "hit_rate": 0, "avg_len_events": 0}
    avg_net = sum(t["net"] for t in trades) / n
    hit_rate = sum(1 for t in trades if t["net"] > 0) / n * 100
    avg_len = sum(t["len"] for t in trades) / n
    return {
        "n_trades": n,
        "total_pct": accumulated * 100,
        "avg_net_bps": avg_net * 10_000,
        "hit_rate": hit_rate,
        "avg_len_events": avg_len,
    }


def fmt_pct(x: float) -> str:
    return f"{x:+.1f}%" if abs(x) < 1000 else f"{x:+.0f}%"


def run(entry_aprs: list[float], exit_apr: float, roundtrip_bps: float) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    data = load_all()
    if not data:
        print("No data. Run download_funding.py first.")
        return

    # stats per (exch, base)
    rows = []
    for (exch, base), df in data.items():
        s = per_series_stats(df)
        s["exchange"] = exch
        s["base"] = base
        rows.append(s)
    stats_df = pd.DataFrame(rows)

    # pivot APR mean по биржам
    apr_pivot = stats_df.pivot(index="base", columns="exchange", values="apr_mean_pct")
    apr_pivot["mean3"] = apr_pivot.mean(axis=1)
    apr_pivot = apr_pivot.sort_values("mean3", ascending=False)

    lines = [
        "# Funding rate research — analysis",
        "",
        f"**Всего пар в анализе:** {len(stats_df)}",
        f"**Средний span:** {stats_df['span_days'].mean():.0f} дней",
        "",
        f"**Backtest params:** entry variable, exit APR={exit_apr:.0f}%, "
        f"roundtrip fees={roundtrip_bps:.0f} bps",
        "",
        "## APR (mean, %) по парам и биржам",
        "",
        "```",
        apr_pivot.round(1).to_string(),
        "```",
        "",
        "## Topline per-pair statistics",
        "",
        "| Exchange | Base | Events | Span (d) | APR mean | APR median | APR p10 | APR p90 | "
        "% positive | Max event | Min event | Flips/mo |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, s in stats_df.sort_values(["base", "exchange"]).iterrows():
        lines.append(
            f"| {s['exchange']:7s} | {s['base']} | {s['n_events']} | {s['span_days']:.0f} | "
            f"{fmt_pct(s['apr_mean_pct'])} | {fmt_pct(s['apr_median_pct'])} | "
            f"{fmt_pct(s['apr_p10_pct'])} | {fmt_pct(s['apr_p90_pct'])} | "
            f"{s['pct_positive']:.0f}% | {s['max_event_pct']:.3f}% | "
            f"{s['min_event_pct']:.3f}% | {s['flips_per_month']:.1f} |"
        )
    lines.append("")

    # Backtest sweep
    lines += [
        "## Backtest: простая threshold strategy",
        "",
        f"Entry: APR > threshold, Exit: APR < {exit_apr:.0f}%, Fees round-trip: {roundtrip_bps:.0f} bps",
        "",
    ]
    for entry in entry_aprs:
        lines += [
            f"### Entry threshold: {entry:.0f}% APR",
            "",
            "| Exchange | Base | Trades | Total % | Avg net bps | Hit rate | Avg len (events) |",
            "|---|---|---|---|---|---|---|",
        ]
        bt_rows = []
        for (exch, base), df in sorted(data.items()):
            bt = backtest_threshold(df, entry, exit_apr, roundtrip_bps)
            bt["exchange"] = exch
            bt["base"] = base
            bt_rows.append(bt)
        for b in sorted(bt_rows, key=lambda x: -x["total_pct"]):
            if b["n_trades"] == 0:
                continue
            lines.append(
                f"| {b['exchange']:7s} | {b['base']} | {b['n_trades']} | "
                f"{b['total_pct']:+.2f}% | {b['avg_net_bps']:+.1f} | "
                f"{b['hit_rate']:.0f}% | {b['avg_len_events']:.1f} |"
            )
        lines.append("")

    # Top candidates across thresholds
    lines += [
        "## Top-10 пар по среднему APR (все биржи среднее)",
        "",
        "| Rank | Base | Binance | Bybit | OKX | Mean |",
        "|---|---|---|---|---|---|",
    ]
    for i, (base, row) in enumerate(apr_pivot.head(10).iterrows(), 1):
        lines.append(
            f"| {i} | {base} | "
            f"{fmt_pct(row.get('binance', float('nan')))} | "
            f"{fmt_pct(row.get('bybit', float('nan')))} | "
            f"{fmt_pct(row.get('okx', float('nan')))} | "
            f"{fmt_pct(row['mean3'])} |"
        )
    lines.append("")

    # cross-exchange divergence
    lines += [
        "## Кросс-биржевое расхождение (std APR между биржами, %)",
        "",
        "Большое значение = funding сильно отличается между биржами → потенциал для cross-exchange "
        "funding arb (выбираем где больше платят).",
        "",
        "| Base | Std APR across exchanges |",
        "|---|---|",
    ]
    cross_std = apr_pivot[EXCHANGES].std(axis=1).sort_values(ascending=False)
    for base, v in cross_std.head(15).items():
        lines.append(f"| {base} | {v:.1f}% |")
    lines.append("")

    out_path = OUT / "funding_analysis.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Report: {out_path}")


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", nargs="+", type=float, default=[5.0, 10.0, 20.0, 50.0])
    ap.add_argument("--exit", type=float, default=0.0, dest="exit_apr")
    ap.add_argument("--fees", type=float, default=20.0, dest="roundtrip_bps")
    args = ap.parse_args()
    run(args.entry, args.exit_apr, args.roundtrip_bps)


if __name__ == "__main__":
    cli()
