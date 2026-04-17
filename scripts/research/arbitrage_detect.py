"""
Детектор cross-exchange арбитражных окон на trades-based bid/ask proxy.

Вход: parquet-файлы из data/processed/arb_research/ (нормализованные per-second).
Выход: отчёт в data/reports/arb_trades_{month}.md + сводка stdout.

Модель:
- Для каждой секунды t и каждой пары (BTC/USDT, ETH/USDT) у нас есть по биржам:
    bid_proxy[exch][t] = max(price | side=sell) — верхняя граница bid
    ask_proxy[exch][t] = min(price | side=buy)  — нижняя граница ask
- Если в секунде нет сделки нужного знака, forward-fill с cap STALE_SEC.
- best_bid, exch_sell = max bid_proxy по всем биржам
  best_ask, exch_buy  = min ask_proxy по всем биржам
- Окно арба: exch_sell != exch_buy AND (best_bid - best_ask) / best_ask * 10000 > fee_bps
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

IN_DIR = Path("data/processed/arb_research")
OUT_DIR = Path("data/reports")

STALE_SEC = 30  # forward-fill не дальше этого числа секунд
FEE_BPS = 20.0  # round-trip (спот taker 10bps на каждой стороне, консервативно)

# (binance_symbol, bybit_symbol, okx_symbol, display)
PAIRS = [
    ("BTCUSDT", "BTCUSDT", "BTC-USDT", "BTC/USDT"),
    ("ETHUSDT", "ETHUSDT", "ETH-USDT", "ETH/USDT"),
]


def load_exchange(
    exchange: str, symbol: str, month: str
) -> pd.DataFrame:
    path = IN_DIR / f"{exchange}_{symbol}_{month}.parquet"
    df = pd.read_parquet(path, columns=["second_ms", "bid_proxy", "ask_proxy"])
    df = df.set_index("second_ms").sort_index()
    df.columns = [f"{exchange}_bid", f"{exchange}_ask"]
    return df


def build_wide(
    pair: tuple[str, str, str, str], month: str
) -> tuple[pd.DataFrame, str]:
    bn_sym, by_sym, okx_sym, display = pair
    binance = load_exchange("binance", bn_sym, month)
    bybit = load_exchange("bybit", by_sym, month)
    okx = load_exchange("okx", okx_sym, month)

    # полный индекс на каждую секунду месяца
    start = min(binance.index.min(), bybit.index.min(), okx.index.min())
    end = max(binance.index.max(), bybit.index.max(), okx.index.max())
    full = pd.RangeIndex(start=start, stop=end + 1000, step=1000, name="second_ms")

    df = pd.concat([binance, bybit, okx], axis=1).reindex(full)

    # forward-fill bid и ask отдельно по каждой бирже, с ограничением staleness
    for col in df.columns:
        df[col] = df[col].ffill(limit=STALE_SEC)

    return df, display


def find_arb(df: pd.DataFrame) -> pd.DataFrame:
    """
    Вернуть DataFrame с арб-секундами:
        second_ms (index), best_bid, best_ask, exch_sell, exch_buy,
        gross_bps, net_bps
    """
    bid_cols = [c for c in df.columns if c.endswith("_bid")]
    ask_cols = [c for c in df.columns if c.endswith("_ask")]

    bids = df[bid_cols]
    asks = df[ask_cols]

    # нужны оба значения — отсекаем до idxmax/idxmin (иначе pandas падает на all-NaN)
    has_bid = bids.notna().any(axis=1)
    has_ask = asks.notna().any(axis=1)
    mask_both = has_bid & has_ask

    best_bid = pd.Series(index=df.index, dtype="float64")
    best_bid_exch = pd.Series(index=df.index, dtype="object")
    best_ask = pd.Series(index=df.index, dtype="float64")
    best_ask_exch = pd.Series(index=df.index, dtype="object")

    if mask_both.any():
        valid_bids = bids[mask_both]
        valid_asks = asks[mask_both]
        best_bid.loc[mask_both] = valid_bids.max(axis=1)
        best_bid_exch.loc[mask_both] = (
            valid_bids.idxmax(axis=1).str.replace("_bid", "", regex=False)
        )
        best_ask.loc[mask_both] = valid_asks.min(axis=1)
        best_ask_exch.loc[mask_both] = (
            valid_asks.idxmin(axis=1).str.replace("_ask", "", regex=False)
        )
    # и чтоб биржи разные
    mask_diff = (best_bid_exch != best_ask_exch) & best_bid_exch.notna() & best_ask_exch.notna()

    gross_bps = (best_bid - best_ask) / best_ask * 10_000
    net_bps = gross_bps - FEE_BPS

    arb_mask = mask_both & mask_diff & (net_bps > 0)
    arb_mask = arb_mask.fillna(False)
    out = pd.DataFrame(
        {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "exch_sell": best_bid_exch,  # где продаём (взяли bid)
            "exch_buy": best_ask_exch,   # где покупаем (взяли ask)
            "gross_bps": gross_bps,
            "net_bps": net_bps,
        }
    )[arb_mask]
    return out


def compute_windows(arb: pd.DataFrame) -> pd.DataFrame:
    """Сгруппировать подряд идущие секунды одного направления в окна."""
    if arb.empty:
        return pd.DataFrame(
            columns=[
                "start_ms", "end_ms", "duration_s",
                "exch_sell", "exch_buy",
                "max_net_bps", "median_net_bps",
            ]
        )
    # направление меняется или пропуск секунды → новое окно
    direction = arb["exch_sell"] + ">" + arb["exch_buy"]
    ts = arb.index.to_series()
    gap = ts.diff().gt(1000) | direction.ne(direction.shift())
    window_id = gap.cumsum()

    grouped = arb.assign(wid=window_id.values).groupby("wid")
    out = grouped.agg(
        start_ms=("best_bid", lambda s: s.index.min()),
        end_ms=("best_bid", lambda s: s.index.max()),
        exch_sell=("exch_sell", "first"),
        exch_buy=("exch_buy", "first"),
        max_net_bps=("net_bps", "max"),
        median_net_bps=("net_bps", "median"),
    )
    out["duration_s"] = ((out["end_ms"] - out["start_ms"]) // 1000) + 1
    return out.reset_index(drop=True)


def pct(vals: pd.Series, q: float) -> float:
    if vals.empty:
        return float("nan")
    return float(vals.quantile(q))


def format_section(display: str, arb: pd.DataFrame, wins: pd.DataFrame, total_sec: int) -> str:
    lines = [f"## {display}", ""]
    if arb.empty:
        lines.append("**Арбитражных окон не найдено** (net_bps > 0 после комиссий).")
        lines.append("")
        return "\n".join(lines)

    total_arb_sec = len(arb)
    arb_pct = total_arb_sec / total_sec * 100 if total_sec else 0
    days = (total_sec / 86400) or 1

    lines += [
        f"- **Арбитражных секунд:** {total_arb_sec:,} ({arb_pct:.2f}% от всех)",
        f"- **Окон:** {len(wins):,}",
        f"- **Окон в сутки (средн.):** {len(wins) / days:.1f}",
        "",
        "### Net bps (после комиссий {:.0f} bps round-trip)".format(FEE_BPS),
        f"- медиана: {arb['net_bps'].median():.2f}",
        f"- p90:     {pct(arb['net_bps'], 0.90):.2f}",
        f"- p99:     {pct(arb['net_bps'], 0.99):.2f}",
        f"- max:     {arb['net_bps'].max():.2f}",
        "",
        "### Длительность окон (секунды)",
        f"- медиана: {wins['duration_s'].median():.0f}",
        f"- p90:     {pct(wins['duration_s'], 0.90):.0f}",
        f"- max:     {wins['duration_s'].max():.0f}",
        f"- 1-сек окон: {(wins['duration_s'] == 1).sum():,} ({(wins['duration_s'] == 1).mean() * 100:.1f}%)",
        f"- >=5с окон: {(wins['duration_s'] >= 5).sum():,} ({(wins['duration_s'] >= 5).mean() * 100:.1f}%)",
        f"- >=30с окон: {(wins['duration_s'] >= 30).sum():,}",
        "",
        "### Топ направлений (exch_sell -> exch_buy)",
    ]
    directions = (
        wins.groupby(["exch_sell", "exch_buy"]).size().sort_values(ascending=False)
    )
    for (s, b), cnt in directions.head(6).items():
        lines.append(f"- `{s} -> {b}`: {cnt:,} окон")
    lines.append("")

    # distribution by hour
    hours = pd.to_datetime(arb.index, unit="ms", utc=True).hour
    h_counts = pd.Series(hours).value_counts().sort_index()
    lines += [
        "### Распределение по часам UTC (arb-секунд)",
        "```",
        " h   count",
    ]
    for h in range(24):
        c = int(h_counts.get(h, 0))
        bar = "#" * min(40, c * 40 // max(1, int(h_counts.max())))
        lines.append(f"{h:2d}  {c:6d}  {bar}")
    lines += ["```", ""]

    return "\n".join(lines)


def run(month: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report_lines = [
        f"# Arbitrage research — trades-based proxy — {month}",
        "",
        f"**Комиссии:** {FEE_BPS:.0f} bps round-trip "
        f"(консервативно: spot taker ~10bps на каждой стороне)",
        f"**Staleness cap:** {STALE_SEC}s (forward-fill не дальше)",
        f"**Биржи:** Binance spot, Bybit spot, OKX spot",
        "",
        "## Методология",
        "Bid/ask proxy извлекаются из трейдов:",
        "- `bid_proxy = max(price | taker=sell)` — нижняя граница верха bid",
        "- `ask_proxy = min(price | taker=buy)` — верхняя граница низа ask",
        "",
        "**Это недооценка quoted-спреда.** Если по proxy арб виден — он гарантированно есть.",
        "Если по proxy арба нет — возможно, на quoted-уровне узкие окна всё же есть (этап 2 — Tardis).",
        "",
    ]

    print(f"\n=== Arb detection, {month} ===")
    for pair in PAIRS:
        display = pair[3]
        print(f"\n-- {display} --")
        df, _ = build_wide(pair, month)
        print(f"  wide grid: {len(df):,} seconds, "
              f"non-null bid_proxy (any exch): {df.filter(like='_bid').notna().any(axis=1).sum():,}")

        arb = find_arb(df)
        wins = compute_windows(arb)

        print(f"  arb seconds: {len(arb):,}")
        print(f"  windows:     {len(wins):,}")
        if not arb.empty:
            print(
                f"  net_bps: median={arb['net_bps'].median():.2f}  "
                f"p99={pct(arb['net_bps'], 0.99):.2f}  max={arb['net_bps'].max():.2f}"
            )
            print(
                f"  duration_s: median={wins['duration_s'].median():.0f}  "
                f"max={wins['duration_s'].max():.0f}"
            )

        report_lines.append(format_section(display, arb, wins, len(df)))

    report_path = OUT_DIR / f"arb_trades_{month}.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"\nReport: {report_path}")


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM (напр. 2026-03)")
    args = ap.parse_args()
    run(args.month)


if __name__ == "__main__":
    cli()
